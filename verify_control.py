"""任务控制加固 · 真实集成验证。

假定服务端已在 http://127.0.0.1:8000 运行（含预置的 CTRL 计划）。
覆盖场景：正常成功 / 故意失败 / 运行中取消 / 超时 / 超时与取消竞争 / 失败后重试成功 /
          同 Executor 独占锁互斥。全程断言 SQLite 状态、日志、report、锁释放与进程正确性。

用法：python verify_control.py [--base http://127.0.0.1:8000] [--db 数据文件路径]
默认从预置自动推导 db 路径。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request

DB_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "db.sqlite")
FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend", "demo", "retry_green.flag")
FAILED = [False]


def check(name: str, cond: bool, extra: str = ""):
    if not cond:
        FAILED[0] = True
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def req(base, path, method="GET", data=None, expect_error=False):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(base + path, data=body, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as e:
        txt = e.read().decode() if e.fp else str(e)
        if expect_error:
            return None, e.code
        raise RuntimeError(f"HTTP {e.code}: {txt}")


def get_jobs(base, run_id):
    d, _ = req(base, f"/api/runs/{run_id}")
    return d["run"], d["jobs"]


def wait_status(base, run_id, deadline=120):
    """轮询 run 与全部 job 至终态。返回 (run, jobs, 过程中是否观察到 cancelling)。

    额外要求 run 聚合已反映最新一次 Job 终态（run.ended_at 不早于所有 Job 的 ended_at）：
    重试场景下 run 可能先因原 Job 失败而落库为 failed，若在其重新聚合前就读，会读到旧值。
    """
    end = time.time() + deadline
    saw_cancelling = False
    while time.time() < end:
        run, jobs = get_jobs(base, run_id)
        if any(j["status"] == "cancelling" for j in jobs):
            saw_cancelling = True
        jobs_terminal = all(j["status"] in ("success", "failed", "cancelled", "timedout", "skipped") for j in jobs)
        latest_job_end = max((j.get("ended_at") or "" for j in jobs), default="")
        run_fresh = (run.get("ended_at") or "") >= latest_job_end
        # 等基层 run 聚合也落库（jobs 已终态但 run 状态尚未聚合/尚未刷新时，避免读早竞态）
        if jobs_terminal and run_fresh and run["status"] in ("success", "failed", "cancelled", "timedout", "partial", "skipped"):
            return run, jobs, saw_cancelling
        time.sleep(0.4)
    raise TimeoutError(f"run {run_id} 未在 {deadline}s 内结束")


def wait_any_running(base, run_id, deadline=30):
    end = time.time() + deadline
    while time.time() < end:
        run, jobs = get_jobs(base, run_id)
        if jobs and jobs[0]["status"] in ("running",) or (jobs and jobs[0]["status"] == "cancelling"):
            return jobs
        time.sleep(0.3)
    raise TimeoutError("job 未进入 running")


def plan_id_by_name(base, name):
    plans, _ = req(base, "/api/plans")
    for p in plans:
        if p["name"] == name:
            return p["id"]
    raise RuntimeError(f"未找到计划: {name}")


def trigger(base, pid):
    out, _ = req(base, f"/api/plans/{pid}/run", "POST", {})
    return out["run_id"]


def held_locks(db):
    c = sqlite3.connect(db)
    try:
        return c.execute(
            "SELECT COUNT(*) FROM executor_locks WHERE released_at IS NULL").fetchone()[0]
    finally:
        c.close()


def report_of(base, job_id):
    rep, _ = req(base, f"/api/reports/{job_id}")
    return json.loads(rep["report_json"]), rep["report_path"]


def logs_text(base, job_id):
    d, _ = req(base, f"/api/jobs/{job_id}/logs")
    return "".join(d["logs"])


def scenario_success(base, db):
    print("== 场景1 正常成功 ==")
    run_id = trigger(base, plan_id_by_name(base, "CTRL 成功"))
    run, jobs, _ = wait_status(base, run_id)
    j = jobs[0]
    check("job=success", j["status"] == "success", f"job#{j['id']}")
    check("run=success", run["status"] == "success", f"run={run['status']}")
    check("attempt=1", (j.get("attempt") or 1) == 1)
    rep, _ = report_of(base, j["id"])
    check("report.status=success", rep["status"] == "success")
    check("report.summary.passed>=1", (rep.get("summary") or {}).get("passed", 0) >= 1)
    check("锁无残留", held_locks(db) == 0, f"held={held_locks(db)}")


def scenario_fail(base, db):
    print("== 场景2 故意失败 ==")
    run_id = trigger(base, plan_id_by_name(base, "CTRL 失败"))
    run, jobs, _ = wait_status(base, run_id)
    j = jobs[0]
    check("job=failed", j["status"] == "failed", f"exit={j['exit_code']}")
    check("run=failed", run["status"] == "failed", f"run={run['status']}")
    check("exit=1 真实测试失败", j["exit_code"] == 1, f"exit={j['exit_code']}")
    rep, _ = report_of(base, j["id"])
    check("report.status=failed", rep["status"] == "failed")
    check("日志含失败现场", "[ctl] about-to-fail" in logs_text(base, j["id"]))
    # 蓝线进程：提交后 job 已终态，不应有残留 running job
    check("锁无残留", held_locks(db) == 0)


def scenario_cancel(base, db):
    print("== 场景3 运行中取消（含幂等 + 非终态重试拒绝） ==")
    pid = plan_id_by_name(base, "CTRL 长跑(可取消)")
    run_id = trigger(base, pid)
    wait_any_running(base, run_id)
    run, jobs = get_jobs(base, run_id)
    jid = jobs[0]["id"]
    # 运行中重试必须被拒绝（幂等/防御：只有终态可重试）
    _, code = req(base, f"/api/jobs/{jid}/retry", "POST", {}, expect_error=True)
    check("运行中重试被拒(400)", code == 400, f"code={code}")
    # 取消（重复两次以验证幂等）
    req(base, f"/api/jobs/{jid}/cancel", "POST", {})
    req(base, f"/api/jobs/{jid}/cancel", "POST", {})
    saw_any = False
    end = time.time() + 8
    while time.time() < end:
        _, jobs2 = get_jobs(base, run_id)
        s = jobs2[0]["status"]
        if s == "cancelling":
            saw_any = True
        if s == "cancelled":
            break
        time.sleep(0.2)
    run_f, jobs_f, _ = wait_status(base, run_id)
    jf = jobs_f[0]
    check("观察到中间态 cancelling", saw_any, "(若未捕捉到也接受, 见 cancel 后终态)")
    check("job=cancelled(非 failed/timedout)", jf["status"] == "cancelled", f"status={jf['status']}")
    check("run=cancelled", run_f["status"] == "cancelled", f"run={run_f['status']}")
    rep, _ = report_of(base, jid)
    check("report.status=cancelled", rep["status"] == "cancelled")
    # 完成后再次取消：幂等，不报错
    req(base, f"/api/jobs/{jid}/cancel", "POST", {})
    check("终态后取消幂等", True)
    check("锁无残留", held_locks(db) == 0)


def scenario_timeout(base, db):
    print("== 场景4 超时 ==")
    run_id = trigger(base, plan_id_by_name(base, "CTRL 超时"))
    run, jobs, _ = wait_status(base, run_id)
    j = jobs[0]
    check("job=timedout", j["status"] == "timedout", f"status={j['status']} error={j.get('error')}")
    check("run=timedout", run["status"] == "timedout", f"run={run['status']}")
    rep, _ = report_of(base, j["id"])
    check("report.status=timedout", rep["status"] == "timedout")
    check("report/err 标识看门狗", "watchdog" in str(j.get("error") or rep.get("error") or ""))
    check("锁无残留", held_locks(db) == 0)


def scenario_race(base, db):
    print("== 场景5 超时与取消竞争（不得出现非法状态/异常） ==")
    run_id = trigger(base, plan_id_by_name(base, "CTRL 超时"))
    time.sleep(0.5)
    # 取消与看门狗超时(4s)几乎同时发生：取当前 job 后立刻取消
    run, jobs = get_jobs(base, run_id)
    if jobs:
        req(base, f"/api/jobs/{jobs[0]['id']}/cancel", "POST", {})
    run_f, jobs_f, _ = wait_status(base, run_id)
    jf = jobs_f[0]
    check("终态合法(cancelled/timedout)", jf["status"] in ("cancelled", "timedout"),
          f"status={jf['status']}")
    check("run 终态合法", run_f["status"] in ("cancelled", "timedout"), f"run={run_f['status']}")
    check("锁无残留", held_locks(db) == 0)


def scenario_retry(base, db):
    print("== 场景6 失败后重试成功（快照/attempt/旧报告保留） ==")
    # 确保开关关闭 → 首次失败
    if os.path.exists(FLAG):
        os.remove(FLAG)
    pid = plan_id_by_name(base, "CTRL 重试")
    run1 = trigger(base, pid)
    run_a, jobs_a, _ = wait_status(base, run1)
    orig = jobs_a[0]
    check("首次失败", orig["status"] == "failed", f"exit={orig['exit_code']}")
    old_rep_path = report_of(base, orig["id"])[1]
    # 置绿后再重试
    with open(FLAG, "w", encoding="utf-8") as f:
        f.write("green")
    out, _ = req(base, f"/api/jobs/{orig['id']}/retry", "POST", {})
    new_id = out["new_job_id"]
    new_run, new_jobs, _ = wait_status(base, run1)  # 重试 Job 属于同一 run
    rj = next((x for x in new_jobs if x["id"] == new_id), None)
    check("重试 Job 成功", rj and rj["status"] == "success", f"new#{new_id} status={rj and rj['status']}")
    check("attempt=2", (rj or {}).get("attempt") == 2, f"attempt={(rj or {}).get('attempt')}")
    check("retry_of 指向原 Job", (rj or {}).get("retry_of") == orig["id"],
          f"retry_of={(rj or {}).get('retry_of')}")
    check("run 聚合为 success", new_run["status"] == "success", f"run={new_run['status']}")
    _, new_rep_path = report_of(base, new_id)
    check("重试生成了独立新报告(不覆盖)", new_rep_path != old_rep_path,
          f"old={old_rep_path} new={new_rep_path}")
    check("锁无残留", held_locks(db) == 0)
    if os.path.exists(FLAG):
        os.remove(FLAG)


def scenario_mutex(base, db):
    print("== 场景7 同 Executor 独占锁互斥 ==")
    run_id = trigger(base, plan_id_by_name(base, "CTRL 互斥"))
    run, jobs, _ = wait_status(base, run_id, deadline=120)
    j1, j2 = jobs[0], jobs[1]
    check("两个 Job 均成功", j1["status"] == "success" and j2["status"] == "success",
          f"{j1['status']}/{j2['status']}")
    check("run=success", run["status"] == "success", f"run={run['status']}")
    # 关键：后者开始时间 >= 前者结束时间（严格串行 + 独占锁非并发）
    excl = j2.get("started_at", "") >= j1.get("ended_at", "")
    check("后者 start >= 前者 end (非并发, 锁排队)", excl,
          f"j1.end={j1.get('ended_at')} j2.start={j2.get('started_at')}")
    # 全程至多一个独占 Job 持有锁：检查表无遗留
    check("锁无残留", held_locks(db) == 0, f"held={held_locks(db)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--only", default="", help="子集: success|fail|cancel|timeout|race|retry|mutex")
    args = ap.parse_args()
    base, db = args.base, args.db

    before_locks = held_locks(db)
    suites = [scenario_success, scenario_fail, scenario_cancel, scenario_timeout,
              scenario_race, scenario_retry, scenario_mutex]
    only = set(filter(None, args.only.replace(",", " ").split()))
    mapping = {"success": 0, "fail": 1, "cancel": 2, "timeout": 3, "race": 4, "retry": 5, "mutex": 6}
    for key, fn in mapping.items():
        if only and key not in only:
            continue
        print("\n# ===== " + key + " =====")
        suites[fn](base, db)

    after = held_locks(db)
    print("\n========== 结果 ==========")
    print("ALL PASS" if not FAILED[0] else "HAS FAILURE")
    print(f"执行机锁残留(应全程为0)  before={before_locks} after={after}")
    return 1 if FAILED[0] else 0


if __name__ == "__main__":
    sys.exit(main())