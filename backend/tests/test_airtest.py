"""UI routing, subprocess outcomes and evidence, using fake devices only."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import types

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config_center as cc, db
from app.api import build_app
from app.ui_config import UI_SUITES, UI_DEVICES, pytest_ui_errors
from app.executors.airtest_runner import AirtestRunner
from app.executors.pytest_runner import PytestRunner
from app.executors import airtest_runner as ar
from app.report_center import engine_compare_key, compare_compatibility, metric_deltas


def params(**extra):
    return {"suite": "unity-smoke", "case": "test_unity_smoke.py", "device": "unity-windows", **extra}


@pytest.fixture
def client(tmp_path):
    path = str(tmp_path / "db.sqlite")
    db.init_db(path)
    app = build_app(str(tmp_path / "static"))
    app.state.db_path = path
    e = db.create_environment(path, {"name": "offline", "kind": "offline", "host": "", "port": 0})
    x = db.create_executor(path, {"name": "x", "cwd_root": "backend-demo", "python_executable": sys.executable,
                                 "allowed_runners": ["pytest", "airtest"], "enabled": 1})
    with TestClient(app) as c:
        yield c, path, e, x


def test_ui_case_binding_fingerprint_and_override(client):
    c, path, e, x = client
    body = {"name": "ui", "engine": "airtest", "params": params(), "requires_exclusive": False}
    r = c.post("/api/test-cases", json=body)
    assert r.status_code == 200, r.text
    tc = db.get_test_case(path, r.json()["id"])
    assert tc["requires_exclusive"] and tc["asset_kind"] == "airtest"
    assert tc["fingerprint"] == cc.test_case_fingerprint("ui", "airtest", params(), True, "airtest")
    ref = {"id": tc["id"], "revision": tc["revision"], "fingerprint": tc["fingerprint"]}
    plan = {"name": "p", "environment_id": e, "executor_id": x,
            "steps": [{"engine": "airtest", "case_ref": ref, "requires_exclusive": False}]}
    r = c.post("/api/plans", json=plan)
    assert r.status_code == 200, r.text
    run = c.post(f"/api/plans/{r.json()['id']}/run")
    assert run.status_code == 200, run.text
    jobs = db.list_jobs_by_run(path, run.json()["run_id"])
    assert jobs[0]["requires_exclusive"]
    assert jobs[0]["config_snapshot"]["content"]["sources"]["ui_device"]["fp"]
    bad = {**body, "name": "bad", "engine": "pytest"}
    assert c.post("/api/test-cases", json=bad).status_code == 400
    body["params"] = params(wait_timeout=5)
    updated = c.put(f"/api/test-cases/{tc['id']}", json=body)
    assert updated.status_code == 200
    current = db.get_test_case(path, tc["id"])
    assert current["revision"] == tc["revision"] + 1 and current["fingerprint"] != tc["fingerprint"]
    assert c.post(f"/api/plans/{r.json()['id']}/run").status_code == 400


@pytest.mark.parametrize("args", [["test_cases/test_mmo_loop.py"], ["test_cases"], ["."]])
def test_pytest_ui_selection_rejected(args):
    assert pytest_ui_errors({"args": args}, UI_SUITES["gameautotest-ui"]["root"])
    assert cc.validate_step_params("pytest", {"args": args, "cwd_root": "gameautotest"})


def test_executor_default_and_legacy_run_are_guarded(client):
    c, path, e, x = client
    db.update_executor(path, x, {"cwd_root": "gameautotest"})
    body = {"name": "bypass", "environment_id": e, "executor_id": x,
            "steps": [{"engine": "pytest", "params": {"args": ["test_cases/test_mmo_loop.py"]}}]}
    assert c.post("/api/plans", json=body).status_code == 400
    from app.domain import Plan, PlanStep
    p = db.create_plan(path, Plan(name="legacy", description="", environment_id=e, executor_id=x,
                       steps=[PlanStep("pytest", "bypass", params=body["steps"][0]["params"])]))
    assert c.post(f"/api/plans/{p}/run").status_code == 400
    assert db.list_runs_meta(path) == []


def test_static_precheck_missing_device_does_not_connect(monkeypatch, client):
    c, path, e, x = client
    monkeypatch.setitem(UI_DEVICES, "unity-windows", {"driver": "unity", "uri": "", "port": "5001"})
    monkeypatch.setattr(cc, "precheck_executor", lambda ex: {"ok": True, "checks": [
        {"name": "airtest 依赖可用", "ok": True}], "next_steps": []})
    pc = cc.precheck_plan(db, path, {"name": "ui", "environment_id": e, "executor_id": x,
                        "steps": [{"engine": "airtest", "params": params()}]})
    assert pc["steps"][0]["requires_exclusive"]
    assert not pc["steps"][0]["ready"]
    result = AirtestRunner().run({"params": params()}, {}, {}, str(Path(path).parent / "junit.xml"), lambda x: None, lambda: False)
    assert result.status == "failed" and result.engine_data["phase"] == "prepare"
    assert result.engine_data["failure_kind"] == "infrastructure"


@pytest.fixture
def fake_device(tmp_path, monkeypatch):
    root = tmp_path / "suite"
    root.mkdir()
    libs = tmp_path / "libs"
    for package in ("airtest", "airtest/core", "poco", "poco/drivers", "poco/drivers/unity3d"):
        d = libs / package
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text("")
    (libs / "airtest/core/api.py").write_text('''
import os
from pathlib import Path
def auto_setup(*args, **kwargs):
    if os.environ.get('FAKE_PREP_FAIL'): raise RuntimeError('no device')
def device(): return type('Device', (), {})()
def snapshot(filename):
    if os.environ.get('FAKE_SCREEN_FAIL'): raise RuntimeError('no screen')
    Path(filename).write_bytes(b'fake screenshot')
''')
    (libs / "poco/drivers/unity3d/__init__.py").write_text('''
import os
class UnityPoco:
    def __init__(self, *args, **kwargs): self.agent = self; self.hierarchy = self; self.rpc = self
    def dump(self):
        if os.environ.get('FAKE_DUMP_FAIL'): raise RuntimeError('no hierarchy')
        return {'root': 'fake'}
    def close(self):
        path = os.environ.get('FAKE_CLOSE_LOG')
        if path:
            with open(path, 'a', encoding='utf-8') as f: f.write('closed\\n')
        if os.environ.get('FAKE_CLOSE_FAIL'): raise RuntimeError('close failed')
''')
    monkeypatch.setitem(UI_SUITES, "fake-ui", {"label": "fake", "root": str(root), "cases": ["test_ui.py"], "driver": "unity"})
    monkeypatch.setitem(UI_DEVICES, "fake-device", {"label": "fake", "driver": "unity", "uri": "Windows:///fake", "host": "127.0.0.1", "port": "5001"})
    original = ar.child_env
    def child(extra):
        env = original(extra)
        env["PYTHONPATH"] = str(libs) + os.pathsep + env["PYTHONPATH"]
        return env
    monkeypatch.setattr(ar, "child_env", child)
    return root, {"suite": "fake-ui", "case": "test_ui.py", "device": "fake-device"}


def execute(tmp_path, fake_device, code, cancelled=lambda: False, register=None):
    root, p = fake_device
    (root / "test_ui.py").write_text(code, encoding="utf-8")
    logs = []
    r = AirtestRunner().run({"params": p}, {}, {"python_executable": sys.executable},
                            str(tmp_path / "out" / "junit.xml"), logs.append, cancelled, register)
    assert not (tmp_path / "out" / ".ui-input.json").exists()
    return r


@pytest.mark.parametrize("code,status,phase", [
    ("def test_ok(platform_poco): assert platform_poco.agent.hierarchy.dump()", "success", "teardown"),
    ("def test_fail(): assert False", "failed", "run"),
    ("import pytest\n@pytest.fixture\ndef bad():\n yield\n assert False\ndef test_ok(bad): pass", "failed", "teardown"),
    ("# zero cases", "failed", "run"),
    ("import pytest\n@pytest.mark.skip\ndef test_skip(): pass", "failed", "run"),
])
def test_real_subprocess_with_fake_device(tmp_path, fake_device, code, status, phase):
    r = execute(tmp_path, fake_device, code)
    assert r.status == status, r.error
    assert r.engine_data_schema == "airtest/1.0" and r.engine_data["phase"] == phase
    assert r.engine_data["connection_cleanup"]["state"] == "closed"
    assert any(a["name"] == "ui-result.json" for a in r.artifacts)
    if "test_fail" in code:
        assert any(a["name"].endswith("screen.png") for a in r.artifacts)
        assert any(a["name"].endswith("hierarchy.json") for a in r.artifacts)


def test_prepare_failure_is_not_test_failure(tmp_path, fake_device, monkeypatch):
    monkeypatch.setenv("FAKE_PREP_FAIL", "1")
    r = execute(tmp_path, fake_device, "def test_ok(): pass")
    assert r.status == "failed" and r.summary["tests"] == 0
    assert r.engine_data["phase"] == "prepare" and r.engine_data["failure_kind"] == "infrastructure"
    assert r.engine_data["connection_cleanup"]["state"] == "not_connected"


def test_prepare_failure_after_connection_closes_rpc(tmp_path, fake_device, monkeypatch):
    monkeypatch.setenv("FAKE_DUMP_FAIL", "1")
    log = tmp_path / "closed.txt"
    monkeypatch.setenv("FAKE_CLOSE_LOG", str(log))
    fake_device[1]["wait_timeout"] = 0.05
    r = execute(tmp_path, fake_device, "def test_ok(): pass")
    assert r.status == "failed" and r.engine_data["phase"] == "prepare"
    assert r.engine_data["connection_cleanup"]["closed"]
    assert log.read_text(encoding="utf-8").splitlines() == ["closed"]


@pytest.mark.parametrize("fails", [False, True])
def test_cleanup_failure_preserves_primary_result(tmp_path, fake_device, monkeypatch, fails):
    monkeypatch.setenv("FAKE_CLOSE_FAIL", "1")
    log = tmp_path / "closed.txt"
    monkeypatch.setenv("FAKE_CLOSE_LOG", str(log))
    r = execute(tmp_path, fake_device, "def test_case(): assert " + ("False" if fails else "True"))
    assert r.status == ("failed" if fails else "success")
    assert r.engine_data["failure_kind"] == ("test" if fails else None)
    assert r.error == ("UI 用例断言或 fixture 失败" if fails else None)
    assert r.engine_data["connection_cleanup"]["state"] == "failed"
    assert r.engine_data["cleanup_errors"] and (tmp_path / "out/cleanup-trace.txt").is_file()
    assert log.read_text(encoding="utf-8").splitlines() == ["closed"]


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_worker_unexpected_exit_still_closes(tmp_path, monkeypatch, exception):
    from app.executors import ui_worker
    closed = []
    config = {"output": str(tmp_path), "root": str(tmp_path), "case": "test_unused.py", "junit": str(tmp_path / "junit.xml")}
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["worker", str(input_path)])
    monkeypatch.chdir(tmp_path)
    def prepare(plugin):
        plugin.poco = types.SimpleNamespace(agent=types.SimpleNamespace(rpc=types.SimpleNamespace(close=lambda: closed.append(True))))
    def raise_error(*args, **kwargs):
        raise exception("unexpected exit")
    monkeypatch.setattr(ui_worker.UIPlugin, "prepare", prepare)
    monkeypatch.setattr(pytest, "main", raise_error)
    if exception is RuntimeError:
        assert ui_worker.main() == 2
    else:
        with pytest.raises(exception):
            ui_worker.main()
    assert closed == [True]
    meta = json.loads((tmp_path / "ui-result.json").read_text(encoding="utf-8"))
    assert meta["connection_cleanup"]["closed"]


def test_rpc_cleanup_is_idempotent(tmp_path):
    from app.executors.ui_worker import UIPlugin
    closed = []
    plugin = UIPlugin(tmp_path, {})
    plugin.poco = types.SimpleNamespace(agent=types.SimpleNamespace(rpc=types.SimpleNamespace(close=lambda: closed.append(True))))
    plugin.close()
    plugin.close()
    assert closed == [True]


def test_evidence_failure_keeps_test_failure(tmp_path, fake_device, monkeypatch):
    monkeypatch.setenv("FAKE_SCREEN_FAIL", "1")
    r = execute(tmp_path, fake_device, "def test_fail(): assert False")
    assert r.status == "failed" and r.engine_data["failure_kind"] == "test"
    assert r.engine_data["evidence_errors"]


def test_cancel_kills_ui_subprocess(tmp_path, fake_device):
    ready = threading.Event()
    def register(proc): ready.set()
    r = execute(tmp_path, fake_device, "import time\ndef test_sleep(): time.sleep(60)", ready.is_set, register)
    assert r.status == "cancelled"


def test_content_change_rejects_retry_and_comparison(tmp_path, fake_device):
    root, p = fake_device
    (root / "test_ui.py").write_text("def test_ok(): pass")
    snap = {"content": cc.build_content_fingerprint("airtest", p)}
    job = {"engine": "airtest", "params": p, "config_snapshot": snap}
    before = engine_compare_key(job, {}, "airtest/1.0")
    (root / "test_ui.py").write_text("def test_ok(): assert True")
    r = AirtestRunner().run(job, {}, {}, str(tmp_path / "out/junit.xml"), lambda x: None, lambda: False)
    assert r.status == "failed" and "变化" in r.error
    job["config_snapshot"] = {"content": cc.build_content_fingerprint("airtest", p)}
    after = engine_compare_key(job, {}, "airtest/1.0")
    assert before["compare_fp"] != after["compare_fp"]
    assert not compare_compatibility({"engine": "airtest", "compare_key": before},
                                     {"engine": "airtest", "compare_key": after})["compatible"]


def test_pytest_collection_guard_prevents_implicit_ui_discovery(tmp_path, fake_device):
    root, p = fake_device
    (root / "test_ui.py").write_text("def test_bad(): raise Exception('SHOULD NOT RUN')")
    # Export registry into a subprocess-local startup hook, without real project edits.
    (root / "conftest.py").write_text("from app.ui_config import UI_SUITES\nUI_SUITES['fixture'] = " + repr(UI_SUITES["fake-ui"]), encoding="utf-8")
    logs = []
    r = PytestRunner().run({"params": {"cwd": str(root), "args": ["-q"]}}, {},
                          {"python_executable": sys.executable}, str(tmp_path / "junit.xml"), logs.append, lambda: False)
    assert r.status == "failed"
    assert "SHOULD NOT RUN" not in "".join(logs)
    assert "airtest" in "".join(logs)


def test_scheduler_timeout_lock_and_retry(client, fake_device, tmp_path, monkeypatch):
    from app.orchestration import scheduler as sc
    from app.domain import Plan, PlanStep
    c, path, e, x = client
    root, p = fake_device
    (root / "test_ui.py").write_text("import os,time\ndef test_wait():\n if os.getenv('FAKE_LONG'): time.sleep(60)\n assert True", encoding="utf-8")
    monkeypatch.setenv("FAKE_LONG", "1")
    monkeypatch.setattr(sc, "DATA_DIR", str(tmp_path / "data"))
    plan = db.create_plan(path, Plan(name="timeout", description="", environment_id=e, executor_id=x,
        steps=[PlanStep(engine="airtest", name="timeout", params={**p, "timeout_sec": 3})]))
    run = c.post(f"/api/plans/{plan}/run").json()["run_id"]
    job = db.list_jobs_by_run(path, run)[0]
    o = sc.Orchestrator(path)
    watchdog = threading.Thread(target=o._watchdog_loop, daemon=True)
    watchdog.start()
    try:
        o._process(job)
        done = db.get_job(path, job["id"])
        assert done["status"] == "timedout"
        assert db.get_active_executor_lock(path, x) is None
        report = json.loads(Path(sc.DATA_DIR, done["report_path"]).read_text(encoding="utf-8"))
        assert report["engine_data"]["phase"] in ("prepare", "run")
        monkeypatch.delenv("FAKE_LONG")
        retry = o.retry(done)
        retried = db.get_job(path, retry)
        assert retried["config_snapshot"] == job["config_snapshot"]
        o._process(retried)
        assert db.get_job(path, retry)["status"] == "success"
        assert db.get_job(path, job["id"])["status"] == "timedout"
        assert db.get_active_executor_lock(path, x) is None
    finally:
        o.shutdown()


def test_nested_evidence_download_and_private_input(client, tmp_path, monkeypatch):
    from app.api import router as router_module
    from app.domain import Plan, Job
    c, path, e, x = client
    monkeypatch.setattr(router_module, "DATA_DIR", str(tmp_path / "data"))
    pid = db.create_plan(path, Plan(name="download", description="", environment_id=e, executor_id=x, steps=[]))
    run = db.create_run(path, pid)
    jid = db.insert_job(path, Job(plan_run_id=run, plan_id=pid, engine="airtest", name="ui", environment_id=e, executor_id=x))
    output = tmp_path / "data" / "reports" / f"run_{run}" / f"job_{jid}"
    (output / "airtest-log").mkdir(parents=True)
    (output / "airtest-log/log.txt").write_text("evidence")
    (output / ".ui-input.json").write_text("private")
    assert c.get(f"/api/artifacts/{jid}/airtest-log/log.txt").text == "evidence"
    assert c.get(f"/api/artifacts/{jid}/.ui-input.json").status_code == 404
    assert c.get(f"/api/artifacts/{jid}/%2e%2e/db.sqlite").status_code == 404


def test_ui_assets_and_catalog():
    cat = cc.public_catalog()
    assert "airtest" in cat["engines"]
    assert cat["asset"]["ui_cases"]["unity-smoke"] == ["test_unity_smoke.py"]
    asset = {"name": "ui", "kind": "airtest", "scenario_ref": "unity-smoke"}
    assert cc.validate_asset_input(asset) == []
    assert cc.precheck_asset_source(asset)["ok"]
    assert cc.asset_fingerprint("airtest", asset) != cc.asset_fingerprint("locust", asset)
    deltas = metric_deltas({"metrics": {"passed": 1, "failed": 1}},
                           {"engine": "airtest", "metrics": {"passed": 2, "failed": 0}})
    assert {item["key"]: item["delta"] for item in deltas["items"]} == {"passed": 1, "failed": -1}


@pytest.mark.parametrize("already_open", [False, True])
def test_colyseus_smoke_uses_safe_begin_flow(monkeypatch, already_open):
    import importlib.util
    file = Path(UI_SUITES["colyseus-ui-smoke"]["root"]) / "test_colyseus_ui.py"
    spec = importlib.util.spec_from_file_location("colyseus_smoke", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actions = []
    class Control:
        def __init__(self, key): self.key = key
        def wait_for_appearance(self, timeout): pass
        def exists(self):
            return already_open or ("click", "BeginBtn") in actions if self.key == "Sign Up" else True
        def set_text(self, value): actions.append((self.key, value))
        def click(self): actions.append(("click", self.key))
    def poco(name=None, **kw): return Control(name or kw["text"])
    for key, value in {"GAME_SERVER_HOST": "127.0.0.1", "GAME_SERVER_PORT": "2567", "PLATFORM_UI_WAIT_TIMEOUT": "5"}.items():
        monkeypatch.setenv(key, value)
    if already_open:
        with pytest.raises(AssertionError): module.test_begin_opens_signup_entry(poco)
        assert not actions
    else:
        module.test_begin_opens_signup_entry(poco)
        assert actions == [("ServerInput", "127.0.0.1"), ("PortInput", "2567"), ("click", "BeginBtn")]


@pytest.mark.parametrize("view_exists", [True, False])
def test_unity_editor_uses_gameview_in_selected_window(tmp_path, monkeypatch, view_exists):
    from app.executors.ui_worker import UIPlugin
    view = types.SimpleNamespace(exists=lambda: view_exists, wrapper_object=lambda: "selected-game-view")
    queried = []
    def child_window(**kwargs):
        queried.append(kwargs)
        return view
    focused = []
    device = types.SimpleNamespace(app=types.SimpleNamespace(top_window=lambda: types.SimpleNamespace(child_window=child_window)),
                                   set_focus=lambda: focused.append(True))
    api = types.ModuleType("airtest.core.api")
    api.auto_setup = lambda *args, **kwargs: None
    api.device = lambda: device
    poco_module = types.ModuleType("poco.drivers.unity3d")
    poco_module.UnityPoco = lambda **kwargs: types.SimpleNamespace(agent=types.SimpleNamespace(
        hierarchy=types.SimpleNamespace(dump=lambda: {"root": "test"})))
    monkeypatch.setitem(sys.modules, "airtest.core.api", api)
    monkeypatch.setitem(sys.modules, "poco.drivers.unity3d", poco_module)
    config = {"root": str(tmp_path), "wait_timeout": 1,
              "device_config": {"uri": "Windows:///explicit-hwnd", "host": "127.0.0.1", "port": 5001, "unity_editor": True}}
    plugin = UIPlugin(tmp_path, config)
    if not view_exists:
        with pytest.raises(RuntimeError, match="GameView"):
            plugin.prepare()
    else:
        plugin.prepare()
        assert device._top_window == "selected-game-view" and device.focus_rect == (0, 40, 0, 0)
        assert focused == [True]
    assert queried == [{"title": "UnityEditor.GameView"}]
