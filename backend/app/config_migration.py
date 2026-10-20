"""Versioned definition exchange; resource rebinding, never code execution.

Imports are durable pending batches. A preview precedes an atomic, append-only
commit. Source IDs, fingerprints and machine paths are never copied as bindings.
"""

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

from . import config_center as cc, db, project_registry as registry

FORMAT = "unified-test-platform/config"
VERSION = 1
MAX_BYTES = 5 * 1024 * 1024
RESOURCE_FIELDS = {
    "cwd": "workdir",
    "cwd_root": "workdir",
    "entry": "pytest_entry",
    "suite": "ui_suite",
    "device": "ui_device",
    "requirements": "runtime_requirement",
    "locustfile": "locust_scenario",
    "dataset": "dataset",
    "asset_source_id": "asset_source",
}
RESOURCE_TYPES = (
    "environment",
    "executor",
    "asset_source",
    "workdir",
    "pytest_entry",
    "ui_suite",
    "ui_device",
    "runtime_requirement",
    "locust_scenario",
    "dataset",
)


class MigrationConflict(ValueError):
    pass


def initialize(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS migration_imports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        document TEXT NOT NULL, mapping TEXT NOT NULL DEFAULT '{}', prefix TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', result TEXT NOT NULL DEFAULT '{}',
        created_at TEXT, updated_at TEXT)"""
    )


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError("字段缺失或包含未支持字段：" + ", ".join(required))


def _text(value, label, maximum=120, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(label + " 必须为非空文本，最长 " + str(maximum) + " 字符")


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value):
        raise ValueError("迁移 ID 只允许字母、数字和 _ . : -，最长 120 字符")


def _marker(value):
    if not isinstance(value, dict) or set(value) != {"$resource"}:
        raise ValueError('资源引用必须写为 {"$resource":"资源槽 ID"}')
    _id(value["$resource"])
    return value["$resource"]


def validate_document(document):
    try:
        if len(_json(document).encode()) > MAX_BYTES:
            raise ValueError("迁移文件不能超过 5 MB")
    except (TypeError, RecursionError, OverflowError):
        raise ValueError("迁移文件必须是有效 JSON") from None
    _fields(
        document,
        ("format", "version", "created_at", "resources", "cases", "suites", "plans"),
        ("format", "version"),
    )
    if (
        document["format"] != FORMAT
        or type(document["version"]) is not int
        or document["version"] != VERSION
    ):
        raise ValueError("不支持的迁移格式 / 版本，请使用平台导出或下载 JSON 模板")
    out = deepcopy(document)
    limits = {"resources": 2000, "cases": 1000, "suites": 1000, "plans": 500}
    indexes = {}
    for group, limit in limits.items():
        values = out.setdefault(group, [])
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(group + " 必须为数组，最多 " + str(limit) + " 条")
        indexes[group] = {}
        for row in values:
            if not isinstance(row, dict):
                raise ValueError(group + " 条目必须为对象")
            _id(row.get("id"))
            if row["id"] in indexes[group]:
                raise ValueError(group + " 存在重复 ID：" + row["id"])
            indexes[group][row["id"]] = row
    if not any(out[g] for g in ("cases", "suites", "plans")):
        raise ValueError("迁移包至少需要一条用例、套件或计划")
    resources = indexes["resources"]
    for row in out["resources"]:
        _fields(row, ("id", "type", "name", "builtin_key"), ("id", "type", "name"))
        if row["type"] not in RESOURCE_TYPES:
            raise ValueError("资源类型不支持：" + str(row["type"]))
        _text(row["name"], "资源名称")
        if row.get("builtin_key"):
            _id(row["builtin_key"])

    def reference(ident, kind):
        if not isinstance(ident, str) or ident not in resources or resources[ident]["type"] != kind:
            raise ValueError("资源槽不存在或类型不匹配：" + str(ident) + " → " + kind)

    def params(value):
        if not isinstance(value, dict):
            raise ValueError("params / override_params 必须为对象")
        for key, kind in RESOURCE_FIELDS.items():
            if key in value and value[key] not in ("", None, 0):
                reference(_marker(value[key]), kind)

    common = {"id", "name", "description", "owner", "tags", "enabled"}
    for group in ("cases", "suites", "plans"):
        for row in out[group]:
            extra = (
                {"engine", "params", "requires_exclusive", "asset_kind"}
                if group == "cases"
                else (
                    {"cases"}
                    if group == "suites"
                    else {
                        "environment",
                        "executor",
                        "asset_source",
                        "steps",
                        "fail_fast",
                        "archived",
                    }
                )
            )
            allowed = (
                (common | extra) if group != "plans" else ((common - {"tags", "enabled"}) | extra)
            )
            _fields(row, allowed, ("id", "name"))
            _text(row["name"], group + " 名称")
            for key, limit in (("description", 4000), ("owner", 120)):
                if key in row:
                    _text(row[key], key, limit, empty=True)
            if "tags" in row and (
                not isinstance(row["tags"], list)
                or len(row["tags"]) > 100
                or any(not isinstance(t, str) or len(t) > 120 for t in row["tags"])
            ):
                raise ValueError("tags 须为最多 100 个短文本")
            for key in ("enabled", "requires_exclusive", "fail_fast", "archived"):
                if key in row and type(row[key]) is not bool:
                    raise ValueError(key + " 必须为布尔值")
            if group == "cases":
                if row.get("engine") not in cc.ENGINES:
                    raise ValueError("不支持的用例引擎")
                params(row.setdefault("params", {}))
                kind = row.get("asset_kind", "")
                if (
                    kind
                    and kind
                    != {
                        "pytest": "",
                        "airtest": "airtest",
                        "locust": "locust",
                        "matcheval": "matcheval",
                    }[row["engine"]]
                ):
                    raise ValueError("asset_kind 与用例引擎不一致")
            elif group == "suites":
                if not isinstance(row.get("cases"), list) or len(row["cases"]) > 1000:
                    raise ValueError("套件 cases 必须为用例 ID 数组")
                for ident in row["cases"]:
                    if not isinstance(ident, str) or ident not in indexes["cases"]:
                        raise ValueError("套件引用了未提供的用例：" + str(ident))
            else:
                for field, kind in (("environment", "environment"), ("executor", "executor")):
                    reference(row.get(field), kind)
                if row.get("asset_source"):
                    reference(row["asset_source"], "asset_source")
                if not isinstance(row.get("steps"), list) or not 1 <= len(row["steps"]) <= 500:
                    raise ValueError("计划需要 1..500 个步骤")
                for step in row["steps"]:
                    _fields(
                        step,
                        (
                            "case",
                            "engine",
                            "params",
                            "override_params",
                            "name",
                            "requires_exclusive",
                        ),
                    )
                    if "name" in step:
                        _text(step["name"], "步骤名", 120, empty=True)
                    if (
                        "requires_exclusive" in step
                        and type(step["requires_exclusive"]) is not bool
                    ):
                        raise ValueError("步骤独占标记必须为布尔值")
                    if step.get("case"):
                        if (
                            not isinstance(step["case"], str)
                            or step["case"] not in indexes["cases"]
                            or "engine" in step
                            or "params" in step
                        ):
                            raise ValueError("引用步骤只接受已提供的 case ID 与 override_params")
                        override = step.setdefault("override_params", {})
                        params(override)
                        if set(override) & {
                            "engine",
                            "asset_source_id",
                            "environment_id",
                            "executor_id",
                        }:
                            raise ValueError("步骤不允许覆盖引擎或执行资源 ID")
                    else:
                        if step.get("engine") not in cc.ENGINES or "override_params" in step:
                            raise ValueError("内联步骤需要合法 engine / params")
                        params(step.setdefault("params", {}))
    return out


def catalog(path):
    cat = cc.public_catalog(path)

    def rows(values):
        return [dict(value=str(r["id"]), label=r["name"]) for r in values if r.get("enabled", 1)]

    def key_rows(values):
        return [
            dict(value=r["key"], label=r.get("label") or r.get("name") or r["key"])
            for r in values
            if r.get("enabled", 1)
        ]

    return dict(
        environment=rows(db.list_environments(path)),
        executor=rows(db.list_executors(path)),
        asset_source=rows(db.list_asset_sources(path)),
        workdir=key_rows(cat["workdir_roots"]),
        pytest_entry=key_rows(cat["pytest_entries"]),
        ui_suite=key_rows(cat["asset"]["ui_suites"]),
        ui_device=key_rows(cat["asset"]["ui_devices"]),
        runtime_requirement=key_rows(cat["runtime_requirements"]),
        locust_scenario=key_rows(cat["asset"]["locust_scenarios"]),
        dataset=key_rows(cat["asset"]["datasets"]),
    )


def template():
    return dict(
        format=FORMAT,
        version=VERSION,
        resources=[
            dict(id="local-env", type="environment", name="我的被测环境"),
            dict(id="local-executor", type="executor", name="我的执行机"),
            dict(id="login-entry", type="pytest_entry", name="我的登录测试入口"),
        ],
        cases=[
            dict(
                id="login",
                name="自己的登录用例",
                engine="pytest",
                params=dict(entry={"$resource": "login-entry"}),
            )
        ],
        suites=[dict(id="regression", name="自己的回归套件", cases=["login"])],
        plans=[
            dict(
                id="daily",
                name="自己的每日回归计划",
                environment="local-env",
                executor="local-executor",
                steps=[dict(case="login")],
            )
        ],
    )


@registry.scoped
def export_document(db_path, selection):
    _fields(selection, ("all", "case_ids", "suite_ids", "plan_ids"))
    if "all" in selection and type(selection["all"]) is not bool:
        raise ValueError("all 必须为布尔值")
    all_cases = {r["id"]: r for r in db.list_test_cases(db_path)}
    all_suites = {r["id"]: r for r in db.list_suites(db_path)}
    all_plans = {r["id"]: r for r in db.list_plans(db_path)}

    def selected(group, values):
        ids = list(values) if selection.get("all", not selection) else selection.get(group, [])
        if not isinstance(ids, list) or any(type(i) is not int or i not in values for i in ids):
            raise ValueError("导出选择包含不存在的记录")
        return set(ids)

    cases, suites, plans = (
        selected("case_ids", all_cases),
        selected("suite_ids", all_suites),
        selected("plan_ids", all_plans),
    )

    def require_case(ref, source):
        case = all_cases.get(ref.get("id"))
        if (
            not case
            or case["revision"] != ref.get("revision")
            or case["fingerprint"] != ref.get("fingerprint")
        ):
            raise ValueError(source + " 引用的用例版本已变化，请同步引用后导出，或取消选择该记录")
        cases.add(case["id"])

    for sid in suites:
        for ref in all_suites[sid]["cases"]:
            require_case(ref, all_suites[sid]["name"])
    for pid in plans:
        for step in all_plans[pid]["steps"]:
            if step.get("case_ref"):
                require_case(step["case_ref"], all_plans[pid]["name"])
    slots = {}
    choices = catalog(db_path)

    def slot(kind, value):
        key = kind + ":" + str(value)
        if key not in slots:
            match = next((r for r in choices[kind] if r["value"] == str(value)), None)
            row = dict(id=key, type=kind, name=(match or {}).get("label") or key)
            if kind not in ("environment", "executor", "asset_source") and not re.match(
                r"(entry|project|ui-device|requirement)-\d+", str(value)
            ):
                row["builtin_key"] = str(value)
            slots[key] = row
        return key

    def portable(params):
        p = deepcopy(params)
        if p.get("suite") and not p.get("requirements"):
            from .runtime_requirements import default_key

            p["requirements"] = default_key(p["suite"])
        if p.get("entry"):
            # Selector/root are derived from the selected target entry after rebinding.
            registry.pytest_params(p)
            p.pop("args", None)
            p.pop("cwd", None)
            p.pop("cwd_root", None)
        if p.get("cwd"):
            value = p.pop("cwd")
            key = value if value in cc.WORKDIR_ROOTS else cc.infer_workdir_root(value)
            p["cwd_root"] = key or "legacy-" + hashlib.sha256(str(value).encode()).hexdigest()[:12]
        for field, kind in RESOURCE_FIELDS.items():
            if field not in p:
                continue
            value = p[field]
            if not value:
                p.pop(field)
                continue
            p[field] = {"$resource": slot(kind, value)}
        return p

    document = dict(
        format=FORMAT,
        version=VERSION,
        created_at=db._now(),
        resources=[],
        cases=[],
        suites=[],
        plans=[],
    )

    def common(row):
        return {
            key: row[key]
            for key in ("name", "description", "owner", "tags", "enabled")
            if key in row
        }

    for cid in sorted(cases):
        c = all_cases[cid]
        row = common(c)
        row.update(
            id="case:" + str(cid),
            engine=c["engine"],
            params=portable(c["params"]),
            requires_exclusive=bool(c["requires_exclusive"]),
            asset_kind=c["asset_kind"],
            enabled=bool(c["enabled"]),
        )
        document["cases"].append(row)
    for sid in sorted(suites):
        s = all_suites[sid]
        row = common(s)
        row.update(
            id="suite:" + str(sid),
            cases=["case:" + str(r["id"]) for r in s["cases"]],
            enabled=bool(s["enabled"]),
        )
        document["suites"].append(row)
    for pid in sorted(plans):
        p = all_plans[pid]
        steps = []
        for s in p["steps"]:
            step = {k: s[k] for k in ("name", "requires_exclusive") if k in s}
            if s.get("case_ref"):
                step.update(
                    case="case:" + str(s["case_ref"]["id"]),
                    override_params=portable(s.get("override_params") or {}),
                )
            else:
                step.update(engine=s["engine"], params=portable(s.get("params") or {}))
            steps.append(step)
        document["plans"].append(
            dict(
                id="plan:" + str(pid),
                name=p["name"],
                description=p["description"],
                owner=p["owner"],
                environment=slot("environment", p["environment_id"]),
                executor=slot("executor", p["executor_id"]),
                asset_source=(
                    slot("asset_source", p["asset_source_id"]) if p.get("asset_source_id") else None
                ),
                steps=steps,
                fail_fast=bool(p["fail_fast"]),
                archived=bool(p.get("archived")),
            )
        )
    document["resources"] = list(slots.values())
    return validate_document(document)


def batches(path):
    with db._LOCK, db.get_conn(path) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id,name,status,created_at,updated_at FROM migration_imports ORDER BY id DESC"
            )
        ]


def get_batch(path, ident):
    with db._LOCK, db.get_conn(path) as conn:
        row = conn.execute("SELECT * FROM migration_imports WHERE id=?", (ident,)).fetchone()
    if not row:
        raise LookupError("导入批次不存在")
    row = dict(row)
    for field in ("document", "mapping", "result"):
        row[field] = json.loads(row[field])
    return row


def stage(path, name, document):
    _text(name, "批次名称")
    doc = validate_document(document)
    choices = catalog(path)
    mapping = {}
    for r in doc["resources"]:
        builtin = (
            r.get("builtin_key")
            if r["type"] not in ("environment", "executor", "asset_source")
            and not re.match(
                r"(entry|project|runtime|ui-device|requirement)-\d+", str(r.get("builtin_key", ""))
            )
            else None
        )
        candidates = [
            c["value"]
            for c in choices[r["type"]]
            if c["label"] == r["name"] or c["value"] == builtin
        ]
        if len(set(candidates)) == 1:
            mapping[r["id"]] = candidates[0]
    with db._LOCK, db.get_conn(path) as conn:
        cur = conn.execute(
            "INSERT INTO migration_imports(name,document,mapping,created_at,updated_at) VALUES (?,?,?,?,?)",
            (name, _json(doc), _json(mapping), db._now(), db._now()),
        )
        ident = cur.lastrowid
    return get_batch(path, ident)


def _target(path, kind, value):
    value = str(value)
    if kind in ("environment", "executor", "asset_source"):
        if not value.isdecimal() or len(value) > 19 or int(value) > 2**63 - 1:
            raise ValueError("请选择目标机已登记的 " + kind)
        getter = {
            "environment": db.get_environment,
            "executor": db.get_executor,
            "asset_source": db.get_asset_source,
        }[kind]
        row = getter(path, int(value))
        if not row or not row.get("enabled", 1):
            raise ValueError("资源不存在或已停用：" + kind)
        if kind == "executor" and not cc.resolve_executor_python(row):
            raise ValueError("执行机 Python 未配置或文件不可用")
        return int(value), row
    if kind == "workdir":
        row = cc.WORKDIR_ROOTS.get(value)
        if not row or not row.get("path") or not Path(row["path"]).is_dir():
            raise ValueError("项目工作目录未配置或不可用")
    elif kind == "pytest_entry":
        row = registry.catalog("pytest").get(value)
    elif kind == "ui_suite":
        row = cc.UI_SUITES.get(value)
        if row and (
            not row.get("root") or not all((Path(row["root"]) / c).is_file() for c in row["cases"])
        ):
            row = None
    elif kind == "ui_device":
        from .ui_config import get_device

        row = get_device(value, path)
    elif kind == "runtime_requirement":
        from .runtime_requirements import get_requirement

        row = get_requirement(value, path)
    elif kind == "locust_scenario":
        row = cc.LOCUST_SCENARIOS.get(value)
        if row and not Path(row.get("path") or "__unconfigured__").is_file():
            row = None
    elif kind == "dataset":
        row = cc.resolve_dataset(value)
        if row and not all(
            Path(row.get(k) or "__unconfigured__").is_dir() for k in ("tpl_dir", "scenes_dir")
        ):
            row = None
    else:
        row = None
    if not row or not row.get("enabled", 1):
        raise ValueError("资源不可用：" + kind + "，请先在资源与环境登记并启用")
    return value, row


_CONFIG_TABLES = (
    "environments",
    "executors",
    "asset_sources",
    "projects",
    "project_resources",
    "machine_paths",
    "ui_devices",
    "runtime_requirements",
)


def _state(conn):
    config = {
        t: [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM " + t + " ORDER BY " + ("key" if t == "machine_paths" else "id")
            )
        ]
        for t in _CONFIG_TABLES
    }
    names = {
        t: [r[0] for r in conn.execute("SELECT name FROM " + t + " ORDER BY name,id")]
        for t in ("test_cases", "test_suites", "plans")
    }
    return _hash(config), names


def _prepare(path, batch, mapping, prefix):
    if not isinstance(mapping, dict) or any(
        not isinstance(v, (str, int)) or isinstance(v, bool) for v in mapping.values()
    ):
        raise ValueError("本机资源关联必须为 ID / key 映射")
    _text(prefix, "名称前缀", 40, empty=True)
    doc = batch["document"]
    slots = {r["id"]: r for r in doc["resources"]}
    if set(mapping) - set(slots):
        raise ValueError("关联中包含未知资源槽")
    required = set()

    def collect(value):
        if isinstance(value, dict):
            if set(value) == {"$resource"}:
                required.add(value["$resource"])
            else:
                for v in value.values():
                    collect(v)
        elif isinstance(value, list):
            for v in value:
                collect(v)

    collect(doc["cases"])
    collect(doc["plans"])
    for plan in doc["plans"]:
        required.update((plan["environment"], plan["executor"]))
        if plan.get("asset_source"):
            required.add(plan["asset_source"])
    errors, warnings, values, targets = [], [], {}, {}
    with db._LOCK, db.get_conn(path) as conn:
        config_before, name_state = _state(conn)
    for ident in sorted(required):
        slot = slots[ident]
        if mapping.get(ident) in (None, ""):
            errors.append("待关联：" + slot["name"] + "（" + slot["type"] + "）")
            continue
        try:
            values[ident], targets[ident] = _target(path, slot["type"], mapping[ident])
        except ValueError as exc:
            errors.append(slot["name"] + "：" + str(exc))
    prepared = dict(cases=[], suites=[], plans=[])
    used = {
        group: set(name_state[table])
        for group, table in (("cases", "test_cases"), ("suites", "test_suites"), ("plans", "plans"))
    }

    def name(group, original):
        base = (prefix + original).strip()[:120]
        current = base
        number = 2
        while current in used[group]:
            suffix = " (导入 " + str(number) + ")"
            current = base[: 120 - len(suffix)] + suffix
            number += 1
        used[group].add(current)
        return current

    def resolve(params):
        out = {}
        for key, value in params.items():
            if key in RESOURCE_FIELDS:
                if value in ("", None, 0):
                    continue
                out[key] = values[_marker(value)]
            else:
                out[key] = deepcopy(value)
        return out

    def validate_step(engine, params, label, executor=None, asset=None, environment=None):
        problems = cc.validate_step_params(engine, params, path)
        if executor:
            allowed = db._json_or_list(executor.get("allowed_runners"))
            if allowed and engine not in allowed:
                problems.append("执行机未允许 " + engine)
        if engine == "pytest" and executor:
            from .ui_config import pytest_ui_errors

            materialized = registry.pytest_params(params)
            problems += pytest_ui_errors(
                materialized, cc.resolve_pytest_workdir(materialized, executor)
            )
        if engine == "airtest":
            from .ui_config import device_binding_errors, get_device

            if executor:
                problems += device_binding_errors(
                    get_device(params.get("device"), path), executor["id"], path
                )
            if asset and (
                asset["kind"] != "airtest" or asset.get("scenario_ref") != params.get("suite")
            ):
                problems.append("UI 套件与资产源不一致")
            if (
                environment
                and environment.get("kind") == "offline"
                and cc.UI_SUITES.get(params.get("suite"), {}).get("requires_service")
            ):
                problems.append("UI 套件需要联机环境")
        errors.extend(label + "：" + str(e) for e in problems)

    if not errors:
        for row in doc["cases"]:
            p = resolve(row["params"])
            if "asset_source_id" in p:
                errors.append(row["name"] + "：用例不能绑定计划级资产源")
            validate_step(row["engine"], p, row["name"])
            case = {
                **row,
                "name": name("cases", row["name"]),
                "params": p,
                "requires_exclusive": row["engine"] == "airtest"
                or row.get("requires_exclusive", False),
                "asset_kind": row.get("asset_kind")
                or ("airtest" if row["engine"] == "airtest" else ""),
            }
            prepared["cases"].append(case)
            if not row.get("enabled", True):
                warnings.append(row["name"] + " 将保持停用，相关计划执行前需要启用")
        case_map = {r["id"]: r for r in prepared["cases"]}
        for row in doc["suites"]:
            if len(set(row["cases"])) != len(row["cases"]):
                errors.append(row["name"] + "：套件不能重复引用同一用例")
            prepared["suites"].append({**row, "name": name("suites", row["name"])})
        for row in doc["plans"]:
            steps = []
            executor = targets[row["executor"]]
            environment = targets[row["environment"]]
            asset = targets.get(row.get("asset_source"))
            for step in row["steps"]:
                if step.get("case"):
                    case = case_map[step["case"]]
                    override = resolve(step.get("override_params") or {})
                    engine = case["engine"]
                    params = {**case["params"], **override}
                    steps.append({**step, "override_params": override})
                else:
                    engine = step["engine"]
                    params = resolve(step["params"])
                    steps.append({**step, "params": params})
                try:
                    validate_step(engine, params, row["name"], executor, asset, environment)
                except ValueError as exc:
                    errors.append(row["name"] + "：" + str(exc))
            prepared["plans"].append(
                {
                    **row,
                    "name": name("plans", row["name"]),
                    "steps": steps,
                    "environment_id": values[row["environment"]],
                    "executor_id": values[row["executor"]],
                    "asset_source_id": (
                        values[row["asset_source"]] if row.get("asset_source") else None
                    ),
                }
            )
    with db._LOCK, db.get_conn(path) as conn:
        config_after, names_after = _state(conn)
    if config_before != config_after or name_state != names_after:
        errors.append("本机配置或定义正在变化，请重新预览")
    fingerprint = _hash(
        dict(
            batch=batch["id"],
            document=doc,
            mapping=mapping,
            prefix=prefix,
            prepared=prepared,
            config=config_after,
            names=names_after,
        )
    )
    return dict(
        ok=not errors,
        errors=errors,
        warnings=warnings,
        counts={k: len(doc[k]) for k in prepared},
        names={k: [dict(source=r["id"], name=r["name"]) for r in v] for k, v in prepared.items()},
        fingerprint=fingerprint,
        required_resources=sorted(required),
        no_execution=True,
        _prepared=prepared,
        _config=config_after,
        _names=names_after,
    )


@registry.scoped
@registry.locked
def preview(db_path, ident, mapping, prefix=""):
    batch = get_batch(db_path, ident)
    if batch["status"] != "pending":
        raise MigrationConflict("该批次已提交，请查看导入结果")
    result = _prepare(db_path, batch, mapping, prefix)
    with db._LOCK, db.get_conn(db_path) as conn:
        conn.execute(
            "UPDATE migration_imports SET mapping=?,prefix=?,updated_at=? WHERE id=?",
            (_json(mapping), prefix, db._now(), ident),
        )
    return {k: v for k, v in result.items() if not k.startswith("_")}


@registry.scoped
@registry.locked
def commit(db_path, ident, fingerprint):
    batch = get_batch(db_path, ident)
    if batch["status"] == "committed":
        return batch["result"]  # Idempotent retry.
    result = _prepare(db_path, batch, batch["mapping"], batch["prefix"])
    if not result["ok"]:
        raise MigrationConflict(
            "尚未完成本机资源关联或参数校验，请重新预览：" + "；".join(result["errors"])
        )
    if result["fingerprint"] != fingerprint:
        raise MigrationConflict("预览已过期，配置或关联已变化，请重新预览后提交")
    prepared = result["_prepared"]
    created = dict(cases=[], suites=[], plans=[])
    with db._LOCK, db.get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        config, names = _state(conn)
        if config != result["_config"] or names != result["_names"]:
            raise MigrationConflict("配置或名称正在变化，请重新预览")

        def insert(table, fields):
            fields.update(created_at=db._now(), updated_at=db._now())
            return conn.execute(
                "INSERT INTO "
                + table
                + "("
                + ",".join(fields)
                + ") VALUES ("
                + ",".join("?" for _ in fields)
                + ")",
                tuple(fields.values()),
            ).lastrowid

        refs = {}
        for case in prepared["cases"]:
            fields = {k: case.get(k, "") for k in ("name", "engine", "asset_kind")}
            fields.update(
                description=case.get("description", ""),
                owner=case.get("owner") or "local",
                tags=_json(case.get("tags", [])),
                enabled=int(case.get("enabled", True)),
                requires_exclusive=int(case["requires_exclusive"]),
                params=_json(case["params"]),
                revision=1,
                fingerprint=cc.test_case_fingerprint(
                    case["name"],
                    case["engine"],
                    case["params"],
                    case["requires_exclusive"],
                    case["asset_kind"],
                ),
            )
            ident_new = insert("test_cases", fields)
            refs[case["id"]] = dict(id=ident_new, revision=1, fingerprint=fields["fingerprint"])
            created["cases"].append(dict(source=case["id"], id=ident_new, name=case["name"]))
        for suite in prepared["suites"]:
            cases = [refs[r] for r in suite["cases"]]
            fields = dict(
                name=suite["name"],
                description=suite.get("description", ""),
                owner=suite.get("owner") or "local",
                tags=_json(suite.get("tags", [])),
                enabled=int(suite.get("enabled", True)),
                cases=_json(cases),
                revision=1,
                fingerprint=cc.suite_fingerprint(suite["name"], cases),
            )
            ident_new = insert("test_suites", fields)
            created["suites"].append(dict(source=suite["id"], id=ident_new, name=suite["name"]))
        for plan in prepared["plans"]:
            steps = []
            for s in plan["steps"]:
                base = {k: s[k] for k in ("name", "requires_exclusive") if k in s}
                if s.get("case"):
                    base.update(case_ref=refs[s["case"]], override_params=s["override_params"])
                else:
                    base.update(
                        engine=s["engine"],
                        params=s["params"],
                        requires_exclusive=s["engine"] == "airtest"
                        or s.get("requires_exclusive", False),
                    )
                steps.append(base)
            ident_new = insert(
                "plans",
                dict(
                    name=plan["name"],
                    description=plan.get("description", ""),
                    owner=plan.get("owner") or "local",
                    environment_id=plan["environment_id"],
                    executor_id=plan["executor_id"],
                    asset_source_id=plan["asset_source_id"],
                    steps=_json(steps),
                    fail_fast=int(plan.get("fail_fast", True)),
                    revision=1,
                    archived=int(plan.get("archived", False)),
                ),
            )
            created["plans"].append(dict(source=plan["id"], id=ident_new, name=plan["name"]))
        created.update(batch_id=ident, committed=True, no_execution=True)
        conn.execute(
            "UPDATE migration_imports SET status='committed',result=?,updated_at=? WHERE id=?",
            (_json(created), db._now(), ident),
        )
    return created
