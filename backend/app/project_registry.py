"""Per-database project registration. Paths are accepted only by local config APIs.

Ordinary test/plan inputs continue to carry controlled references. Context-local
catalogs keep isolated platform instances separate without mutating built-ins.
"""

import ast
import functools
import hashlib
import inspect
import json
import os
import re
import threading
from copy import deepcopy
from collections.abc import MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PureWindowsPath

from . import db

_database = ContextVar("project_registry_database", default=None)
_request_snapshot = ContextVar("project_registry_snapshot", default=None)
bindings_lock = threading.RLock()
KINDS = ("runtime", "pytest", "airtest", "locust", "matcheval")


def locked(function):
    """Serialize reference validation/writes with project deletion and launch."""

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with bindings_lock:
            return function(*args, **kwargs)

    # FastAPI must resolve future annotations in the original module's globals.
    wrapped.__signature__ = inspect.signature(function, eval_str=True)
    return wrapped


@contextmanager
def registry_scope(path):
    path = str(path) if path else None
    token = _database.set(path)
    state = _request_snapshot.get()
    key = os.path.normcase(os.path.abspath(path)) if path else None
    snapshot_token = None
    if state is None or state["path"] != key:
        snapshot_token = _request_snapshot.set({"path": key, "values": {}})
    try:
        yield
    finally:
        if snapshot_token is not None:
            _request_snapshot.reset(snapshot_token)
        _database.reset(token)


def request_cached(key, load, path=None):
    """Reuse work only inside this request/scope, including its nested calls."""
    state = _request_snapshot.get()
    path = path or _database.get()
    if state is None or not path or state["path"] != os.path.normcase(os.path.abspath(path)):
        return load()
    if key not in state["values"]:
        state["values"][key] = load()
    return state["values"][key]


def invalidate_snapshot(path):
    """Called after a successful DB write, before any subsequent read."""
    state = _request_snapshot.get()
    if state is not None and path and state["path"] == os.path.normcase(os.path.abspath(path)):
        state["values"].clear()


def scoped(function):
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind_partial(*args, **kwargs).arguments
        path = bound.get("db_path") or bound.get("path") or _database.get()
        with registry_scope(path):
            return function(*args, **kwargs)

    return wrapped


def initialize(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
            root_path TEXT NOT NULL, notes TEXT DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
            revision INTEGER NOT NULL DEFAULT 1, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS project_resources (
            id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL DEFAULT 0,
            kind TEXT NOT NULL, name TEXT NOT NULL UNIQUE, config TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT, updated_at TEXT);
    """
    )


def records(table, path=None):
    assert table in ("projects", "project_resources")
    path = path or _database.get()
    if not path:
        return []
    return deepcopy(request_cached(("records", table), lambda: _records(table, path), path))


def _records(table, path):
    with db._LOCK, db.get_conn(path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM " + table + " ORDER BY id")]
    for row in rows:
        if table == "project_resources":
            row["config"] = json.loads(row["config"])
        row["key"] = (
            "project-"
            if table == "projects"
            else "runtime-" if row["kind"] == "runtime" else "entry-"
        ) + str(row["id"])
    return rows


def find(ref, path=None):
    table = "projects" if str(ref).startswith("project-") else "project_resources"
    return next((r for r in records(table, path) if r["key"] == ref), None)


def save(table, payload, ident=None):
    path = _database.get()
    fields = dict(payload)
    if table == "project_resources":
        fields["config"] = json.dumps(fields["config"], ensure_ascii=False, sort_keys=True)
    with bindings_lock, db._LOCK, db.get_conn(path) as conn:
        if (
            table == "project_resources"
            and payload.get("project_id")
            and not conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (payload["project_id"],)
            ).fetchone()
        ):
            raise ValueError("所属项目已删除，请刷新后重新选择")
        if ident:
            conn.execute(
                "UPDATE "
                + table
                + " SET "
                + ",".join(k + "=?" for k in fields)
                + ",revision=revision+1,updated_at=? WHERE id=?",
                (*fields.values(), db._now(), ident),
            )
        else:
            cur = conn.execute(
                "INSERT INTO "
                + table
                + "("
                + ",".join(fields)
                + ",created_at,updated_at) VALUES ("
                + ",".join("?" for _ in range(len(fields) + 2))
                + ")",
                (*fields.values(), db._now(), db._now()),
            )
            ident = cur.lastrowid
    return next(r for r in records(table) if r["id"] == ident)


def local_absolute(value, directory=False):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("请填写本机绝对路径")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("必须填写本机绝对路径")
    path = path.resolve()
    if directory and not path.is_dir():
        raise ValueError("目录不存在，请确认路径属于平台所在电脑")
    if not directory and not path.is_file():
        raise ValueError("文件不存在，请确认路径属于平台所在电脑")
    return str(path)


def contained(root, relative, directory=False, selector=False):
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise ValueError("请填写项目内的相对路径")
    name, *nodes = relative.split("::") if selector else [relative]
    win = PureWindowsPath(name)
    if (
        win.drive
        or win.root
        or Path(name).is_absolute()
        or "\\" in name
        or any(part in ("..", "") for part in name.split("/"))
        or any(c in name for c in ":;|&$`<>")
    ):
        raise ValueError("仅允许项目内相对路径（正斜杠），禁止越界和命令片段")
    if nodes and any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) for n in nodes):
        raise ValueError("用例选择器只支持文件、类名和函数名")
    base = Path(root).resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base):
        raise ValueError("路径或符号链接超出登记项目目录")
    if not (target.is_dir() if directory else target.is_file()):
        raise ValueError("登记的相对目录或文件不存在")
    return str(target)


def validate_project(data, existing=None):
    if set(data) - {"name", "root_path", "notes", "enabled"}:
        raise ValueError("项目包含未支持的字段")
    out = dict(name="", root_path="", notes="", enabled=1)
    out.update(data)
    validate_common(out)
    if not (existing and not out["enabled"] and out["root_path"] == existing["root_path"]):
        out["root_path"] = local_absolute(out["root_path"], directory=True)
    return out


def validate_common(out):
    if not isinstance(out["name"], str) or not 1 <= len(out["name"].strip()) <= 120:
        raise ValueError("名称必须为 1..120 字符")
    out["name"] = out["name"].strip()
    if type(out["enabled"]) not in (bool, int) or out["enabled"] not in (0, 1):
        raise ValueError("启用状态必须为 0 或 1")
    if "notes" in out and (not isinstance(out["notes"], str) or len(out["notes"]) > 2000):
        raise ValueError("备注最多 2000 字符")


def validate_resource(data, existing=None):
    if set(data) - {"project_id", "kind", "name", "config", "enabled"}:
        raise ValueError("入口或运行环境包含未支持的字段")
    out = dict(project_id=0, kind="", name="", config={}, enabled=1)
    out.update(data)
    validate_common(out)
    kind, config = out["kind"], out["config"]
    if kind not in KINDS or not isinstance(config, dict):
        raise ValueError("请选择支持的引擎 / 运行环境并填写配置")
    if existing and (existing["kind"] != kind or existing["project_id"] != out["project_id"]):
        raise ValueError("已登记条目的类型与所属项目不能修改，请新增条目")
    if existing and not out["enabled"] and config == existing["config"]:
        return out  # Allow disabling a missing file / unavailable interpreter.
    if kind == "runtime":
        if set(config) - {"python_path", "modules"} or out["project_id"] != 0:
            raise ValueError("运行环境只接受 Python 路径与模块名，所属项目为 0")
        config["python_path"] = local_absolute(config.get("python_path"))
        if not re.fullmatch(
            r"python(?:\d+(?:\.\d+)*)?w?(?:\.exe)?", Path(config["python_path"]).name, re.I
        ):
            raise ValueError("请选择 Python 解释器文件")
        modules = config.setdefault("modules", [])
        if (
            not isinstance(modules, list)
            or len(modules) > 30
            or any(
                not isinstance(m, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", m)
                for m in modules
            )
        ):
            raise ValueError("依赖须为最多 30 个 Python 模块名，不接受安装命令")
        return out
    if type(out["project_id"]) is not int:
        raise ValueError("请选择已登记项目")
    project = find("project-" + str(out["project_id"]))
    if not project:
        raise ValueError("所属项目不存在")
    allowed = (
        {"path"}
        if kind in ("pytest", "locust")
        else (
            {"path", "requires_service"}
            if kind == "airtest"
            else {"tpl_dir", "scenes_dir", "script", "ground_truth", "airtest_source"}
        )
    )
    if set(config) - allowed:
        raise ValueError("入口或资产包含未支持字段")
    if kind == "matcheval":
        for key in ("tpl_dir", "scenes_dir"):
            visual_path(project["root_path"], config.get(key), directory=True)
        visual_path(project["root_path"], config.get("script"))
        if config.get("airtest_source"):
            source = visual_path(project["root_path"], config["airtest_source"], directory=True)
            if not (Path(source) / "airtest/__init__.py").is_file():
                raise ValueError("Airtest 源码根目录必须包含 airtest/__init__.py")
        if not str(config["script"]).endswith(".py"):
            raise ValueError("评估脚本必须是 Python 文件，遵循 match_eval CLI / CSV 约定")
        if config.setdefault("ground_truth", "fixture-filename") != "fixture-filename":
            raise ValueError("本轮支持模板/场景文件名真值约定 fixture-filename")
    else:
        target = contained(project["root_path"], config.get("path"), selector=kind == "pytest")
        if not target.endswith(".py"):
            raise ValueError("测试入口必须是 Python 文件")
        if kind == "pytest" and "::" in config["path"]:
            try:
                body = ast.parse(Path(target).read_text(encoding="utf-8-sig")).body
                for name in config["path"].split("::")[1:]:
                    node = next(
                        n
                        for n in body
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and n.name == name
                    )
                    body = node.body
            except (StopIteration, SyntaxError, UnicodeError):
                raise ValueError("文件中的类 / 函数选择器不存在或无法静态解析") from None
        if kind == "airtest" and type(config.setdefault("requires_service", False)) is not bool:
            raise ValueError("需要被测服务必须为布尔值")
    return out


def visual_path(root, value, directory=False):
    # Only the local registration API accepts absolute asset/engine locations.
    # Ordinary case/plan parameters still accept controlled references exclusively.
    if isinstance(value, str) and Path(value).is_absolute():
        return local_absolute(value, directory=directory)
    return contained(root, value, directory=directory)


def catalog(category):
    return deepcopy(request_cached(("catalog", category), lambda: _catalog(category)))


def _catalog(category):
    projects = {
        p["id"]: p for p in records("projects") if p["enabled"] and Path(p["root_path"]).is_dir()
    }
    if category == "workdirs":
        return {p["key"]: dict(label=p["name"], path=p["root_path"]) for p in projects.values()}
    out = {}
    for row in records("project_resources"):
        if not row["enabled"]:
            continue
        key, kind, c = row["key"], row["kind"], row["config"]
        categories = {
            "runtime": ("interpreters",), "pytest": ("pytest",),
            "airtest": ("ui",), "locust": ("locust",),
            "matcheval": ("datasets", "templates", "scenes"),
        }
        if category not in categories.get(kind, ()):
            continue
        if kind == "runtime":
            if category == "interpreters":
                out[key] = dict(
                    label=row["name"], path=c["python_path"], modules=c.get("modules", [])
                )
            continue
        project = projects.get(row["project_id"])
        if not project:
            continue
        root = project["root_path"]
        try:
            if kind in ("pytest", "airtest", "locust"):
                file = contained(root, c["path"], selector=kind == "pytest")
                if category == "ui" and kind == "airtest":
                    out[key] = dict(
                        label=row["name"],
                        root=root,
                        cases=[c["path"]],
                        driver="unity",
                        requires_service=c.get("requires_service", False),
                    )
                elif category == "locust" and kind == "locust":
                    out[key] = dict(label=row["name"], path=file)
                elif category == "pytest" and kind == "pytest":
                    out[key] = dict(label=row["name"], root_key=project["key"], selector=c["path"])
            elif kind == "matcheval":
                tpl = visual_path(root, c["tpl_dir"], directory=True)
                scenes = visual_path(root, c["scenes_dir"], directory=True)
                script = visual_path(root, c["script"])
                source = (
                    visual_path(root, c["airtest_source"], directory=True)
                    if c.get("airtest_source")
                    else ""
                )
                if category == "datasets":
                    out[key] = dict(
                        label=row["name"],
                        tpl_dir=key + "-tpl",
                        scenes_dir=key + "-scenes",
                        ground_truth=c["ground_truth"],
                        script=script,
                        airtest_source=source,
                    )
                elif category == "templates":
                    out[key + "-tpl"] = dict(label=row["name"] + " · 模板", path=tpl)
                elif category == "scenes":
                    out[key + "-scenes"] = dict(label=row["name"] + " · 场景", path=scenes)
        except (ValueError, OSError):
            # A moved file, symlink escape or unavailable project invalidates its ref.
            continue
    return out


class RegisteredMap(MutableMapping):
    def __init__(self, builtin, category):
        self.builtin, self.category = builtin, category

    def __getitem__(self, key):
        if key in self.builtin:
            from .machine_paths import overlay

            return overlay(self.category, key, self.builtin[key])
        return catalog(self.category)[key]

    def __setitem__(self, key, value):
        self.builtin[key] = value
        invalidate_snapshot(_database.get())

    def __delitem__(self, key):
        del self.builtin[key]
        invalidate_snapshot(_database.get())

    def _resolved(self):
        from .machine_paths import overlay

        return {**{k: overlay(self.category, k, v) for k, v in self.builtin.items()},
                **catalog(self.category)}

    def items(self):
        return self._resolved().items()

    def values(self):
        return self._resolved().values()

    def __iter__(self):
        return iter({**self.builtin, **catalog(self.category)})

    def __len__(self):
        return len(set(self.builtin) | set(catalog(self.category)))


def pytest_params(params):
    if "entry" in params and not isinstance(params["entry"], str):
        raise ValueError("pytest 入口必须是登记后的引用标识")
    entry = params.get("entry")
    if not entry:
        return dict(params)
    spec = catalog("pytest").get(entry)
    if not spec:
        raise ValueError("pytest 入口不存在、已停用或文件已移动")
    args = [spec["selector"], "-o", "addopts=", "--tb=short"]
    if (
        params.get("args") not in (None, args)
        or params.get("cwd_root") not in (None, spec["root_key"])
        or params.get("cwd")
    ):
        raise ValueError("已登记入口的工作目录与选择器必须保持一致；自定义选择器请取消入口引用")
    return {**params, "args": args, "cwd_root": spec["root_key"]}


def snapshot(params, executor):
    refs = {
        str(params.get(k) or "") for k in ("entry", "cwd_root", "suite", "locustfile", "dataset")
    }
    refs |= {str(executor.get("python_ref") or ""), str(executor.get("cwd_root") or "")}
    rows = []
    for ref in sorted(refs):
        if not re.fullmatch(r"(project|entry|runtime)-\d+", ref):
            continue
        row = find(ref)
        rows.append((ref, row))
        if row and row.get("project_id"):
            parent = find("project-" + str(row["project_id"]))
            rows.append(("project-" + str(row["project_id"]), parent))
    from .machine_paths import snapshot as path_snapshot

    return {
        **{
            ref: {
                "revision": (row or {}).get("revision"),
                "fingerprint": hashlib.sha256(
                    json.dumps(row, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()[:16],
            }
            for ref, row in rows
        },
        **path_snapshot(params, executor),
    }


def redact(value):
    if not isinstance(value, str) or not _database.get():
        return value
    paths = [r["root_path"] for r in records("projects")]
    paths += [
        str(Path(r["config"]["python_path"]).parent)
        for r in records("project_resources")
        if r["kind"] == "runtime"
    ]
    from .machine_paths import records as path_records

    paths += [r["path"] for r in path_records() if r["path"]]
    paths += [
        v
        for r in records("project_resources")
        if r["kind"] == "matcheval"
        for v in r["config"].values()
        if isinstance(v, str) and Path(v).is_absolute()
    ]
    for path in sorted(paths, key=len, reverse=True):
        for variant in (path, path.replace("\\", "/"), path.replace("\\", "\\\\")):
            value = value.replace(variant, "[project]")
    return value
