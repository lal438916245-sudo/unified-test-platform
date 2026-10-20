"""Isolated web acceptance instance; deliberately unmatched device, no scheduler."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app import db
from app.api import build_app
from fastapi.testclient import TestClient


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--port", type=int, default=18081)
    args = parser.parse_args()
    directory = Path(args.data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    path = str(directory / "db.sqlite")
    db.init_db(path)
    app = build_app(str(ROOT / "frontend"))
    app.state.db_path = path
    if not db.list_plans(path):
        env = db.create_environment(path, dict(name="离线预检验收", kind="offline", host="", port=0, enabled=1))
        ex = db.create_executor(path, dict(name="本机 UI 预检验收", python_ref="anaconda", cwd_root="backend-demo",
                                         python_executable="", allowed_runners=["airtest"], enabled=1))
        with TestClient(app) as c:
            device = c.post("/api/config/ui-devices", json=dict(name="不存在窗口负例", executor_id=ex,
                title_match="__RUNTIME_PRECHECK_NO_REAL_WINDOW__", unity_editor=0)).json()
            req = c.post("/api/config/runtime-requirements", json=dict(name="Unity 共用验收要求", suite="unity-smoke",
                modules=["requests"], mongo_ref="", enabled=1)).json()
            case = c.post("/api/test-cases", json=dict(name="只读预检验收用例", engine="airtest", params=dict(
                suite="unity-smoke", case="test_unity_smoke.py", device=f"ui-device-{device['id']}",
                requirements=req["key"], timeout_sec=120, wait_timeout=10, reset="none"))).json()
            # Register selectors required by the controlled smoke suite, but never execute them.
            c.put(f"/api/config/ui-devices/{device['id']}", json=dict(smoke_selector="NeverClick", smoke_result="NeverRun"))
            ref = {k: case[k] for k in ("id", "revision", "fingerprint")}
            c.post("/api/suites", json=dict(name="公共要求复用验收", cases=[ref]))
            response = c.post("/api/plans", json=dict(name="客户端未启动预检验收", environment_id=env,
                executor_id=ex, steps=[dict(engine="airtest", case_ref=ref)]))
            response.raise_for_status()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
