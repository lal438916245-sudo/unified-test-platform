"""Server-owned UI suite/device registry. No client supplied paths or commands."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path

from .project_registry import RegisteredMap, scoped

BACKEND = Path(__file__).resolve().parents[1]
WORKSPACE = BACKEND.parents[1]
UI_SUITES = {
    "colyseus-ui-smoke": {
        "label": "Colyseus MMO：Begin → 注册入口（不注册）",
        "root": str(BACKEND / "demo" / "ui"),
        "cases": ["test_colyseus_ui.py"],
        "driver": "unity",
        "dedicated": True,
        "requires_service": True,
    },
    "unity-smoke": {
        "label": "Unity 控件连通冒烟（需真实客户端）",
        "root": str(BACKEND / "demo" / "ui"),
        "cases": ["test_unity_smoke.py"],
        "driver": "unity",
        "dedicated": True,
    },
    "gameautotest-ui": {
        "label": "GameAutoTest UI（需服务端、测试账号与 MongoDB）",
        "root": str(WORKSPACE / "GameAutoTest-Pro"),
        "cases": ["test_cases/test_mmo_loop.py", "test_cases/test_full_link_loop.py"],
        "driver": "unity",
        "requires_service": True,
    },
}
UI_SUITES = RegisteredMap(UI_SUITES, "ui")
# Missing target is deliberate: never attach to an arbitrary foreground window.
UI_DEVICES = {
    "unity-windows": {
        "label": "Windows Unity（管理员登记窗口后可用）",
        "driver": "unity",
        "uri": os.environ.get("PLATFORM_UI_DEVICE_URI", ""),
        "host": os.environ.get("PLATFORM_UI_POCO_HOST", "127.0.0.1"),
        "port": os.environ.get("PLATFORM_UI_POCO_PORT", "5001"),
        "unity_editor": os.environ.get("PLATFORM_UI_UNITY_EDITOR", "0") == "1",
        "smoke_selector": os.environ.get("PLATFORM_UI_SMOKE_SELECTOR", ""),
        "smoke_result": os.environ.get("PLATFORM_UI_SMOKE_RESULT", ""),
    },
}


def get_device(ref, db_path=None):
    if str(ref).startswith("ui-device-"):
        if not db_path:
            return None
        from . import db

        try:
            row = db.get_ui_device(db_path, int(str(ref)[10:]))
        except (TypeError, ValueError):
            return None
        return {**row, "driver": "unity", "label": row["name"], "key": ref} if row else None
    return UI_DEVICES.get(ref)


def device_catalog(db_path=None):
    if not db_path:
        return [{"key": k, "label": d["label"]} for k, d in UI_DEVICES.items()]
    from . import db

    rows = [
        {
            "key": f"ui-device-{d['id']}",
            "label": d["name"],
            "executor_id": d["executor_id"],
            "enabled": d["enabled"],
        }
        for d in db.list_ui_devices(db_path)
    ]
    rows += [
        {"key": k, "label": d["label"] + "（旧环境变量）"}
        for k, d in UI_DEVICES.items()
        if d.get("uri")
    ]
    return rows


def device_fingerprint(ref, db_path=None, device=None):
    d = device if device is not None else (get_device(ref, db_path) or {})
    if "title_match" in d:
        d = {
            k: d.get(k)
            for k in (
                "executor_id",
                "title_match",
                "match_mode",
                "process_name",
                "window_class",
                "unity_editor",
                "host",
                "port",
                "smoke_selector",
                "smoke_result",
                "enabled",
            )
        }
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]


def validate_device_input(data):
    errors = []
    allowed = {
        "name",
        "executor_id",
        "title_match",
        "match_mode",
        "process_name",
        "window_class",
        "unity_editor",
        "host",
        "port",
        "smoke_selector",
        "smoke_result",
        "notes",
        "enabled",
    }
    if set(data) - allowed:
        errors.append("UI 设备不接受未声明字段、设备 URI 或固定窗口句柄")
    for k in ("name", "title_match"):
        if not isinstance(data.get(k), str) or not data[k].strip() or len(data[k]) > 256:
            errors.append(f"{k} 必须为 1..256 字符的非空文本")
    if data.get("match_mode", "contains") not in ("contains", "exact"):
        errors.append("窗口匹配方式只能是 contains / exact")
    for k in ("process_name", "window_class", "smoke_selector", "smoke_result", "notes"):
        if k in data and (not isinstance(data[k], str) or len(data[k]) > 512):
            errors.append(f"{k} 必须是最多 512 字符的文本")
    if any(c in str(data.get("process_name", "")) for c in ("/", "\\", ":")):
        errors.append("进程名称只能填写文件名，例如 Unity.exe")
    for k in ("enabled", "unity_editor"):
        if k in data and (not isinstance(data[k], (int, bool)) or data[k] not in (0, 1)):
            errors.append(f"{k} 必须为 0 或 1")
    for k, lo, hi in (("port", 1, 65535), ("executor_id", 1, 2147483647)):
        value = data.get(k, 5001 if k == "port" else 0)
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            errors.append(f"{k} 必须为 {lo}..{hi} 的整数")
    try:
        if not ipaddress.ip_address(data.get("host", "127.0.0.1")).is_loopback:
            raise ValueError()
    except (ValueError, TypeError):
        errors.append("Poco 地址必须为 loopback IP（如 127.0.0.1）")
    return errors


def device_binding_errors(device, executor_id, db_path=None):
    errors = []
    if not device:
        return ["UI 设备不存在"]
    if not int(device.get("enabled", 1)):
        errors.append("UI 设备已停用")
    bound = device.get("executor_id")
    if bound and executor_id is not None and int(bound) != int(executor_id):
        errors.append("UI 设备绑定的执行机与计划执行机不一致")
    if bound and db_path:
        from . import db

        ex = db.get_executor(db_path, bound)
        if not ex or not int(ex.get("enabled", 1)):
            errors.append("UI 设备绑定的执行机不存在或已停用")
        elif db._json_or_list(ex.get("allowed_runners")) and "airtest" not in db._json_or_list(
            ex.get("allowed_runners")
        ):
            errors.append("UI 设备绑定的执行机未允许 airtest")
    return errors


def ui_files():
    files = {
        Path(s["root"]).joinpath(c).resolve()
        for s in UI_SUITES.values()
        if s.get("root")
        for c in s["cases"]
    }
    try:
        files.update(
            Path(p).resolve() for p in json.loads(os.environ.get("PLATFORM_UI_BOUND_FILES", "[]"))
        )
    except (ValueError, TypeError):
        pass
    return files


def pytest_ui_errors(params, cwd=""):
    """Reject explicit UI selection (including a parent directory / default cwd).

    Collection guard covers implicit pytest discovery too. This is an engine
    routing constraint, not a sandbox for arbitrary trusted Python code.
    """
    errors = []
    args = params.get("args") or []
    if any("platform_ui_guard" in str(a) for a in args):
        errors.append("pytest 不允许禁用 UI 收集保护")
    if cwd:
        base = Path(cwd).resolve()
        dedicated_roots = [
            Path(s["root"]).resolve() for s in UI_SUITES.values() if s.get("dedicated", False)
        ]
        if any(base == p or p in base.parents for p in dedicated_roots):
            errors.append("pytest 不允许使用专用 UI 套件工作目录")
        targets = [
            str(a).split("::")[0]
            for a in args
            if not str(a).startswith("-")
            and (str(a).split("::")[0].endswith(".py") or (base / str(a)).is_dir())
        ]
        if not targets and not args:
            targets = ["."]
        for t in targets:
            target = (base / t).resolve()
            if any(target == p or target in p.parents for p in ui_files()):
                errors.append("pytest 不允许引用 UI 套件目录/用例；请选择 engine=airtest")
                break
    return errors


@scoped
def validate_ui_params(params, db_path=None):
    from .runtime_requirements import requirement_errors

    s = UI_SUITES.get(params.get("suite"))
    d = get_device(params.get("device"), db_path)
    errors = requirement_errors(params, db_path)
    if not s:
        errors.append("airtest suite 必须是受控 UI 套件引用")
    if not d:
        errors.append("airtest device 必须是受控设备引用")
    if s and d and s["driver"] != d["driver"]:
        errors.append("UI 套件与设备驱动不相容")
    if s and params.get("case") not in s["cases"]:
        errors.append("airtest case 必须是该 UI 套件已登记的用例")
    try:
        n = float(params.get("wait_timeout", 10))
        if not 0 < n <= 120:
            raise ValueError()
    except (TypeError, ValueError):
        errors.append("wait_timeout 必须在 0..120 秒之间（不含 0）")
    if params.get("reset", "none") != "none":
        errors.append("首版 reset 只支持 none；用例须自行检查/恢复初始状态")
    return errors


@scoped
def ui_precheck(params, db_path=None, executor_id=None, device=None):
    """Static only: no device connection, no SDK initialization."""
    errors = validate_ui_params(params, db_path)
    if errors:
        return errors
    s, d = UI_SUITES[params["suite"]], device or get_device(params["device"], db_path)
    errors += device_binding_errors(d, executor_id, db_path)
    if not s.get("root") or not Path(s["root"], params["case"]).is_file():
        errors.append("UI 用例文件未就位")
    if "title_match" in d:
        errors += validate_device_input(
            {
                k: d[k]
                for k in (
                    "name",
                    "executor_id",
                    "title_match",
                    "match_mode",
                    "process_name",
                    "window_class",
                    "unity_editor",
                    "host",
                    "port",
                    "smoke_selector",
                    "smoke_result",
                    "notes",
                    "enabled",
                )
                if k in d
            }
        )
    else:
        uri = d.get("uri", "")
        if not uri.startswith("Windows:///") or uri == "Windows:///":
            errors.append("请在资源与环境登记 UI 设备；旧配置需 PLATFORM_UI_DEVICE_URI")
    try:
        if not 0 < int(d.get("port", 0)) < 65536:
            raise ValueError()
    except (TypeError, ValueError):
        errors.append("Poco 端口配置非法")
    if params["suite"] == "unity-smoke" and not d.get("smoke_selector"):
        errors.append("Unity 冒烟需在 UI 设备中登记安全点击控件")
    if params["suite"] == "unity-smoke" and (
        not d.get("smoke_result") or d.get("smoke_result") == d.get("smoke_selector")
    ):
        errors.append("Unity 冒烟需在 UI 设备中登记不同的点击后目标控件")
    return errors
