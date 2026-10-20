"""Persistent devices, per-instance routing, window uniqueness and real worker transport."""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, config_center as cc
from app.api import build_app
from app.ui_config import get_device, device_fingerprint
from app.ui_windows import matching_windows, resolve_window
from test_airtest import fake_device  # Reuse subprocess SDK doubles; never connect to Unity.


@pytest.fixture
def setup(tmp_path):
    path = str(tmp_path / "db.sqlite")
    db.init_db(path)
    ex = db.create_executor(path, dict(name="ui", python_ref="anaconda", python_executable=sys.executable,
        cwd_root="backend-demo", enabled=1, allowed_runners=["pytest", "airtest"]))
    env = db.create_environment(path, dict(name="offline", kind="offline", host="", port=0))
    app = build_app(str(tmp_path / "static"))
    app.state.db_path = path
    with TestClient(app) as client:
        yield client, path, ex, env


def record(ex, **extra):
    return dict(name="Unity client", executor_id=ex, title_match="ColyseusTechDemo-MMO",
                match_mode="contains", process_name="Unity.exe", window_class="", unity_editor=0,
                host="127.0.0.1", port=5001, **extra)


def save(client, ex):
    response = client.post("/api/config/ui-devices", json=record(ex))
    assert response.status_code == 200, response.text
    return response.json()


def plan(ex, env, did):
    return dict(name="UI", executor_id=ex, environment_id=env, steps=[dict(engine="airtest",
        params=dict(suite="unity-smoke", case="test_unity_smoke.py", device=f"ui-device-{did}"))])


def test_persistence_catalog_and_instance_isolation(setup, tmp_path):
    c, path, ex, env = setup
    row = save(c, ex)
    ref = f"ui-device-{row['id']}"
    assert get_device(ref, path)["title_match"] == row["title_match"]
    db.init_db(path)  # Restart migration is idempotent and preserves saved config.
    assert db.get_ui_device(path, row["id"]) == row
    assert c.get("/api/config/catalog").json()["asset"]["ui_devices"][0]["executor_id"] == ex
    second = str(tmp_path / "other.sqlite")
    db.init_db(second)
    assert get_device(ref, second) is None
    body = record(ex)
    body["title_match"] = "Another game"
    db.create_ui_device(second, body)
    assert get_device(ref, second)["title_match"] != get_device(ref, path)["title_match"]
    assert c.post("/api/config/ui-devices", json=record(ex)).status_code == 400


@pytest.mark.parametrize("changes", [
    {"host": "192.168.1.1"}, {"port": 0}, {"port": 5001.5}, {"port": True},
    {"executor_id": 9999}, {"title_match": " "}, {"match_mode": "regex"},
    {"uri": "Windows:///1"}, {"process_name": "C:/Unity.exe"}, {"unity_editor": "yes"},
])
def test_invalid_device_is_rejected(setup, changes):
    c, path, ex, env = setup
    assert c.post("/api/config/ui-devices", json={**record(ex), **changes}).status_code == 400
    assert db.list_ui_devices(path) == []


def test_binding_toggle_and_legacy_plan_start_guard(setup):
    c, path, ex, env = setup
    row = save(c, ex)
    body = plan(ex, env, row["id"])
    pid = c.post("/api/plans", json=body).json()["id"]
    other = db.create_executor(path, dict(name="other", python_executable=sys.executable,
        python_ref="anaconda", allowed_runners=["airtest"], enabled=1))
    assert c.post("/api/plans", json={**body, "executor_id": other}).status_code == 400
    assert not c.post("/api/plans/precheck", json={**body, "executor_id": other}).json()["ok"]
    c.post(f"/api/config/ui-devices/{row['id']}/toggle")
    assert c.post(f"/api/plans/{pid}/run").status_code == 400
    assert not c.post("/api/plans/precheck", json=body).json()["ok"]
    assert not db.list_runs_meta(path)
    c.post(f"/api/config/ui-devices/{row['id']}/toggle")
    assert c.post(f"/api/plans/{pid}/run").status_code == 200
    db.update_executor(path, ex, {"allowed_runners": ["pytest"]})
    assert c.post(f"/api/plans/{pid}/run").status_code == 400


def test_device_snapshot_fingerprint_and_retry(setup, tmp_path):
    from app.executors.airtest_runner import AirtestRunner
    c, path, ex, env = setup
    row = save(c, ex)
    c.put(f"/api/config/ui-devices/{row['id']}", json={"smoke_selector": "BeginBtn", "smoke_result": "Next"})
    body = plan(ex, env, row["id"])
    pid = c.post("/api/plans", json=body).json()["id"]
    run = c.post(f"/api/plans/{pid}/run").json()["run_id"]
    job = db.list_jobs_by_run(path, run)[0]
    original = json.loads(json.dumps(job["config_snapshot"]))
    ref = f"ui-device-{row['id']}"
    fp = device_fingerprint(ref, path)
    c.put(f"/api/config/ui-devices/{row['id']}", json={"notes": "new note", "name": "renamed"})
    assert device_fingerprint(ref, path) == fp
    c.put(f"/api/config/ui-devices/{row['id']}", json={"port": 5002})
    assert device_fingerprint(ref, path) != fp
    result = AirtestRunner().run({**job, "_db_path": path}, {}, {}, str(tmp_path / "out/junit.xml"), lambda _: None, lambda: False)
    assert result.status == "failed" and "已变化" in result.error
    assert db.get_job(path, job["id"])["config_snapshot"] == original
    assert original["ui_device"]["port"] == 5001


WINDOW = dict(hwnd=101, title="ColyseusTechDemo-MMO - Login - Unity", process_name="Unity.exe", window_class="UnityWndClass")


def test_window_rule_and_fresh_handles():
    config = record(1)
    assert resolve_window(config, [WINDOW])["hwnd"] == 101
    assert resolve_window(config, [{**WINDOW, "hwnd": 202}])["hwnd"] == 202
    assert not matching_windows({**config, "match_mode": "exact"}, [WINDOW])
    assert not matching_windows({**config, "process_name": "Other.exe"}, [WINDOW])
    assert not matching_windows({**config, "window_class": "Other"}, [WINDOW])
    for rows in ([], [WINDOW, {**WINDOW, "hwnd": 202}]):
        with pytest.raises(RuntimeError, match="必须恰好 1 个"):
            resolve_window(config, rows)


def test_readonly_probe_uses_bound_executor(setup, monkeypatch):
    from app.api import configuration as api
    c, path, ex, env = setup
    row = save(c, ex)
    monkeypatch.setattr(api, "precheck_device", lambda *a: {"ok": True, "checks": [], "missing": [], "next_steps": []})
    calls = []
    def probe(executor, config):
        calls.append((executor["id"], config))
        return {"windows": [WINDOW]}
    monkeypatch.setattr(api, "probe_windows", probe)
    response = c.post(f"/api/config/ui-devices/{row['id']}/probe").json()
    assert response["ok"] and response["windows"][0]["hwnd"] == 101
    assert calls[0][0] == ex
    assert c.post("/api/config/ui-windows/probe", json={"executor_id": ex}).status_code == 200
    assert calls[-1][1] == {}  # Inventory never accepts client-supplied commands.


@pytest.mark.parametrize("xid", [0, -1, False, None, "2", 2.5, 99999])
def test_window_inventory_rejects_missing_executor_before_probe(setup, monkeypatch, xid):
    from app.api import configuration as api
    c, path, ex, env = setup
    def unexpected_probe(*args):
        pytest.fail("Invalid executor must never start window enumeration")
    monkeypatch.setattr(api, "probe_windows", unexpected_probe)
    response = c.post("/api/config/ui-windows/probe", json={"executor_id": xid})
    assert response.status_code == 400, response.text
    assert "执行机" in response.json()["detail"]


def test_probe_service_reports_missing_executor():
    from app.ui_devices import probe_windows
    with pytest.raises(ValueError, match="请先选择关联执行机"):
        probe_windows(None, {})


def test_readiness_requires_enabled_device_on_usable_executor(setup, monkeypatch):
    c, path, ex, env = setup
    row = save(c, ex)
    monkeypatch.setattr(cc, "_run_py", lambda *a: (0, "ok"))
    def ready():
        return next(e for e in cc.engine_readiness(db, path) if e["engine"] == "airtest")
    assert ready()["runnable"]
    db.update_ui_device(path, row["id"], {"enabled": 0})
    assert not ready()["runnable"]
    db.update_ui_device(path, row["id"], {"enabled": 1})
    db.update_executor(path, ex, {"enabled": 0})
    assert not ready()["runnable"]


def test_persistent_device_reaches_real_worker(setup, fake_device, tmp_path):
    from app.executors.airtest_runner import AirtestRunner
    c, path, ex, env = setup
    row = save(c, ex)
    root, params = fake_device
    params["device"] = f"ui-device-{row['id']}"
    libs = root.parent / "libs"
    (libs / "sitecustomize.py").write_text("import app.ui_windows\napp.ui_windows.enumerate_windows=lambda: " + repr([WINDOW]), encoding="utf-8")
    (root / "test_ui.py").write_text("import os\ndef test_uri(platform_poco):\n assert os.environ['POCO_DEVICE']=='Windows:///101'\n", encoding="utf-8")
    result = AirtestRunner().run({"params": params, "executor_id": ex, "_db_path": path}, {},
        {"python_executable": sys.executable}, str(tmp_path / "output/junit.xml"), lambda _: None, lambda: False)
    assert result.status == "success", result.error
    assert result.engine_data["target_window"]["hwnd"] == 101
    assert result.engine_data["connection_cleanup"]["state"] == "closed"


@pytest.mark.parametrize("count", [0, 2])
def test_ambiguous_window_fails_before_connect(setup, fake_device, tmp_path, count):
    from app.executors.airtest_runner import AirtestRunner
    c, path, ex, env = setup
    row = save(c, ex)
    root, params = fake_device
    params["device"] = f"ui-device-{row['id']}"
    (root.parent / "libs/sitecustomize.py").write_text("import app.ui_windows\napp.ui_windows.enumerate_windows=lambda: " + repr([WINDOW] * count), encoding="utf-8")
    (root / "test_ui.py").write_text("def test_never(): raise AssertionError('should not run')", encoding="utf-8")
    result = AirtestRunner().run({"params": params, "executor_id": ex, "_db_path": path}, {},
        {"python_executable": sys.executable}, str(tmp_path / "out/junit.xml"), lambda _: None, lambda: False)
    assert result.status == "failed" and result.engine_data["phase"] == "prepare"
    assert "必须恰好 1 个" in result.error
    assert result.engine_data["connection_cleanup"]["state"] == "not_connected"
