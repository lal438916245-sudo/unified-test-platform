"""Reusable, controlled UI runtime requirements; no client supplied code or URIs."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import db

MODULES = ("requests", "psutil", "pymongo", "pytest_html", "pytest_rerunfailures")
BUILTINS = {
    "unity-common": {"name": "Unity / Poco 公共要求", "suite": "", "modules": [], "mongo_ref": ""},
    "mmo-common": {
        "name": "MMO Unity 公共要求",
        "suite": "gameautotest-ui",
        "modules": ["requests", "psutil", "pymongo"],
        "mongo_ref": "gameautotest",
    },
}


def default_key(suite):
    return "mmo-common" if suite == "gameautotest-ui" else "unity-common"


def get_requirement(key, path=None):
    if not isinstance(key, str):
        return None
    if key in BUILTINS:
        return {**BUILTINS[key], "key": key, "enabled": 1, "builtin": True, "revision": 1}
    if path and isinstance(key, str) and key.startswith("requirement-"):
        try:
            row = db.get_runtime_requirement(path, int(key.removeprefix("requirement-")))
        except ValueError:
            return None
        return {**row, "key": key, "builtin": False} if row else None


def catalog(path=None):
    rows = [get_requirement(k) for k in BUILTINS]
    if path:
        rows += [
            {**r, "key": f"requirement-{r['id']}", "builtin": False}
            for r in db.list_runtime_requirements(path)
        ]
    return rows


def payload(data, existing=None):
    out = dict(name="", suite="gameautotest-ui", modules=[], mongo_ref="", notes="", enabled=1)
    out.update({k: v for k, v in (existing or {}).items() if k in out})
    out.update(data)
    return out


def validate_record(data):
    from .ui_config import UI_SUITES

    errors = []
    if set(data) - {"name", "suite", "modules", "mongo_ref", "notes", "enabled"}:
        errors.append("运行要求只接受受控字段，不接受脚本、路径或 MongoDB URI")
    if not isinstance(data.get("name"), str) or not 1 <= len(data["name"].strip()) <= 120:
        errors.append("运行要求名称必须为 1..120 字符")
    if not isinstance(data.get("suite"), str) or data["suite"] not in UI_SUITES:
        errors.append("运行要求必须关联受控 UI 套件")
    modules = data.get("modules")
    if not isinstance(modules, list) or any(
        not isinstance(m, str) or m not in MODULES for m in modules
    ):
        errors.append("项目依赖必须从受控模块列表选择")
    if data.get("mongo_ref") not in ("", "gameautotest"):
        errors.append("MongoDB 配置来源必须是受控项目引用")
    if data.get("mongo_ref") and data.get("suite") != "gameautotest-ui":
        # Registered UI entries use the same project-owned config.py contract.
        # The suite root is resolved by the ordinary registration catalog.
        suite = UI_SUITES.get(data.get("suite"), {})
        root = suite.get("root")
        if not str(data.get("suite", "")).startswith("entry-") or not root or not (Path(root) / "config.py").is_file():
            errors.append("MongoDB 配置需要关联带 config.py 的已登记 UI 项目")
    if not isinstance(data.get("notes", ""), str) or len(data.get("notes", "")) > 1000:
        errors.append("备注最多 1000 字符")
    if type(data.get("enabled")) not in (bool, int) or data.get("enabled") not in (0, 1):
        errors.append("enabled 必须为 0 或 1")
    return errors


def requirement_errors(params, path=None):
    if "requirements" in params and not isinstance(params["requirements"], str):
        return ["公共运行要求必须为受控引用"]
    key = params.get("requirements") or default_key(params.get("suite"))
    if not isinstance(key, str):
        return ["公共运行要求必须为受控引用"]
    row = get_requirement(key, path)
    if not row or not row.get("enabled"):
        return ["公共运行要求不存在或已停用"]
    if row.get("suite") and row["suite"] != params.get("suite"):
        return ["公共运行要求与 UI 套件不相容"]
    return []


def resolve_requirement(params, path=None):
    if requirement_errors(params, path):
        raise ValueError("公共运行要求不存在、停用或与套件不相容")
    key = params.get("requirements") or default_key(params.get("suite"))
    row = get_requirement(key, path)
    # A custom profile can add checks, but cannot bypass the suite's declared minimum.
    base = BUILTINS[default_key(params.get("suite"))]
    out = {
        "key": key,
        "name": row["name"],
        "suite": params.get("suite"),
        "modules": sorted(set(base["modules"] + row["modules"])),
        "mongo_ref": base["mongo_ref"] or row["mongo_ref"],
        "enabled": row["enabled"],
    }
    semantic = {k: v for k, v in out.items() if k != "name"}
    out["fingerprint"] = hashlib.sha256(json.dumps(semantic, sort_keys=True).encode()).hexdigest()[
        :12
    ]
    return out


def mongo_environment_fingerprint():
    # Credentials never enter a response or snapshot, but changes invalidate retries.
    values = [
        os.environ.get(k, "") for k in ("MONGO_URI", "MONGO_DB_NAME", "MONGO_USER_COLLECTION")
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:12]
