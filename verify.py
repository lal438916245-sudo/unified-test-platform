"""端到端验证脚本：假定服务端已在 http://127.0.0.1:8000 运行。
依次验证 计划列表 → 触发(demo计划) → 轮询到终态 → 日志 → 报告 → 产物。
用法：python verify.py [--base http://127.0.0.1:8000]"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def req(base, path, method="GET", data=None):
    url = base + path
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(url, data=body, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=15) as resp:
        return json.loads(resp.read().decode())


def wait_terminal(base, run_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = req(base, f"/api/runs/{run_id}")
        if all(j["status"] in ("success", "failed", "cancelled", "timedout") for j in data["jobs"]):
            return data
        time.sleep(1)
    raise TimeoutError("run 未在超时内结束")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--run-id", type=int, default=None, help="复用指定 run 而非新触发")
    ap.add_argument("--trigger", action="store_true", help="触发 demo 计划(默认)"
                                                            "；否则用已有 run")
    args = ap.parse_args()

    base = args.base
    failed = False

    def check(name, cond, extra=""):
        nonlocal failed
        mark = "[PASS]" if cond else "[FAIL]"
        if not cond:
            failed = True
        print(f"{mark} {name} {extra}")

    print("== 1. 计划列表 ==")
    plans = req(base, "/api/plans")
    check("获取计划", len(plans) >= 2, f"({len(plans)} 个)")
    demo = next((p for p in plans if "自检" in p["name"]), plans[0])
    real = next((p for p in plans if "API 冒烟" in p["name"]), None)

    print("== 2. 触发 demo 计划 ==")
    if args.run_id:
        run_id = args.run_id
    else:
        out = req(base, f"/api/plans/{demo['id']}/run", "POST", {})
        run_id = out["run_id"]
    print(f"   run_id={run_id}")

    print("== 3. 轮询到终态 ==")
    data = wait_terminal(base, run_id)
    job = data["jobs"][0]
    print(f"   job#{job['id']} status={job['status']} exit={job['exit_code']}")
    check("demo Job 成功", job["status"] == "success", f"status={job['status']}")

    print("== 4. 日志 ==")
    logs = req(base, f"/api/jobs/{job['id']}/logs")
    text = "".join(logs["logs"])
    check("有增量日志", len(logs["logs"]) > 0 and "test" in text.lower(), f"({len(text)} 字符)")

    print("== 5. 报告 + 校验 ==")
    rep = req(base, f"/api/reports/{job['id']}")
    rj = json.loads(rep["report_json"])
    need = ["schema_version", "plan_run_id", "job_id", "engine", "status",
            "started_at", "ended_at", "environment_snapshot", "summary", "metrics",
            "engine_data_schema", "artifacts"]
    missing = [k for k in need if k not in rj]
    check("report 字段齐全", not missing, f"missing={missing or '无'}")
    check("schema_version=1.0", rj.get("schema_version") == "1.0")
    check("summary.passed>=1", (rj.get("summary") or {}).get("passed", 0) >= 1,
          f"summary={rj.get('summary')}")
    ok_art = isinstance(rj.get("artifacts"), list) and rj["artifacts"] and \
        all(a in rj["artifacts"][0] for a in ("name", "type", "path", "size"))
    check("artifacts 为对象数组(name/type/path/size)", bool(ok_art),
          f"artifacts={rj.get('artifacts')}")

    print("== 6. 产物访问 ==")
    art = rj["artifacts"][0]
    status = urllib.request.urlopen(base + f"/api/artifacts/{job['id']}/{art['path']}",
                                    timeout=15).status
    check("产物可下载", status == 200, f"HTTP {status} {art['name']}")

    print("== 7. 失败场景(真实 API 冒烟，若服务端未起则为 failed) ==")
    if real:
        out = req(base, f"/api/plans/{real['id']}/run", "POST", {})
        r2 = wait_terminal(base, out["run_id"])
        jb = r2["jobs"][0]
        check("真实路径能归档(成功或失败)", jb["status"] in ("success", "failed"),
              f"status={jb['status']} exit={jb['exit_code']}")
        # exit=-1 表示 Runner 自身异常(如 cwd 不存在)，是平台缺陷，不是被测结果
        check("不是 Runner 异常(-1)", jb["exit_code"] != -1,
              f"exit={jb['exit_code']} error={jb.get('error')}")

    print("\n========== 结果 ==========")
    print("ALL PASS" if not failed else "HAS FAILURE")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())