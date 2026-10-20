"""Registration deletion and reference inspection. Never touches files."""

import json
import os

from . import db, project_registry as registry

_REF_FIELDS = {
    "cwd_root",
    "cwd",
    "python_ref",
    "entry",
    "suite",
    "locustfile",
    "dataset",
    "dataset_ref",
    "tpl_ref",
    "scenes_ref",
    "scenario_ref",
    "airtest_ref",
    "key",
}


def _json(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def _uses(value, refs):
    if isinstance(value, str):
        parsed = _json(value)
        # Fail closed if a damaged snapshot still names one of these references.
        if parsed is None:
            return any(json.dumps(ref) in value for ref in refs)
        return _uses(parsed, refs) if not isinstance(parsed, str) else False
    if isinstance(value, list):
        return any(_uses(v, refs) for v in value)
    if isinstance(value, dict):
        return any(
            k in refs
            or (
                k in _REF_FIELDS
                and isinstance(v, str)
                and (
                    v in refs
                    or (
                        v.replace("\\", "/").rstrip("/").casefold()
                        if os.name == "nt"
                        else v.rstrip("/")
                    )
                    in refs
                )
            )
            or (isinstance(v, (dict, list)) and _uses(v, refs))
            for k, v in value.items()
        )
    return False


def _case_uses(steps, ids):
    return any(
        isinstance(s, dict)
        and isinstance(s.get("case_ref"), dict)
        and s["case_ref"].get("id") in ids
        for s in (_json(steps) or [])
    )


def _inspect(conn, ident, resource=False):
    if resource:
        row = conn.execute(
            "SELECT id,name,enabled,kind FROM project_resources WHERE id=?", (ident,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id,name,enabled,root_path FROM projects WHERE id=?", (ident,)
        ).fetchone()
    if not row:
        raise LookupError("入口 / 运行环境登记不存在" if resource else "项目登记不存在")
    target, found = dict(row), []
    runtime = resource and target["kind"] == "runtime"
    subject = ("运行环境" if runtime else "测试入口 / 资产") if resource else "项目"
    target["key"] = ("runtime-" if runtime else "entry-" if resource else "project-") + str(ident)
    refs = {target["key"]}
    if resource and not runtime:
        refs.update((target["key"] + "-tpl", target["key"] + "-scenes"))
    if not resource:
        # Historical definitions may contain a materialized cwd instead of its key.
        root = target.pop("root_path")
        normalized = (
            root.replace("\\", "/").rstrip("/").casefold() if os.name == "nt" else root.rstrip("/")
        )
        refs.update(r for r in (root, normalized) if r)

    def rows(table):
        return [dict(r) for r in conn.execute("SELECT * FROM " + table + " ORDER BY id")]

    def add(kind, row, reason):
        state = "已归档" if row.get("archived") else ("启用" if row.get("enabled", 1) else "停用")
        if kind in ("运行记录", "执行任务", "历史报告"):
            state = row.get("status") or "历史记录"
        found.append(
            dict(
                type=kind,
                id=row["id"],
                name=row.get("name") or kind + " #" + str(row["id"]),
                state=state,
                reason=reason,
            )
        )

    for row in rows("project_resources") if not resource else []:
        if row["project_id"] == ident:
            key = ("runtime-" if row["kind"] == "runtime" else "entry-") + str(row["id"])
            refs.update((key, key + "-tpl", key + "-scenes"))
            add("测试入口 / 资产", row, "所属项目为 " + target["key"])

    executors, assets, cases = set(), set(), set()
    for row in rows("executors"):
        fields = (
            {"python_ref": row.get("python_ref")}
            if runtime
            else {"cwd_root": row.get("cwd_root"), "cwd": row.get("cwd")}
        )
        if _uses(fields, refs):
            executors.add(row["id"])
            add("执行机", row, "Python 引用该运行环境" if runtime else "默认工作目录引用该项目")
    for row in rows("asset_sources"):
        if _uses(row, refs):
            assets.add(row["id"])
            add("资产源", row, "引用该" + subject + "的入口或资产")
    for row in rows("test_cases"):
        if _uses(row.get("params"), refs):
            cases.add(row["id"])
            add("测试用例", row, "参数引用该" + subject)
    for row in rows("test_suites"):
        if any(c.get("id") in cases for c in (_json(row["cases"]) or []) if isinstance(c, dict)):
            add("测试套件", row, "包含引用该" + subject + "的测试用例")
    for row in rows("plans"):
        if (
            row.get("executor_id") in executors
            or row.get("asset_source_id") in assets
            or _uses(row.get("steps"), refs)
            or _case_uses(row.get("steps"), cases)
        ):
            add("测试计划", row, "步骤、用例、执行机或资产源引用该" + subject)
    for row in rows("runtime_requirements"):
        if row.get("suite") in refs:
            add("公共运行要求", row, "绑定该" + subject + "的 UI 测试入口")

    job_ids, run_ids = set(), set()
    for row in rows("jobs"):
        if (
            _uses(row.get("params"), refs)
            or _uses(row.get("config_snapshot"), refs)
            or (not row.get("config_snapshot") and row.get("executor_id") in executors)
        ):
            job_ids.add(row["id"])
            run_ids.add(row["plan_run_id"])
            add("执行任务", row, "任务参数、执行机或固化配置引用该" + subject)
    for row in rows("plan_runs"):
        if row["id"] in run_ids or _uses(row.get("plan_snapshot"), refs):
            snapshot = _json(row.get("plan_snapshot")) or {}
            add(
                "运行记录",
                {**row, "name": snapshot.get("name")},
                "历史计划快照或执行任务引用该" + subject,
            )
    for row in rows("reports"):
        if row.get("job_id") in job_ids or _uses(row.get("report_json"), refs):
            add("历史报告", row, "报告或其执行任务保留该" + subject + "引用")

    return {
        ("resource" if resource else "project"): target,
        "can_delete": not found,
        "references": found,
        "message": (
            ("已有引用，只能停用" + subject + "；停用后引用记录和历史报告保留。")
            if found
            else (subject + "未被引用，可以删除登记记录；本机目录和文件不会删除。")
        ),
    }


def inspect(ident, resource=False):
    with db._LOCK, db.get_conn(registry._database.get()) as conn:
        return _inspect(conn, ident, resource)


def delete(ident, resource=False):
    # The check and deletion share a write transaction. Normal reference-creating
    # API mutations also hold bindings_lock across validation and persistence.
    with registry.bindings_lock, db._LOCK, db.get_conn(registry._database.get()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result = _inspect(conn, ident, resource)
        if result["can_delete"]:
            table = "project_resources" if resource else "projects"
            conn.execute("DELETE FROM " + table + " WHERE id=?", (ident,))
        return result
