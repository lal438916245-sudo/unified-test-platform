"""Persistent UI device configuration and read-only window discovery."""

import json
import subprocess

from . import db
from .ui_config import BACKEND, device_binding_errors, device_fingerprint, validate_device_input


def device_payload(payload, existing=None):
    defaults = dict(
        name="",
        executor_id=0,
        title_match="",
        match_mode="contains",
        process_name="Unity.exe",
        window_class="",
        unity_editor=1,
        host="127.0.0.1",
        port=5001,
        smoke_selector="",
        smoke_result="",
        notes="",
        enabled=1,
    )
    defaults.update({k: v for k, v in (existing or {}).items() if k in db.UI_DEVICE_COLS})
    defaults.update(payload)
    return {k: v.strip() if isinstance(v, str) else v for k, v in defaults.items()}


def validate_device_record(path, data, did=None):
    errors = validate_device_input(data)
    if errors:
        return errors
    errors += device_binding_errors(data, data["executor_id"], path)
    # Disabled records can still be edited and saved.
    errors = [e for e in errors if e != "UI 设备已停用"]
    if any(d["name"] == data["name"] and d["id"] != did for d in db.list_ui_devices(path)):
        errors.append("已存在同名 UI 设备")
    return errors


def probe_windows(executor, config):
    from .config_center import resolve_executor_python
    from .executors.base import child_env

    if not executor:
        raise ValueError("请先选择关联执行机（需允许 airtest）")
    py = resolve_executor_python(executor)
    if not py:
        raise ValueError("执行机 Python 不可用")
    try:
        proc = subprocess.run(
            [py, "-B", "-m", "app.ui_windows"],
            input=json.dumps(config),
            cwd=str(BACKEND),
            env=child_env({"PYTHONPATH": str(BACKEND)}),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise ValueError("窗口探测超时，请检查执行机和交互式桌面会话") from None
    if proc.returncode:
        raise ValueError(
            "窗口探测失败，请预检执行机及 pywin32 依赖，并确认平台运行在交互式桌面会话"
        )
    return json.loads(proc.stdout)


def precheck_device(path, row):
    from .config_center import precheck_executor

    errors = validate_device_input({k: row[k] for k in db.UI_DEVICE_COLS})
    errors += device_binding_errors(row, row["executor_id"], path)
    ex = db.get_executor(path, row["executor_id"])
    if ex:
        check = precheck_executor({**ex, "allowed_runners": ["airtest"]})
        errors += check["missing"]
    return {
        "entity": "UI 设备",
        "id": row["id"],
        "name": row["name"],
        "ok": not errors,
        "checks": [
            {
                "name": "配置与执行机",
                "ok": not errors,
                "detail": (
                    "；".join(errors) if errors else "静态配置就绪；未连接 Poco、未操作客户端"
                ),
            }
        ],
        "missing": errors,
        "next_steps": ["修正配置后重新预检"] if errors else [],
        "fingerprint": device_fingerprint(None, device=row),
    }
