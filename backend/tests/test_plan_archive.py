"""Reversible plan lifecycle, immutable history and atomic execution guards."""
import concurrent.futures
import json
from pathlib import Path
import sqlite3
import sys
import threading

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db
from app.api import build_app
from app.config_center import build_plan_snapshot
from app.orchestration.scheduler import Orchestrator


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = str(tmp_path / "db.sqlite")
    db.init_db(path)
    env = db.create_environment(path, dict(name="offline", kind="offline", host="", port=0, enabled=1))
    ex = db.create_executor(path, dict(name="python", python_executable=sys.executable,
                                     cwd_root="backend-demo", allowed_runners=["pytest"], enabled=1))
    app = build_app(str(tmp_path / "static"))
    app.state.db_path = path
    app.state.orchestrator = Orchestrator(path)  # Never started: API-only lifecycle tests.
    from app.api import router as api
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path / "data"))
    body = dict(name="Keep history", environment_id=env, executor_id=ex,
                steps=[dict(engine="pytest", name="demo", params=dict(args=["demo/test_platform_demo.py"]))])
    with TestClient(app) as c:
        pid = c.post("/api/plans", json=body).json()["id"]
        yield c, path, pid, body, tmp_path


def history(setup):
    c, path, pid, body, tmp = setup
    run = c.post(f"/api/plans/{pid}/run").json()["run_id"]
    job = db.list_jobs_by_run(path, run)[0]
    db.update_job(path, job["id"], dict(status="success", exit_code=0))
    db.update_run_status(path, run, "success", ended=True)
    artifact = tmp / "data/reports" / f"run_{run}/job_{job['id']}/evidence.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("preserved evidence", encoding="utf-8")
    db.save_report(path, job["id"], run, "report.json", dict(engine="pytest", status="success"))
    db.upsert_report_index(path, dict(job_id=job["id"], plan_id=pid, plan_run_id=run,
                                    plan_name=body["name"], engine="pytest", status="success"))
    return run, job["id"]


def test_archive_restore_filter_idempotence_and_definition_stability(setup):
    c, path, pid, body, _ = setup
    original = db.get_plan(path, pid)
    fingerprint = build_plan_snapshot(original)["snapshot_fingerprint"]
    archived = c.post(f"/api/plans/{pid}/archive")
    assert archived.status_code == 200 and archived.json()["archived"]
    assert archived.json()["archived_at"]
    assert c.get("/api/plans").json() == []
    assert [p["id"] for p in c.get("/api/plans?status=archived").json()] == [pid]
    assert [p["id"] for p in c.get("/api/plans?status=all").json()] == [pid]
    assert len(db.list_plans(path)) == 1  # Reference and seed scans retain archived records.
    assert c.post(f"/api/plans/{pid}/archive").json() == archived.json()
    assert c.get("/api/plans?status=invalid").status_code == 422
    restored = c.post(f"/api/plans/{pid}/restore").json()
    assert restored["archived"] is False and restored["archived_at"] is None
    assert c.get("/api/plans?status=archived").json() == []
    assert c.post(f"/api/plans/{pid}/restore").json() == restored
    for key in ("steps", "revision", "name", "created_at", "updated_at"):
        assert original[key] == restored[key] == archived.json()[key]
    assert build_plan_snapshot(restored)["snapshot_fingerprint"] == fingerprint
    assert len(c.get("/api/plans").json()) == 1
    assert c.post(f"/api/plans/{pid}/run").status_code == 200


def test_archived_history_report_artifact_remain_readable_and_retry_guarded(setup):
    c, path, pid, body, _ = setup
    run, job = history(setup)
    old_run, old_job, old_report = db.get_run(path, run), db.get_job(path, job), db.get_report(path, job)
    old_index = c.get("/api/report-center").json()
    assert c.post(f"/api/plans/{pid}/archive").status_code == 200
    assert c.post(f"/api/plans/{pid}/run").status_code == 409
    assert c.put(f"/api/plans/{pid}", json={**body, "name": "must not save"}).status_code == 409
    assert c.post(f"/api/jobs/{job}/retry").status_code == 409
    assert db.get_run(path, run) == old_run and db.get_job(path, job) == old_job
    assert db.get_report(path, job) == old_report
    assert c.get(f"/api/runs/{run}").json()["plan_archived"] is True
    assert c.get(f"/api/reports/{job}").json() == old_report
    assert c.get(f"/api/artifacts/{job}/evidence.txt").text == "preserved evidence"
    assert c.get("/api/report-center").json() == old_index
    assert c.get("/api/runs").json()[0]["plan_name"] == body["name"]
    copy = c.post(f"/api/plans/{pid}/copy", json=dict(name="Active copy"))
    assert copy.status_code == 200
    copied = db.get_plan(path, copy.json()["id"])
    assert copied["archived"] is False and copied["revision"] == 1
    assert copied["steps"] == db.get_plan(path, pid)["steps"]
    c.post(f"/api/plans/{pid}/restore")
    assert c.put(f"/api/plans/{pid}", json=body).status_code == 200
    assert c.post(f"/api/jobs/{job}/retry").status_code == 200


@pytest.mark.parametrize("run_status,job_status", [
    ("queued", "queued"), ("running", "running"), ("running", "cancelling"), ("success", "queued")])
def test_active_run_or_retry_prevents_archive(setup, run_status, job_status):
    c, path, pid, _, _ = setup
    run, job = history(setup)
    db.update_run_status(path, run, run_status)
    db.update_job(path, job, dict(status=job_status))
    before = db.get_plan(path, pid)
    response = c.post(f"/api/plans/{pid}/archive")
    assert response.status_code == 409 and "未结束" in response.json()["detail"]
    assert db.get_plan(path, pid) == before


@pytest.mark.parametrize("status", ["success", "failed", "cancelled", "timedout", "partial"])
def test_terminal_runs_do_not_block_archive(setup, status):
    c, path, pid, _, _ = setup
    run, job = history(setup)
    db.update_run_status(path, run, status)
    assert c.post(f"/api/plans/{pid}/archive").status_code == 200


def test_archive_and_new_execution_are_atomic(setup):
    c, path, pid, _, _ = setup
    barrier = threading.Barrier(2)
    def request(action):
        barrier.wait(timeout=5)
        return c.post(f"/api/plans/{pid}/{action}").status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, ("archive", "run")))
    assert sorted(results) == [200, 409]
    plan = db.get_plan(path, pid)
    assert (len(db.list_runs_meta(path)) == 0) if plan["archived"] else (len(db.list_runs_meta(path)) == 1)


def test_archive_and_retry_are_atomic(setup):
    c, path, pid, _, _ = setup
    run, job = history(setup)
    barrier = threading.Barrier(2)
    def request(archive):
        barrier.wait(timeout=5)
        return c.post(f"/api/plans/{pid}/archive" if archive else f"/api/jobs/{job}/retry").status_code
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(request, (True, False)))
    assert sorted(results) == [200, 409]
    assert len(db.list_jobs_by_run(path, run)) == (1 if db.get_plan(path, pid)["archived"] else 2)


def test_missing_plan_and_legacy_database_migration(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE plans(id INTEGER PRIMARY KEY,name TEXT,description TEXT,environment_id INTEGER,executor_id INTEGER,"
                  "asset_source_id INTEGER,steps TEXT,fail_fast INTEGER,owner TEXT,revision INTEGER,created_at TEXT,updated_at TEXT)")
        c.execute("INSERT INTO plans VALUES(1,'legacy','keep',1,1,NULL,'[]',1,'local',7,'before','before')")
    db.init_db(path)
    assert db.get_plan(path, 1)["archived"] is False
    assert db.set_plan_archived(path, 1, True)
    db.init_db(path)
    row = db.get_plan(path, 1)
    assert row["archived"] and row["revision"] == 7 and row["updated_at"] == "before" and row["steps"] == []
    app = build_app(str(tmp_path / "static"))
    app.state.db_path = path
    with TestClient(app) as c:
        assert c.post("/api/plans/9999/archive").status_code == 404
        assert c.post("/api/plans/9999/restore").status_code == 404


def test_archived_case_reference_still_prevents_case_deletion(setup):
    c, path, pid, body, _ = setup
    case = c.post("/api/test-cases", json=dict(name="Shared", engine="pytest", params=dict(args=["demo/test_platform_demo.py"]))).json()
    ref = {k: case[k] for k in ("id", "revision", "fingerprint")}
    response = c.put(f"/api/plans/{pid}", json={**body, "steps": [dict(case_ref=ref)]})
    assert response.status_code == 200
    assert c.post(f"/api/plans/{pid}/archive").status_code == 200
    assert c.delete(f"/api/test-cases/{case['id']}").status_code == 409
