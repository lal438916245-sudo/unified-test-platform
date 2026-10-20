"""前端计划编辑器「加载 → 保存」往返保真 · 回归测试。

为什么放在 pytest 里：这类缺陷（params 键序被重排、表单不管理的键被丢掉、
缺失键被凭空注入默认值）只发生在前端 JS 的合并逻辑里，后端单测覆盖不到；
但它的后果会写进 plans.steps、进而改变 plan_snapshot 指纹，属于必须回归的部分。

做法：本测试在**隔离临时 SQLite** 里造出计划，导出为 JSON，
再驱动 `_v0.3_baseline/fe_roundtrip_test.js`（真实加载 index.html 的内联脚本、
真实渲染表单、再从渲染结果读回控件值）做逐字节比对。

不依赖运行中的服务、不发起任何执行、不触碰真实运行库。
若本机无 node，则跳过（不计为失败）。
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import config_center as cc  # noqa: E402
from app import db  # noqa: E402
from app.domain import Plan, PlanStep  # noqa: E402

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PLATFORM = os.path.dirname(_BACKEND)
_HARNESS = os.path.join(_PLATFORM, "_v0.3_baseline", "fe_roundtrip_test.js")

# node 位置：优先环境变量 PLATFORM_NODE，其次 PATH 上的 node。
# 刻意不硬编码本机路径 —— 该文件会进入仓库，必须对任意 runner 可用。
_NODE_CANDIDATES = [p for p in (os.environ.get("PLATFORM_NODE"),) if p]


def _find_node() -> str | None:
    for p in _NODE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return shutil.which("node")


# 与真实运行库 plan#1–8 同形：cwd 是"表单不管理"的键（后端白名单允许、前端无控件）
_PLAN_A_STEPS = [{
    "engine": "pytest", "name": "unmanaged", "requires_exclusive": False,
    "params": {"timeout_sec": 60, "cwd": "X:\\probe\\backend",
               "args": ["demo/test_platform_demo.py"], "asset_source_id": 7},
}]
# 与真实运行库 4 个 locust 步骤同形：注意入库键序 timeout_sec 在**最后**
_PLAN_B_STEPS = [{
    "engine": "locust", "name": "colyseus-bot", "requires_exclusive": False,
    "params": {"locustfile": "http-fixture", "users": 4, "spawn_rate": 2,
               "run_time": "12s", "csv_full_history": False, "timeout_sec": 120},
}]

_PLAN_UI_STEPS = [{
    "engine": "airtest", "name": "ui-smoke", "requires_exclusive": True,
    "params": {"suite": "unity-smoke", "case": "test_unity_smoke.py", "device": "unity-windows", "timeout_sec": 120},
}]


def test_frontend_plan_editor_roundtrip_is_byte_identical():
    node = _find_node()
    if not node:
        import pytest
        pytest.skip("本机未找到 node，无法运行前端往返测试台")
    assert os.path.isfile(_HARNESS), f"缺少前端往返测试台：{_HARNESS}"

    tmp = tempfile.mkdtemp(prefix="pe_fe_rt_")
    try:
        db_path = os.path.join(tmp, "db.sqlite")
        db.init_db(db_path)

        # 受控配置：Plan 引用校验要求"存在且启用"
        env = db.create_environment(db_path, {
            "name": "offline-eval", "host": "127.0.0.1", "port": 8000,
            "kind": "offline", "protocol": "http", "labels": [], "enabled": 1})
        ex = db.create_executor(db_path, {
            "name": "anaconda-matcheval", "python_executable": sys.executable, "cwd": tmp,
            "python_ref": "anaconda", "cwd_root": "backend-demo",
            "allowed_runners": ["pytest", "locust", "matcheval"],
            "max_concurrency": 1, "enabled": 1})
        asset = db.create_asset_source(db_path, {
            "name": "locust-colyseus", "kind": "locust",
            "scenario_ref": "colyseus-bot", "enabled": 1})

        plans = []
        for name, steps in (("rt-unmanaged", _PLAN_A_STEPS), ("rt-locust-order", _PLAN_B_STEPS), ("rt-ui", _PLAN_UI_STEPS)):
            pid = db.create_plan(db_path, Plan(
                name=name, description="", environment_id=env, executor_id=ex,
                asset_source_id=asset, fail_fast=True, owner="local",
                steps=[PlanStep(**s) for s in steps]))
            plans.append(db.get_plan(db_path, pid))

        payload = {
            "plans": [{
                "id": p["id"], "name": p["name"], "description": p["description"] or "",
                "environment_id": p["environment_id"], "executor_id": p["executor_id"],
                "asset_source_id": p["asset_source_id"] or 0, "fail_fast": bool(p["fail_fast"]),
                "owner": p["owner"] or "local", "revision": p["revision"],
                "created_at": p["created_at"], "updated_at": p["updated_at"],
                "steps": p["steps"],
            } for p in plans],
            # 实体必须用与 API 完全相同的脱敏形态（例如 allowed_runners 要是 list，
            # 而不是数据库里的 JSON 字符串），否则前端渲染会抛错、控件读不到值。
            "envs": [cc.mask_environment(e) for e in db.list_environments(db_path)],
            "execs": [cc.mask_executor(x) for x in db.list_executors(db_path)],
            "assets": [cc.mask_asset_source(a) for a in db.list_asset_sources(db_path)],
            "catalog": cc.public_catalog(),
        }
        inp = os.path.join(tmp, "fe_input.json")
        with open(inp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        env_vars = dict(os.environ)
        env_vars["PLATFORM_ROOT"] = _PLATFORM
        r = subprocess.run([node, _HARNESS, inp], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env_vars, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        assert r.returncode == 0, f"前端往返测试台未通过：\n{out[-4000:]}"
        assert "0 failed" in out, out[-2000:]
    finally:
        # gc + 重试：Windows 上 sqlite 句柄释放滞后（见 P1 技术债），否则临时库删不掉
        for _ in range(8):
            gc.collect()
            shutil.rmtree(tmp, ignore_errors=True)
            if not os.path.exists(tmp):
                break
            time.sleep(0.2)


@pytest.mark.parametrize("shared_requirement", [False, True])
def test_airtest_suite_reference_roundtrip_saves_valid_script(tmp_path, shared_requirement):
    import pytest
    from fastapi.testclient import TestClient
    from app.api import build_app
    node = _find_node()
    if not node:
        pytest.skip("Node is required for editor DOM regression")
    db_path = str(tmp_path / "db.sqlite")
    db.init_db(db_path)
    env = db.create_environment(db_path, {"name": "offline", "kind": "offline", "host": "", "port": 0})
    ex = db.create_executor(db_path, {"name": "UI", "python_executable": sys.executable, "cwd_root": "backend-demo",
                                   "allowed_runners": ["airtest"], "enabled": 1})
    did = db.create_ui_device(db_path, {"name": "Unity", "executor_id": ex, "title_match": "Test Unity",
        "match_mode": "contains", "process_name": "Unity.exe", "window_class": "", "unity_editor": 0,
        "host": "127.0.0.1", "port": 5001, "smoke_selector": "", "smoke_result": "", "notes": "", "enabled": 1})
    app = build_app(str(tmp_path / "static"))
    app.state.db_path = db_path
    with TestClient(app) as client:
        extra = {}
        if shared_requirement:
            req = client.post("/api/config/runtime-requirements", json={"name": "MMO shared", "suite": "gameautotest-ui",
                "modules": ["requests"], "mongo_ref": "gameautotest", "enabled": 1}).json()
            extra["requirements"] = req["key"]
        case = client.post("/api/test-cases", json={"name": "register", "engine": "airtest", "params": {
            "suite": "gameautotest-ui", "case": "test_cases/test_mmo_loop.py", "device": f"ui-device-{did}",
            "wait_timeout": 10, "reset": "none", "timeout_sec": 180, **extra}}).json()
        suite = client.post("/api/suites", json={"name": "MMO suite", "cases": [
            {"id": case["id"], "revision": case["revision"], "fingerprint": case["fingerprint"]}]}).json()
        payload = {"catalog": cc.public_catalog(db_path), "refs": {"cases": [case], "suites": [suite],
            "envs": [cc.mask_environment(db.get_environment(db_path, env))],
            "execs": [cc.mask_executor(db.get_executor(db_path, ex))], "assets": []}}
        inp, out = tmp_path / "input.json", tmp_path / "output.json"
        inp.write_text(json.dumps(payload), encoding="utf-8")
        result = subprocess.run([node, os.path.join(_PLATFORM, "_ci", "ui_case_reference_roundtrip.js"), str(inp), str(out)],
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        body = json.loads(out.read_text(encoding="utf-8"))
        body.update(environment_id=env, executor_id=ex)
        response = client.post("/api/plans", json=body)
        assert response.status_code == 200, response.text
        saved = db.get_plan(db_path, response.json()["id"])
        assert all(s["case_ref"]["id"] == case["id"] and not s["override_params"] for s in saved["steps"])
