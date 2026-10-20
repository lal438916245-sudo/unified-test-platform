"""Retry original UI acceptance jobs without changing their frozen records."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id", type=int)
    parser.add_argument("--device-uri", required=True)
    args = parser.parse_args()
    output = ROOT / "_ci_out" / "ui_real_20261013"
    os.environ.update(PLATFORM_DATA_DIR=str(output / "data"), PLATFORM_UI_DEVICE_URI=args.device_uri,
                      PLATFORM_UI_POCO_HOST="127.0.0.1", PLATFORM_UI_POCO_PORT="5001", PLATFORM_UI_UNITY_EDITOR="1")
    from app import db
    from app.orchestration.scheduler import Orchestrator
    path = str(output / "data" / "db.sqlite")
    original = db.get_job(path, args.job_id)
    if not original or original["engine"] != "airtest":
        raise ValueError("Expected an existing airtest acceptance job")
    orch = Orchestrator(path)
    retry_id = orch.retry(original)
    retry = db.get_job(path, retry_id)
    assert retry["config_snapshot"] == original["config_snapshot"]
    threading.Thread(target=orch._watchdog_loop, daemon=True).start()
    try:
        orch._process(retry)
    finally:
        orch.shutdown()
    job = db.get_job(path, retry_id)
    report_path = output / "data" / job["report_path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = {"original_job_id": args.job_id, "retry_job_id": retry_id, "status": job["status"],
               "phase": report.get("engine_data", {}).get("phase"),
               "failure_kind": report.get("engine_data", {}).get("failure_kind"),
               "summary": report["summary"], "error": job.get("error"), "report_path": str(report_path),
               "exclusive_lock_released": db.get_active_executor_lock(path, job["executor_id"]) is None}
    (output / f"retry-job-{retry_id}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
