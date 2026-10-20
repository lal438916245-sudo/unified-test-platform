"""Plan-level aggregation; a live UI probe holds the same lock as a real Job."""

from __future__ import annotations

import json
import subprocess

from . import db
from .executors.base import child_env
from .runtime_probe import check
from .runtime_requirements import resolve_requirement
from .ui_config import BACKEND, UI_SUITES, get_device


def probe_runtime(executor, config):
    from .config_center import resolve_executor_python

    py = resolve_executor_python(executor)
    if not py:
        return [check("probe", "运行要求检查", "failed", "执行机 Python 不可用")]
    try:
        p = subprocess.run(
            [py, "-B", "-m", "app.runtime_probe"],
            input=json.dumps(config),
            cwd=str(BACKEND),
            env=child_env({"PYTHONPATH": str(BACKEND), "PYTHONDONTWRITEBYTECODE": "1"}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=25,
        )
        if p.returncode:
            raise ValueError("Probe process failed")
        return json.loads(p.stdout)["checks"]
    except subprocess.TimeoutExpired:
        return [
            check(
                "probe",
                "运行要求检查",
                "failed",
                "探测超时，子进程已停止；连接回收未确认",
                "检查客户端响应与连接状态后重新探测",
            )
        ]
    except Exception:
        return [check("probe", "运行要求检查", "failed", "探测未完成，请检查执行机与项目配置")]


def precheck_runtime(path, executor, params, realtime=False, cache=None):
    req = resolve_requirement(params, path)
    config = {
        "root": UI_SUITES[params["suite"]]["root"],
        "runtime_requirements": req,
        "device_config": get_device(params["device"], path),
        "realtime": realtime,
        "probe_ui": realtime,
    }
    cache = cache if cache is not None else {}
    key = json.dumps(config, sort_keys=True)
    if key in cache:
        return cache[key]
    rows = [check("requirements", "公共运行要求", "passed", req["name"] + " · " + req["key"])]
    lock_id = None
    try:
        if realtime:
            lock_id = db.acquire_executor_lock(path, executor["id"], -1)
            if not lock_id:
                rows += probe_runtime(executor, {**config, "probe_ui": False})
                rows.append(
                    check(
                        "device_lock",
                        "Unity / Poco 实时探测",
                        "busy",
                        "执行机被任务或其他探测占用；未连接客户端",
                        "等待设备空闲后重新预检",
                    )
                )
            else:
                rows += probe_runtime(executor, config)
        else:
            rows += probe_runtime(executor, config)
            rows.append(
                check(
                    "poco",
                    "Unity / Poco 实时探测",
                    "not_checked",
                    "仅检查配置与项目依赖，未连接客户端",
                )
            )
    finally:
        db.release_executor_lock(path, lock_id)
    out = {
        "requirements": req,
        "checks": rows,
        "ok": (
            all(r["status"] in ("passed", "not_applicable") for r in rows)
            if realtime
            else not any(r["status"] == "failed" for r in rows)
        ),
    }
    cache[key] = out
    return out
