"""Explicit Colyseus Unity acceptance in an isolated platform database.

inspect: enumerate one intended Unity window, connect Poco, save evidence.
run: execute the registered Begin -> Sign Up case through API + orchestrator.
"""
from pathlib import Path
import argparse
import json
import os
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["inspect", "run", "negative"])
    parser.add_argument("--output", default=str(ROOT / "_ci_out" / "ui_real_20261013"))
    parser.add_argument("--label", default="", help="Unique case suffix for additional attempts")
    args = parser.parse_args()
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=True)
    import win32gui
    import win32process
    import psutil
    windows = []
    def collect(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if win32gui.IsWindowVisible(hwnd) and "ColyseusTechDemo-MMO" in title:
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            if psutil.Process(pid).name().lower() == "unity.exe":
                windows.append({"hwnd": hwnd, "title": title})
    win32gui.EnumWindows(collect, None)
    if len(windows) != 1:
        raise RuntimeError(f"Expected one Colyseus Unity window, found {windows!r}")
    os.environ["PLATFORM_UI_DEVICE_URI"] = "Windows:///" + str(windows[0]["hwnd"])
    os.environ["PLATFORM_UI_POCO_HOST"] = "127.0.0.1"
    os.environ["PLATFORM_UI_POCO_PORT"] = "5001"
    os.environ["PLATFORM_UI_UNITY_EDITOR"] = "1"
    os.environ["PLATFORM_DATA_DIR"] = str(out / "data")
    from app.ui_config import UI_DEVICES, UI_SUITES
    params = {"suite": "colyseus-ui-smoke", "case": "test_colyseus_ui.py",
              "device": "unity-windows", "wait_timeout": 15, "timeout_sec": 90, "reset": "none"}
    if args.mode == "inspect":
        from app.executors.ui_worker import UIPlugin
        from airtest.core.api import snapshot
        plugin = UIPlugin(out, {"root": UI_SUITES[params["suite"]]["root"],
                               "device_config": UI_DEVICES[params["device"]], "wait_timeout": 15})
        plugin.prepare()
        tree = plugin.poco.agent.hierarchy.dump()
        (out / "before-hierarchy.json").write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
        snapshot(filename=str(out / "before-screen.png"))
        summary = {"window": windows[0], "BeginBtn": plugin.poco("BeginBtn").exists(),
                   "ServerInput": plugin.poco("ServerInput").exists(), "PortInput": plugin.poco("PortInput").exists(),
                   "Sign Up": plugin.poco(text="Sign Up").exists()}
        if summary["BeginBtn"]:
            summary["BeginBtn_pos"] = plugin.poco("BeginBtn").attr("pos")
        (out / "inspection.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))
        return
    from app import db
    from app.api import build_app
    from app.orchestration.scheduler import Orchestrator
    from fastapi.testclient import TestClient
    path = out / "data" / "db.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(str(path))
    env = db.create_environment(str(path), {"name": "colyseus-ui-acceptance", "host": "127.0.0.1", "port": 2567,
                                          "kind": "colyseus", "protocol": "http", "enabled": 1})
    executor = db.create_executor(str(path), {"name": "ui-acceptance-python", "python_executable": sys.executable,
        "python_ref": "", "cwd_root": "backend-demo", "allowed_runners": ["airtest"], "enabled": 1})
    app = build_app(str(ROOT / "frontend"))
    app.state.db_path = str(path)
    orch = Orchestrator(str(path))
    app.state.orchestrator = orch
    with TestClient(app) as client:
        name = "Colyseus Begin real" if args.mode == "run" else "Colyseus Begin negative"
        if args.label:
            name += " " + args.label
        case = client.post("/api/test-cases", json={"name": name, "engine": "airtest", "params": params})
        case.raise_for_status()
        tc = client.get("/api/test-cases/" + str(case.json()["id"])).json()
        body = {"name": "Colyseus UI real acceptance", "environment_id": env, "executor_id": executor,
                "steps": [{"engine": "airtest", "case_ref": {k: tc[k] for k in ("id", "revision", "fingerprint")}}]}
        pc = client.post("/api/plans/precheck", json=body)
        pc.raise_for_status()
        (out / "precheck.json").write_text(json.dumps(pc.json(), ensure_ascii=False, indent=2), encoding="utf-8")
        if not pc.json()["ok"]:
            raise RuntimeError("Static precheck failed; inspect precheck.json")
        plan = client.post("/api/plans", json=body)
        plan.raise_for_status()
        run = client.post(f"/api/plans/{plan.json()['id']}/run")
        run.raise_for_status()
        job = db.list_jobs_by_run(str(path), run.json()["run_id"])[0]
        threading.Thread(target=orch._watchdog_loop, daemon=True).start()
        try:
            orch._process(job)
        finally:
            orch.shutdown()
        finished = db.get_job(str(path), job["id"])
        report_path = out / "data" / finished["report_path"]
        report = json.loads(report_path.read_text(encoding="utf-8"))
        negative_observed = report.get("engine_data", {}).get("phase") == "run" and report.get("engine_data", {}).get("failure_kind") == "test"
        summary = {"run_id": run.json()["run_id"], "job_id": job["id"], "status": finished["status"],
                   "error": finished.get("error"), "summary": report["summary"], "report_path": str(report_path),
                   "exclusive_lock_released": db.get_active_executor_lock(str(path), executor) is None}
        if args.mode == "negative":
            summary["negative_expected_failure_observed"] = negative_observed
        filename = "acceptance.json" if args.mode == "run" else "negative-acceptance.json"
        if args.label:
            filename = "acceptance-job-" + str(job["id"]) + ".json"
        (out / filename).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))
        expected = "failed" if args.mode == "negative" else "success"
        if finished["status"] != expected or (args.mode == "negative" and not negative_observed):
            sys.exit(1)


if __name__ == "__main__":
    main()
