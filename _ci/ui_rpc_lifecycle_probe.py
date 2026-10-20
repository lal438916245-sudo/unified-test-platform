"""Read-only RPC lifecycle experiment. Never clicks or changes the game page."""
import argparse
import json
from pathlib import Path
import time
import traceback


def main():
    p = argparse.ArgumentParser()
    p.add_argument("close", choices=["abrupt", "graceful"])
    p.add_argument("--output", required=True)
    args = p.parse_args()
    from poco.utils.simplerpc.rpcclient import RpcClient
    from poco.utils.simplerpc.transport.tcp.main import TcpClient
    rpc = RpcClient(TcpClient(("127.0.0.1", 5001)))
    result = {"close": args.close, "calls": [], "errors": []}
    try:
        rpc.connect()
        for method in ("GetSDKVersion", "Dump"):
            started = time.monotonic()
            value, error = rpc.call(method).wait(timeout=5)
            item = {"method": method, "seconds": round(time.monotonic() - started, 3), "error": error}
            if method == "Dump":
                text = json.dumps(value, ensure_ascii=False)
                item.update(nonempty=bool(value), BeginBtn='BeginBtn' in text, SignUp='Sign Up' in text)
            else:
                item["value"] = value
            result["calls"].append(item)
    except Exception:
        result["errors"].append(traceback.format_exc())
    finally:
        if args.close == "graceful":
            try:
                rpc.close()
                result["closed"] = True
            except Exception:
                result["errors"].append(traceback.format_exc())
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
