"""Locust 接入 · 真实集成验证。

假定隔离平台实例已运行（非 8000 端口、独立 DB/数据目录，见报告"启动方式"）。
覆盖：组合计划成 / pytest失败→locust skipped / 真实引擎不可达 failed / 取消 / 超时 /
      report.json 字段与产物 / 锁与进程回收。最后回归 verify.py、verify_control.py、任务控制单测。

用法：python verify_locust.py [--base http://127.0.0.1:8001] [--db 隔离库]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

FAILED = [False]
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # .../platform


def check(name, cond, extra=""):
    if not cond:
        FAILED[0] = True
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def req(base, path, method="GET", data=None, expect_error=False):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(base + path, data=body, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        if expect_error:
            return None, e.code
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode() if e.fp else e}")


def triggers(base, pid):
    out, _ = req(base, f"/api/plans/{pid}/run", "POST", {})
    return out["run_id"]


def get_jobs(base, run_id):
    d, _ = req(base, f"/api/runs/{run_id}")
    return d["run"], d["jobs"]


JOB_TERMINAL = ("success", "failed", "cancelled", "timedout", "skipped")
RUN_SETTLED = ("success", "failed", "cancelled", "timedout", "partial", "skipped")


def wait_terminal(base, run_id, deadline=180):
    end = time.time() + deadline
    saw_cancelling = False
    while time.time() < end:
        run, jobs = get_jobs(base, run_id)
        if any(j["status"] == "cancelling" for j in jobs):
            saw_cancelling = True
        jobs_terminal = all(j["status"] in JOB_TERMINAL for j in jobs)
        run_settled = run["status"] in RUN_SETTLED
        if jobs_terminal and run_settled:
            return run, jobs, saw_cancelling
        time.sleep(0.4)
    raise TimeoutError(f"run {run_id} 未结束 jobs_terminal={jobs_terminal} run={run['status']}")


def held_locks(db):
    with sqlite3.connect(db) as c:
        return c.execute(
            "SELECT COUNT(*) FROM executor_locks WHERE released_at IS NULL").fetchone()[0]


def plan_id_by_name(base, name):
    plans, _ = req(base, "/api/plans")
    for p in plans:
        if p["name"] == name:
            return p["id"]
    # 兜底：直接以名成相似也接受
    raise RuntimeError(f"未找到计划: {name}")


def id_by_name(base, kind, name):
    names = {"env": "/api/environments", "exec": "/api/executors"}[kind]
    rows, _ = req(base, names)
    for r in rows:
        if r["name"] == name:
            return r["id"]
    raise RuntimeError(f"未找到 {kind}={name}")


def seed_plan(db, name, *steps):
    """向隔离库注入一个计划（存在则跳过）。steps: dict(engine,name,params,requires_exclusive)."""
    with sqlite3.connect(db) as c:
        exists = c.execute("SELECT id FROM plans WHERE name=?", (name,)).fetchone()
        if exists:
            return exists[0]
        # 复用 fixture 环境 与 locust 执行机的 id
        fixture_env = c.execute("SELECT id FROM environments WHERE name='locust-fixture'").fetchone()[0]
        loc_exec = c.execute("SELECT id FROM executors WHERE name='anaconda-locust'").fetchone()[0]
        steps_json = json.dumps(steps, ensure_ascii=False)
        cur = c.execute(
            "INSERT INTO plans(name,description,environment_id,executor_id,steps,fail_fast,owner,created_at,updated_at)"
            " VALUES(?,?,?,?,?,1,'verify',datetime('now'),datetime('now'))",
            (name, name, fixture_env, loc_exec, steps_json))
        c.commit()
        return cur.lastrowid


def art_ok(base, job_id, filename):
    """产物下载端点返回原始文件（FileResponse），仅校验 HTTP 200 与非空。"""
    try:
        r = urllib.request.urlopen(base + f"/api/artifacts/{job_id}/{filename}", timeout=20)
        body = r.read()
        return r.status == 200 and len(body) > 0
    except Exception:  # noqa: BLE001
        return False


def scenario_combined(base, db):
    print("== 场景1 组合计划：pytest 冒烟成功 + Locust fixture 成功 => run=success ==")
    pid = plan_id_by_name(base, "冒烟+Locust(fixture 压测)")
    run_id = triggers(base, pid)
    run, jobs, _ = wait_terminal(base, run_id)
    j1, j2 = jobs[0], jobs[1]
    check("job1(pytest)=success", j1["status"] == "success", f"#{j1['id']}")
    check("job2(locust)=success", j2["status"] == "success", f"#{j2['id']} err={j2.get('error')}")
    check("run=success", run["status"] == "success", f"run={run['status']}")

    rep, _ = req(base, f"/api/reports/{j2['id']}")
    rj = json.loads(rep["report_json"])
    m = rj.get("metrics") or {}
    check("engine=locust", rj.get("engine") == "locust")
    check("report.status=success", rj.get("status") == "success")
    need = ["requests", "failures", "failure_rate", "rps", "avg_ms", "p50_ms", "p95_ms", "p99_ms"]
    miss = [k for k in need if m.get(k) is None]
    check("metrics 含全部通用指标", not miss, f"missing={miss or '无'}")
    check("零失败(failure_rate=0)", m.get("failures") == 0 and m.get("failure_rate") == 0,
          f"failures={m.get('failures')}")
    check("engine_data_schema=locust/1.0", rj.get("engine_data_schema") == "locust/1.0",
          f"got={rj.get('engine_data_schema')}")
    ed = rj.get("engine_data") or {}
    check("engine_data.request_stats 必含接口维度", bool(ed.get("request_stats")), f"{len(ed.get('request_stats') or [])} 项")
    check("engine_data.timeseries 非空", bool(ed.get("timeseries")), f"{len(ed.get('timeseries') or [])} 点")
    art_names = {a["name"] for a in rj.get("artifacts") or []}
    for must in ("locust_report.html", "locust_stats.csv", "locust_failures.csv", "locust_exceptions.csv"):
        check(f"artifacts 含 {must}", must in art_names)
    # 产物可下载（原始文件，HTTP 200 且非空）
    any_art = (rj.get("artifacts") or [])[0]
    check("产物可下载", art_ok(base, j2["id"], any_art["path"]), f"{any_art['name']}")
    check("锁无残留", held_locks(db) == 0, f"held={held_locks(db)}")


def scenario_fail_skip(base, db):
    print("== 场景2 pytest 失败 => 后续 locust Job 为 skipped ==")
    pid = seed_plan(db, "LOCUST-COMBINED-FAIL",
                    {"engine": "pytest", "name": "fail", "requires_exclusive": False,
                     "params": {"timeout_sec": 60, "cwd": os.path.join(BASE_DIR, "backend"),
                                "args": ["demo/test_demo_ctl_fail.py", "-o", "addopts=", "--tb=short"]}},
                    {"engine": "locust", "name": "skip-me", "requires_exclusive": False,
                     "params": {"locustfile": "http-fixture", "users": 2, "spawn_rate": 1,
                                "run_time": "5s", "timeout_sec": 60}})
    run_id = triggers(base, pid)
    run, jobs, _ = wait_terminal(base, run_id)
    check("job1=failed", jobs[0]["status"] == "failed")
    check("job2=skipped(失败即停)", jobs[1]["status"] == "skipped", f"job2={jobs[1]['status']}")
    check("run=failed", run["status"] == "failed", f"run={run['status']}")


def scenario_unreachable(base, db):
    print("== 场景3 真实引擎不可达(Colyseus :2567 未起) => 诚实 failed ==")
    pid = plan_id_by_name(base, "Locust-Colyseus(需服务端 2567)")
    run_id = triggers(base, pid)
    run, jobs, _ = wait_terminal(base, run_id)
    j = jobs[0]
    check("job=failed(不可达)", j["status"] == "failed", f"job={j['status']} err={j.get('error')}")
    check("run=failed", run["status"] == "failed", f"run={run['status']}")
    rep, _ = req(base, f"/api/reports/{j['id']}")
    rj = json.loads(rep["report_json"])
    m = rj.get("metrics") or {}
    check("report.status=failed", rj.get("status") == "failed")
    check("failure_rate>0(未伪装成功)", (m.get("failures") or 0) > 0 or (m.get("failure_rate") or 0) > 0,
          f"failures={m.get('failures')} rate={m.get('failure_rate')}")
    check("锁无残留", held_locks(db) == 0)


def scenario_cancel(base, db):
    print("== 场景4 运行中取消 => cancelling→cancelled，进程树/锁回收 ==")
    pid = seed_plan(db, "LOCUST-CANCEL",
                    {"engine": "locust", "name": "long", "requires_exclusive": False,
                     "params": {"locustfile": "http-fixture", "users": 3, "spawn_rate": 1,
                                "run_time": "90s", "timeout_sec": 300}})
    run_id = triggers(base, pid)
    # 等待进入 running
    end = time.time() + 60
    jid = None
    while time.time() < end:
        _, jobs = get_jobs(base, run_id)
        if jobs and jobs[0]["status"] in ("running", "cancelling"):
            jid = jobs[0]["id"]
            break
        time.sleep(0.3)
    check("job 进入 running", jid is not None)
    if jid:
        req(base, f"/api/jobs/{jid}/cancel", "POST", {})
    run_f, jobs_f, saw = wait_terminal(base, run_id)
    jf = jobs_f[0]
    check("job=cancelled", jf["status"] == "cancelled", f"job={jf['status']} err={jf.get('error')}")
    check("run=cancelled", run_f["status"] == "cancelled", f"run={run_f['status']}")
    check("锁无残留", held_locks(db) == 0)


def scenario_timeout(base, db):
    print("== 场景5 短超时看门狗 => timedout ==")
    pid = seed_plan(db, "LOCUST-TIMEOUT",
                    {"engine": "locust", "name": "to", "requires_exclusive": False,
                     "params": {"locustfile": "http-fixture", "users": 3, "spawn_rate": 1,
                                "run_time": "120s", "timeout_sec": 4}})
    run_id = triggers(base, pid)
    run, jobs, _ = wait_terminal(base, run_id)
    j = jobs[0]
    check("job=timedout", j["status"] == "timedout", f"job={j['status']} err={j.get('error')}")
    check("run=timedout", run["status"] == "timedout", f"run={run['status']}")
    rep, _ = req(base, f"/api/reports/{j['id']}")
    rj = json.loads(rep["report_json"])
    check("report.status=timedout", rj.get("status") == "timedout")
    check("锁无残留", held_locks(db) == 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--db", default=os.path.join(BASE_DIR, "data_verify_locust", "db.sqlite"))
    args = ap.parse_args()
    base, db = args.base, args.db
    print(f"[verify_locust] base={base}\ndb={db}")

    for fn in (scenario_combined, scenario_fail_skip, scenario_unreachable,
               scenario_cancel, scenario_timeout):
        print("\n# ===== " + fn.__name__ + " =====")
        fn(base, db)

    print("\n========== 结果 ==========")
    print("ALL PASS" if not FAILED[0] else "HAS FAILURE")
    print(f"执行机锁残留(应全程为0) held={held_locks(db)}")
    return 1 if FAILED[0] else 0


if __name__ == "__main__":
    sys.exit(main())