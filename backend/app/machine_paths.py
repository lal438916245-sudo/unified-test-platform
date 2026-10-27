"""Persistent, per-installation bindings for legacy controlled path references.

Package fixtures remain relative to the installed platform. External locations are
discovered once when available, then owned by the local configuration database.
"""

import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from . import db

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent


def venv_python(name):
    return str(ROOT / name / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))


DEFINITIONS = {
    "python.anaconda": ("Python", "默认 Python（兼容 anaconda 引用）", "python"),
    "python.airtest-venv": ("Python", "Airtest / Poco Python", "python"),
    "python.locust-venv": ("Python", "Locust Python", "python"),
    "python.matcheval": ("Python", "视觉评估 Python", "python"),
    "python.trae-py": ("Python", "旧工具 Python（可不配置）", "python"),
    "workdir.gameautotest": ("项目", "GameAutoTest-Pro 项目根", "directory"),
    "workdir.privahigh-backend": ("项目", "PrivaHigh 后端项目根", "directory"),
    "scenario.colyseus-bot": ("外部引擎", "Colyseus-Storm Locust 脚本", "script"),
    "script.matcheval": ("外部引擎", "兼容 match_eval 的视觉评估脚本", "script"),
    "asset.airtest-master": ("外部引擎", "Airtest 源码根（含 airtest/，可不配置）", "directory"),
    "asset.privahigh-real-tpl": ("资产", "PrivaHigh 模板目录", "directory"),
    "asset.privahigh-real-scenes": ("资产", "PrivaHigh 场景目录", "directory"),
}


def defaults():
    env = os.environ
    sdet = env.get("PLATFORM_SDET_PROJ", "")
    return {
        "python.anaconda": env.get("PLATFORM_PYTHON")
        or env.get("PLATFORM_DEFAULT_PYTHON")
        or sys.executable,
        "python.airtest-venv": env.get("PLATFORM_UI_PYTHON") or venv_python(".venv-ui"),
        "python.locust-venv": env.get("PLATFORM_LOCUST_PYTHON") or venv_python(".venv-locust"),
        "python.matcheval": env.get("PLATFORM_MATCHEVAL_PYTHON") or sys.executable,
        "python.trae-py": "",
        "workdir.gameautotest": str(WORKSPACE / "GameAutoTest-Pro"),
        "workdir.privahigh-backend": str(Path(sdet) / "PrivaHigh/backend") if sdet else "",
        "scenario.colyseus-bot": str(WORKSPACE / "Colyseus-Storm/locustfile.py"),
        "script.matcheval": env.get("PLATFORM_MATCHEVAL_SCRIPT")
        or str(WORKSPACE / "match_eval.py"),
        "asset.airtest-master": env.get("PLATFORM_AIRTEXT_DIR")
        or (str(Path(sdet) / "Airtest-master") if sdet else ""),
        "asset.privahigh-real-tpl": str(WORKSPACE / "real_assets/privahigh/tpl"),
        "asset.privahigh-real-scenes": str(WORKSPACE / "real_assets/privahigh/scenes"),
    }


def initialize(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS machine_paths (
        key TEXT PRIMARY KEY, path TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT)"""
    )
    candidates = defaults()
    # Preserve existing installations without rewriting any old configuration row.
    for row in conn.execute(
        "SELECT python_ref,python_executable,cwd_root,cwd FROM executors ORDER BY id"
    ):
        for key, value in (("python." + str(row[0]), row[1]), ("workdir." + str(row[2]), row[3])):
            if key in DEFINITIONS and value and Path(value).exists():
                candidates[key] = value
    # A legacy external script may declare its source checkout. Read syntax only;
    # never import or edit that script while migrating configuration.
    script = candidates["script.matcheval"]
    if script and Path(script).is_file() and not candidates["asset.airtest-master"]:
        try:
            tree = ast.parse(Path(script).read_text(encoding="utf-8-sig"))
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "AIRTEST_SRC" for t in node.targets
                ):
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        source = Path(node.value.value)
                        if (source / "airtest").is_dir():
                            candidates["asset.airtest-master"] = str(source)
                            backend = source.parent / "PrivaHigh/backend"
                            if not candidates["workdir.privahigh-backend"] and backend.is_dir():
                                candidates["workdir.privahigh-backend"] = str(backend)
        except (OSError, SyntaxError, UnicodeError):
            pass
    for key in DEFINITIONS:
        value = candidates.get(key, "")
        if value and not Path(value).exists():
            value = ""
        conn.execute(
            "INSERT OR IGNORE INTO machine_paths(key,path,updated_at) VALUES (?,?,?)",
            (key, value, db._now()),
        )


def records():
    from .project_registry import _database, request_cached

    path = _database.get()
    if not path:
        return []
    return [dict(r) for r in request_cached(("machine_paths",), lambda: _records(path))]


def _records(path):
    with db._LOCK, db.get_conn(path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM machine_paths ORDER BY key")]


def resolve(key, fallback=""):
    row = next((r for r in records() if r["key"] == key), None)
    return row["path"] if row is not None else fallback


def public_records():
    rows = {r["key"]: r for r in records()}
    out = []
    for key, (group, label, kind) in DEFINITIONS.items():
        row = rows.get(key, dict(key=key, path="", revision=0, updated_at=None))
        p = row["path"]
        ok = bool(p) and (Path(p).is_dir() if kind == "directory" else Path(p).is_file())
        out.append({**row, "group": group, "label": label, "kind": kind, "available": ok})
    return out


def save(key, data):
    from .project_registry import _database, bindings_lock, local_absolute

    if key not in DEFINITIONS or set(data) != {"path"} or not isinstance(data["path"], str):
        raise ValueError("请选择已登记的路径项，只填写 path 字段")
    value = data["path"].strip()
    kind = DEFINITIONS[key][2]
    if value:
        value = local_absolute(value, directory=kind == "directory")
        if kind == "python" and not re.fullmatch(
            r"python(?:\d+(?:\.\d+)*)?w?(?:\.exe)?", Path(value).name, re.I
        ):
            raise ValueError("请选择 Python 解释器文件")
        if kind == "script" and Path(value).suffix.lower() != ".py":
            raise ValueError("请选择 Python 引擎脚本")
        if key == "asset.airtest-master" and not (Path(value) / "airtest/__init__.py").is_file():
            raise ValueError("Airtest 源码根目录必须包含 airtest/__init__.py")
    with bindings_lock, db._LOCK, db.get_conn(_database.get()) as conn:
        conn.execute(
            "UPDATE machine_paths SET path=?,revision=revision+1,updated_at=? WHERE key=?",
            (value, db._now(), key),
        )
    return next(r for r in public_records() if r["key"] == key)


def overlay(category, key, spec):
    """Resolve legacy catalogs using the same bindings consumed by runners."""
    prefix = {
        "workdirs": "workdir.",
        "locust": "scenario.",
        "templates": "asset.",
        "scenes": "asset.",
        "airtest_dirs": "asset.",
    }.get(category)
    binding = (prefix + key) if prefix else ""
    if category == "ui" and key == "gameautotest-ui":
        return {**spec, "root": resolve("workdir.gameautotest", spec.get("root", ""))}
    if binding in DEFINITIONS:
        return {**spec, "path": resolve(binding, spec.get("path", ""))}
    return spec


def snapshot(params, executor):
    keys = set()
    for ref in (params.get("cwd_root"), executor.get("cwd_root")):
        keys.add("workdir." + str(ref or ""))
    keys.add("python." + str(executor.get("python_ref") or ""))
    keys.add("scenario." + str(params.get("locustfile") or ""))
    if params.get("suite") == "gameautotest-ui":
        keys.add("workdir.gameautotest")
    if params.get("dataset"):
        if not str(params["dataset"]).startswith("entry-"):
            keys |= {"script.matcheval", "asset.airtest-master"}
        if params["dataset"] == "privahigh-real":
            keys |= {"asset.privahigh-real-tpl", "asset.privahigh-real-scenes"}
    return {
        "path:"
        + r["key"]: dict(
            revision=r["revision"],
            fingerprint=hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()[:16],
        )
        for r in records()
        if r["key"] in keys
    }
