"""Read-only worker experiment: production close or historical abrupt exit."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("close", choices=["production", "abrupt", "graceful"])
    p.add_argument("--output", required=True)
    p.add_argument("--db", help="Read a persisted UI device from this platform database")
    p.add_argument("--device", help="Persisted key, e.g. ui-device-1; requires --db")
    args = p.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    suite = output / "read-only-suite"
    suite.mkdir(exist_ok=True)
    (suite / "test_read_only.py").write_text("def test_read_only(platform_poco):\n assert platform_poco.agent.hierarchy.dump()\n", encoding="utf-8")
    input_path = output / ".input.json"
    config = {"root": str(suite), "case": "test_read_only.py", "wait_timeout": 10,
              "junit": str(output / "junit.xml"), "output": str(output),
              "device_config": {"uri": "Windows:///923030", "unity_editor": False, "host": "127.0.0.1", "port": 5001}}
    if args.db:
        from app.ui_config import get_device, device_binding_errors
        device = get_device(args.device, str(Path(args.db).resolve()))
        errors = device_binding_errors(device, None, str(Path(args.db).resolve()))
        if errors:
            raise ValueError("；".join(errors))
        config["device_config"] = device
    elif args.device:
        p.error("--device requires --db")
    input_path.write_text(json.dumps(config), encoding="utf-8")
    from app.executors import ui_worker
    if args.close == "abrupt":
        # Explicit historical reproduction only; never used for production QA.
        ui_worker.UIPlugin.close = lambda plugin: None
    sys.argv = [sys.argv[0], str(input_path)]
    try:
        return ui_worker.main()
    finally:
        input_path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
