"""Public requirements, read-only probes and real worker enforcement in isolation."""
import json
from pathlib import Path
import subprocess
import sys
import types

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, config_center as cc
from app.api import build_app
from app import runtime_precheck as rp, runtime_probe as worker
from app.runtime_requirements import resolve_requirement
from app.ui_config import UI_DEVICES
from test_airtest import fake_device  # Production worker with fake SDK libraries only.


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = str(tmp_path / "db.sqlite")
    db.init_db(path)
    ex = db.create_executor(path, dict(name="UI", python_executable=sys.executable,
                                     allowed_runners=["airtest"], enabled=1))
    env = db.create_environment(path, dict(name="offline", kind="offline", host="", port=0))
    monkeypatch.setitem(UI_DEVICES, "test-device", dict(driver="unity", uri="Windows:///101", host="127.0.0.1", port=5001,
                                                      smoke_selector="BeginBtn", smoke_result="Sign Up"))
    monkeypatch.setattr(cc, "precheck_executor", lambda x: dict(ok=True, checks=[dict(name="airtest 依赖可用", ok=True)], missing=[]))
    app = build_app(str(tmp_path / "static"))
    app.state.db_path = path
    with TestClient(app) as c:
        yield c, path, ex, env


def profile(**kw):
    return dict(name="Shared", suite="unity-smoke", modules=["requests"], mongo_ref="", notes="", enabled=1, **kw)


def params(**kw):
    return dict(suite="unity-smoke", case="test_unity_smoke.py", device="test-device", **kw)


def plan(ex, env, p=None):
    return dict(name="UI regression", executor_id=ex, environment_id=env,
                steps=[dict(engine="airtest", params=p or params())])


def test_profile_persists_and_reuses_across_case_suite_plan(setup):
    c, path, ex, env = setup
    response = c.post("/api/config/runtime-requirements", json=profile())
    assert response.status_code == 200, response.text
    req = response.json()
    db.init_db(path)
    assert db.get_runtime_requirement(path, req["id"])["modules"] == ["requests"]
    cases = []
    for name in ("A", "B"):
        r = c.post("/api/test-cases", json=dict(name=name, engine="airtest", params=params(requirements=req["key"])))
        assert r.status_code == 200, r.text
        cases.append(r.json())
    refs = [dict(id=x["id"], revision=x["revision"], fingerprint=x["fingerprint"]) for x in cases]
    assert c.post("/api/suites", json=dict(name="Shared suite", cases=refs)).status_code == 200
    body = plan(ex, env)
    body["steps"] = [dict(engine="airtest", case_ref=r) for r in refs]
    p = c.post("/api/plans", json=body)
    assert p.status_code == 200, p.text
    run = c.post(f"/api/plans/{p.json()['id']}/run")
    jobs = db.list_jobs_by_run(path, run.json()["run_id"])
    assert len(jobs) == 2
    assert all(j["config_snapshot"]["runtime_requirements"]["key"] == req["key"] for j in jobs)
    saved = jobs[0]["config_snapshot"]
    before = cc.build_content_fingerprint("airtest", params(requirements=req["key"]), path)
    c.put(f"/api/config/runtime-requirements/{req['id']}", json={"notes": "changed"})
    assert before == cc.build_content_fingerprint("airtest", params(requirements=req["key"]), path)
    c.put(f"/api/config/runtime-requirements/{req['id']}", json={"modules": ["psutil"]})
    assert before["fingerprint"] != cc.build_content_fingerprint("airtest", params(requirements=req["key"]), path)["fingerprint"]
    assert db.get_job(path, jobs[0]["id"])["config_snapshot"] == saved
    from app.executors.airtest_runner import AirtestRunner
    r = AirtestRunner().run({**jobs[0], "_db_path": path}, {}, {}, str(Path(path).parent / "result/junit.xml"), lambda _: None, lambda: False)
    assert r.status == "failed" and "已变化" in r.error


@pytest.mark.parametrize("changes", [{"modules": ["os;evil"]}, {"modules": "requests"},
    {"mongo_ref": "mongodb://user:secret@host"}, {"suite": "unknown"}, {"enabled": "1"},
    {"script": "arbitrary.py"}, {"name": " "}, {"mongo_ref": "gameautotest"}])
def test_uncontrolled_profiles_rejected(setup, changes):
    c, path, _, _ = setup
    assert c.post("/api/config/runtime-requirements", json={**profile(), **changes}).status_code == 400
    assert db.list_runtime_requirements(path) == []


def test_default_minimum_cannot_be_bypassed_and_disabled_ref_rejected(setup):
    c, path, ex, env = setup
    row = c.post("/api/config/runtime-requirements", json={**profile(), "suite": "gameautotest-ui", "modules": []}).json()
    req = resolve_requirement(dict(suite="gameautotest-ui", requirements=row["key"]), path)
    assert set(req["modules"]) == {"requests", "psutil", "pymongo"}
    assert req["mongo_ref"] == "gameautotest"
    assert cc.validate_step_params("airtest", params(requirements=row["key"]), path)  # Wrong suite.
    c.post(f"/api/config/runtime-requirements/{row['id']}/toggle")
    assert cc.validate_step_params("airtest", dict(suite="gameautotest-ui", case="test_cases/test_full_link_loop.py",
                                                  device="test-device", requirements=row["key"]), path)
    assert resolve_requirement(dict(suite="gameautotest-ui"))["key"] == "mmo-common"
    assert not db.list_runs_meta(path)


@pytest.mark.parametrize("ref", [False, 0, [], {}, None])
def test_invalid_reference_types_cannot_silently_inherit_default(setup, ref):
    c, path, ex, env = setup
    assert c.post("/api/test-cases", json=dict(name="invalid", engine="airtest", params=params(requirements=ref))).status_code == 400


def test_live_precheck_lock_cache_no_jobs_and_release_on_error(setup, monkeypatch):
    c, path, ex, env = setup
    calls = []
    def probe(executor, config):
        assert db.get_active_executor_lock(path, ex)["holder_job_id"] == -1
        calls.append(config)
        return [worker.check("poco", "Poco", "passed", "readable")]
    monkeypatch.setattr(rp, "probe_runtime", probe)
    body = plan(ex, env)
    body["steps"] *= 2
    response = c.post("/api/plans/precheck", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] and response.json()["precheck_mode"] == "realtime"
    assert len(calls) == 1  # Same profile/device checked once in this request only.
    assert not db.get_active_executor_lock(path, ex) and not db.list_runs_meta(path)
    def broken(*args):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(rp, "probe_runtime", broken)
    with pytest.raises(RuntimeError):
        rp.precheck_runtime(path, db.get_executor(path, ex), params(), True)
    assert not db.get_active_executor_lock(path, ex)


def test_device_realtime_endpoint_uses_same_lock_and_no_test_requirement(setup, monkeypatch):
    from app.api import configuration as api
    c, path, ex, env = setup
    did = db.create_ui_device(path, dict(name="Unity", executor_id=ex, title_match="Unique Unity",
        match_mode="contains", process_name="Unity.exe", window_class="", unity_editor=1,
        host="127.0.0.1", port=5001, smoke_selector="", smoke_result="", notes="", enabled=1))
    monkeypatch.setattr(api, "precheck_device", lambda *args: {"ok": True})
    def probe(executor, cfg):
        assert db.get_active_executor_lock(path, ex)["holder_job_id"] == -1
        assert not cfg["runtime_requirements"]["mongo_ref"]
        assert not cfg["runtime_requirements"]["modules"]
        assert cfg["device_config"]["id"] == did
        return [worker.check("poco", "Poco", "passed", "readable")]
    monkeypatch.setattr(rp, "probe_runtime", probe)
    result = c.post(f"/api/config/ui-devices/{did}/runtime-probe")
    assert result.status_code == 200 and result.json()["ok"]
    assert not db.get_active_executor_lock(path, ex) and not db.list_runs_meta(path)
    assert c.post("/api/config/ui-devices/99999/runtime-probe").status_code == 404


def test_busy_device_is_not_connected_and_config_mode_is_explicit(setup, monkeypatch):
    c, path, ex, env = setup
    calls = []
    monkeypatch.setattr(rp, "probe_runtime", lambda _, cfg: calls.append(cfg) or [])
    lock = db.acquire_executor_lock(path, ex, 99)
    result = c.post("/api/plans/precheck", json=plan(ex, env)).json()
    assert not result["ok"]
    assert result["steps"][0]["runtime"]["checks"][-1]["status"] == "busy"
    assert calls and not calls[0]["probe_ui"]
    assert db.get_active_executor_lock(path, ex)["holder_job_id"] == 99
    db.release_executor_lock(path, lock)
    result = c.post("/api/plans/precheck?realtime=false", json=plan(ex, env)).json()
    assert result["ok"] and result["precheck_mode"] == "configuration"
    assert result["steps"][0]["runtime"]["checks"][-1]["status"] == "not_checked"
    assert not calls[-1]["realtime"] and not calls[-1]["probe_ui"]


@pytest.mark.parametrize("failure", ["dependency", "mongodb", "window", "poco", "rpc_cleanup"])
def test_failure_blocks_precheck_with_item_detail(setup, monkeypatch, failure):
    c, path, ex, env = setup
    monkeypatch.setattr(rp, "probe_runtime", lambda *a: [worker.check(failure, failure, "failed", "unavailable", "fix it")])
    result = c.post("/api/plans/precheck", json=plan(ex, env)).json()
    assert not result["ok"] and not result["steps"][0]["ready"]
    assert failure in result["missing"][0] and "fix it" in result["next_steps"]
    assert not db.list_runs_meta(path)


def test_probe_timeout_is_bounded_and_does_not_expose_output(monkeypatch):
    monkeypatch.setattr(cc, "resolve_executor_python", lambda _: sys.executable)
    def timed_out(*args, **kwargs):
        assert kwargs["timeout"] == 25
        raise subprocess.TimeoutExpired(args[0], 25, output="credential-secret")
    monkeypatch.setattr(rp.subprocess, "run", timed_out)
    rows = rp.probe_runtime({}, {})
    assert rows[0]["status"] == "failed" and "超时" in rows[0]["detail"]
    assert "credential-secret" not in json.dumps(rows)


def test_mongodb_uses_project_config_readonly_and_closes(tmp_path, monkeypatch):
    (tmp_path / "config.py").write_text("import os\nMONGO_URI=os.environ['TEST_MONGO_URI']\nMONGO_DB_NAME='target'\nMONGO_USER_COLLECTION='users'\n")
    (tmp_path / "conftest.py").write_text("raise AssertionError('Never import fixture during precheck')")
    monkeypatch.setenv("TEST_MONGO_URI", "mongodb://user:secret@localhost")
    calls = []
    class Client:
        def __init__(self, uri, **kwargs):
            assert uri == "mongodb://user:secret@localhost"
            assert kwargs["serverSelectionTimeoutMS"] == 3000
        @property
        def admin(self): return self
        def command(self, name): calls.append(name)
        def __getitem__(self, name): calls.append(name); return self
        def find_one(self, *args, **kwargs): calls.append("read"); return {"_id": "private record"}
        def close(self): calls.append("closed")
    monkeypatch.setitem(sys.modules, "pymongo", types.SimpleNamespace(MongoClient=Client))
    cfg = dict(root=str(tmp_path), runtime_requirements=dict(modules=["pymongo"], mongo_ref="gameautotest"))
    rows = worker.check_project(cfg)
    assert all(r["ok"] for r in rows)
    assert calls == ["ping", "target", "users", "read", "closed"]
    assert "secret" not in json.dumps(rows) and "private record" not in json.dumps(rows)
    calls.clear()
    rows = worker.check_project(cfg, realtime=False)
    assert not calls and rows[-1]["status"] == "not_checked"


def test_mongodb_failure_closes_and_redacts(tmp_path, monkeypatch):
    (tmp_path / "config.py").write_text("MONGO_URI='mongodb://secret'\nMONGO_DB_NAME='db'\nMONGO_USER_COLLECTION='users'\n")
    closed = []
    class Client:
        def __init__(self, *a, **kw): pass
        @property
        def admin(self): return self
        def command(self, *a): raise RuntimeError("credential-secret")
        def close(self): closed.append(True)
    monkeypatch.setitem(sys.modules, "pymongo", types.SimpleNamespace(MongoClient=Client))
    rows = worker.check_project(dict(root=str(tmp_path), runtime_requirements=dict(modules=[], mongo_ref="gameautotest")))
    assert rows[-1]["status"] == "failed" and closed == [True]
    assert "credential-secret" not in json.dumps(rows)


@pytest.mark.parametrize("failure", [None, "connect", "dump", "close"])
@pytest.mark.parametrize("editor", [False, True])
def test_readonly_rpc_probe_always_closes(monkeypatch, failure, editor):
    from app import ui_windows
    monkeypatch.setattr(ui_windows, "resolve_window", lambda _: dict(hwnd=101, title="Unity"))
    calls = []
    if editor:
        class LegacyApplication:
            def __init__(self, backend):
                assert backend == "win32"
            def connect(self, **kwargs):
                # Mirrors installed pywinauto 0.6.3's incompatible argument guard.
                if "timeout" in kwargs:
                    raise ValueError("Timeout could be specified with path param only")
                assert kwargs == {"handle": 101}
                return self
            def window(self, **kwargs):
                assert kwargs == {"handle": 101}
                return self
            def child_window(self, **kwargs):
                assert kwargs == {"title": "UnityEditor.GameView"}
                return self
            def exists(self, timeout):
                assert timeout == 1
                return True
        monkeypatch.setitem(sys.modules, "pywinauto", types.SimpleNamespace(Application=LegacyApplication))
    class RPC:
        def __init__(self, conn): pass
        def connect(self):
            calls.append("connect")
            if failure == "connect": raise RuntimeError("private")
        def call(self, method):
            calls.append(method)
            class Pending:
                def wait(self, timeout):
                    assert timeout <= 5
                    if failure == "dump" and method == "Dump": raise TimeoutError("private")
                    return (6 if method == "GetSDKVersion" else {"name": "root"}), None
            return Pending()
        def close(self):
            calls.append("close")
            if failure == "close": raise RuntimeError("private")
    monkeypatch.setitem(sys.modules, "poco.utils.simplerpc.rpcclient", types.SimpleNamespace(RpcClient=RPC))
    monkeypatch.setitem(sys.modules, "poco.utils.simplerpc.transport.tcp.main", types.SimpleNamespace(TcpClient=lambda _: object()))
    rows = worker.check_unity(dict(device_config=dict(title_match="Unity", host="127.0.0.1", port=5001, unity_editor=int(editor))))
    assert next(r for r in rows if r["key"] == "gameview")["status"] == ("passed" if editor else "not_applicable")
    assert calls[-1] == "close"
    assert all(m in ("connect", "GetSDKVersion", "Dump", "close") for m in calls)
    assert not any(r["status"] == "failed" for r in rows) if failure is None else any(r["status"] == "failed" for r in rows)


def test_no_unique_window_never_connects_rpc(monkeypatch):
    from app import ui_windows
    def absent(_): raise RuntimeError("no window")
    monkeypatch.setattr(ui_windows, "resolve_window", absent)
    rows = worker.check_unity(dict(device_config=dict(title_match="Unity")))
    assert [(r["key"], r["status"]) for r in rows] == [("window", "failed"), ("gameview", "not_checked"), ("poco", "not_checked")]


def test_refused_connection_releases_local_socket_without_false_cleanup_failure(monkeypatch):
    from app import ui_windows
    monkeypatch.setattr(ui_windows, "resolve_window", lambda _: dict(hwnd=101, title="Unity"))
    closed = []
    transport = types.SimpleNamespace(c=types.SimpleNamespace(sock=types.SimpleNamespace(close=lambda: closed.append(True))))
    class RPC:
        def __init__(self, connection): pass
        def connect(self): raise ConnectionRefusedError()
        def close(self): raise OSError("shutdown on unconnected socket")
    monkeypatch.setitem(sys.modules, "poco.utils.simplerpc.rpcclient", types.SimpleNamespace(RpcClient=RPC))
    monkeypatch.setitem(sys.modules, "poco.utils.simplerpc.transport.tcp.main", types.SimpleNamespace(TcpClient=lambda _: transport))
    rows = worker.check_unity(dict(device_config=dict(title_match="Unity", host="127.0.0.1", port=5001)))
    assert closed == [True]
    assert next(r for r in rows if r["key"] == "poco")["status"] == "failed"
    assert rows[-1]["key"] == "rpc_cleanup" and rows[-1]["status"] == "not_applicable"


def test_real_worker_enforces_project_requirements_before_fixture(tmp_path, fake_device):
    from app.executors.airtest_runner import AirtestRunner
    root, p = fake_device
    path = str(tmp_path / "db.sqlite")
    db.init_db(path)
    rid = db.save_runtime_requirement(path, {**profile(), "suite": "fake-ui", "modules": ["pymongo"]})
    p["requirements"] = f"requirement-{rid}"
    (root.parent / "libs/pymongo.py").write_text("raise ImportError('missing MongoDB dependency')")
    (root / "conftest.py").write_text("raise AssertionError('must not reach fixture')")
    (root / "test_ui.py").write_text("def test_never(): assert False")
    result = AirtestRunner().run(dict(params=p, _db_path=path), {}, dict(python_executable=sys.executable),
                                str(tmp_path / "result/junit.xml"), lambda _: None, lambda: False)
    assert result.status == "failed" and result.engine_data["phase"] == "prepare"
    assert result.engine_data["failure_kind"] == "infrastructure"
    assert "pymongo" in result.error
    assert result.engine_data["connection_cleanup"]["state"] == "not_connected"
    assert not (tmp_path / "result/junit.xml").exists()
