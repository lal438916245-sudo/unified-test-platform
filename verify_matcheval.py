"""MatchEval 接入 · 真实集成验证。

假定隔离平台实例已运行（非 8000 端口、独立 DB/数据目录，见报告"启动方式"）。
覆盖：真实 fixture 评估成功 / 非法 algorithm,datset key 被拒 / 脚本失败如实 failed /
      运行中取消(进程树+锁回收) / 超时 timedout / report.json 字段/指标/产物 /
      三引擎组合(pytest→locust→matcheval)顺序与聚合。最后回归既有 verify 与单测。

用法：python verify_matcheval.py [--base http://127.0.0.1:8001] [--db 隔离库]
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


def wait_terminal(base, run_id, deadline=240):
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
    raise TimeoutError(f"run {run_id} 未结束")


def held_locks(db):
    with sqlite3.connect(db) as c:
        return c.execute(
            "SELECT COUNT(*) FROM executor_locks WHERE released_at IS NULL").fetchone()[0]


def plan_id_by_name(base, name):
    plans, _ = req(base, "/api/plans")
    for p in plans:
        if p["name"] == name:
            return p["id"]
    raise RuntimeError(f"未找到计划: {name}")


def art_ok(base, job_id, filename):
    try:
        r = urllib.request.urlopen(base + f"/api/artifacts/{job_id}/{filename}", timeout=20)
        body = r.read()
        return r.status == 200 and len(body) > 0
    except Exception:  # noqa: BLE001
        return False


def rep_of(base, job_id):
    rep, _ = req(base, f"/api/reports/{job_id}")
    return json.loads(rep["report_json"])


def seed_plan(db, name, *steps):
    """向隔离库注入一个 matcheval 计划（存在则跳过）。steps: dict(engine,name,params,requires_exclusive)."""
    with sqlite3.connect(db) as c:
        exists = c.execute("SELECT id FROM plans WHERE name=?", (name,)).fetchone()
        if exists:
            return exists[0]
        mat = c.execute("SELECT id FROM executors WHERE name='anaconda-matcheval'").fetchone()
        env = c.execute("SELECT id FROM environments WHERE name='local'").fetchone()
        if not (mat and env):
            raise RuntimeError("隔离库缺 matcheval 执行机或 local 环境，请先以对应解释器启动并预置")
        steps_json = json.dumps(steps, ensure_ascii=False)
        cur = c.execute(
            "INSERT INTO plans(name,description,environment_id,executor_id,steps,fail_fast,owner,created_at,updated_at)"
            " VALUES(?,?,?,?,?,1,'verify',datetime('now'),datetime('now'))",
            (name, name, env[0], mat[0], steps_json))
        c.commit()
        return cur.lastrowid


def scenario_single(base, db):
    print("== 场景1 真实 fixture 评估成功 => success，report 字段/指标/产物齐全 ==")
    pid = plan_id_by_name(base, "MatchEval(小样本 fixture 评估)")
    run_id = triggers(base, pid)
    run, jobs, _ = wait_terminal(base, run_id)
    j = jobs[0]
    check("job=success", j["status"] == "success", f"#{j['id']} err={j.get('error')}")
    check("run=success", run["status"] == "success", f"run={run['status']}")

    rj = rep_of(base, j["id"])
    check("engine=matcheval", rj.get("engine") == "matcheval")
    check("engine_data_schema=matcheval/v1", rj.get("engine_data_schema") == "matcheval/v1",
          f"got={rj.get('engine_data_schema')}")
    m = rj.get("metrics") or {}
    for k in ("tp", "fp", "fn", "precision", "recall", "f1", "sample_total", "duration_ms"):
        check(f"metrics 含 {k}", m.get(k) is not None, f"{m.get(k)}")
    check("sample_total>0", (m.get("sample_total") or 0) > 0, f"={m.get('sample_total')}")
    ed = rj.get("engine_data") or {}
    check("engine_data.dataset=fixture-small", ed.get("dataset") == "fixture-small")
    check("engine_data.group_metrics 含算法×阈值", bool(ed.get("group_metrics")))
    check("engine_data.heatmap_index 非空", bool(ed.get("heatmap_index")))
    names = {a["name"] for a in rj.get("artifacts") or []}
    check("artifacts 含 matcheval.csv", "matcheval.csv" in names)
    has_hm = any(n.startswith("heatmap_") for n in names)
    has_err = any(n.startswith("error_") for n in names)
    check("artifacts 含热力图", has_hm)
    check("artifacts 含错误样本图(至少一个算法有 miss)", has_err, f"names={sorted(names)}")
    check("产物可下载", art_ok(base, j["id"], "matcheval.csv"))
    check("锁无残留", held_locks(db) == 0, f"held={held_locks(db)}")


def scenario_bad_params(base, db):
    print("== 场景2 非法 algorithm / 非法 dataset key 被 Runner 拒绝 => failed ==")
    # 非法 dataset
    pid = seed_plan(db, "MATCH-BAD-DATASET",
                    {"engine": "matcheval", "name": "bad-ds", "requires_exclusive": False,
                     "params": {"dataset": "not-a-real-dataset", "algorithm": ["tpl"], "timeout_sec": 30}})
    _, jobs, _ = wait_terminal(base, triggers(base, pid))
    check("dataset 非法 -> failed", jobs[0]["status"] == "failed",
          f"err={jobs[0].get('error')}")
    # 非法 algorithm
    pid2 = seed_plan(db, "MATCH-BAD-ALG",
                     {"engine": "matcheval", "name": "bad-alg", "requires_exclusive": False,
                      "params": {"dataset": "fixture-small", "algorithm": ["surf"], "timeout_sec": 30}})
    _, jobs2, _ = wait_terminal(base, triggers(base, pid2))
    check("algorithm 非法 -> failed", jobs2[0]["status"] == "failed",
          f"err={jobs2[0].get('error')}")
    check("锁无残留", held_locks(db) == 0)


def scenario_script_fail(base, db):
    print("== 场景3 脚本失败(match_eval 缺图退出) => 如实 failed ==")
    pid = seed_plan(db, "MATCH-SCRIPT-FAIL",
                    {"engine": "matcheval", "name": "empty", "requires_exclusive": False,
                     "params": {"dataset": "fixture-empty", "algorithm": ["tpl"], "timeout_sec": 60}})
    run, jobs, _ = wait_terminal(base, triggers(base, pid))
    j = jobs[0]
    check("job=failed(如实归档，不伪造成功)", j["status"] == "failed",
          f"job={j['status']} err={j.get('error')}")
    check("run=failed", run["status"] == "failed", f"run={run['status']}")
    rj = rep_of(base, j["id"])
    check("report.status=failed", rj.get("status") == "failed")
    check("锁无残留", held_locks(db) == 0)


def scenario_three(base, db):
    print("== 场景4 三引擎组合：pytest→locust→matcheval 串行，均成功 => run=success ==")
    pid = plan_id_by_name(base, "三引擎组合(pytest+Locust+MatchEval)")
    run_id = triggers(base, pid)
    run, jobs, _ = wait_terminal(base, run_id)
    eng = [j["engine"] for j in jobs]
    check("引擎顺序 pytest,locust,matcheval", eng == ["pytest", "locust", "matcheval"], f"{eng}")
    ok = all(j["status"] == "success" for j in jobs)
    check("三 job 均 success", ok, f"{[(j['engine'], j['status']) for j in jobs]}")
    check("run=success(聚合)", run["status"] == "success", f"run={run['status']}")
    # 每个引擎 report 的 engine_data_schema 与其自身一致
    for j in jobs:
        rj = rep_of(base, j["id"])
        expect = {"pytest": None, "locust": "locust/1.0", "matcheval": "matcheval/v1"}[j["engine"]]
        if expect:
            check(f"{j['engine']} schema={expect}", rj.get("engine_data_schema") == expect,
                  f"got={rj.get('engine_data_schema')}")
    check("锁无残留", held_locks(db) == 0)


def scenario_cancel(base, db):
    print("== 场景5 运行中取消 => cancelling→cancelled，进程树/锁回收 ==")
    pid = seed_plan(db, "MATCH-CANCEL",
                    {"engine": "matcheval", "name": "long", "requires_exclusive": True,
                     "params": {"dataset": "fixture-small",
                                "algorithm": ["tpl", "mstpl", "kaze", "brisk", "akaze", "orb"],
                                "threshold": 0.8, "timeout_sec": 300}})
    run_id = triggers(base, pid)
    end = time.time() + 90
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
    check("锁无残留(运行中取消)", held_locks(db) == 0, f"held={held_locks(db)}")


def scenario_timeout(base, db):
    print("== 场景6 短超时看门狗 => timedout，进程/锁回收 ==")
    pid = seed_plan(db, "MATCH-TIMEOUT",
                    {"engine": "matcheval", "name": "to", "requires_exclusive": True,
                     "params": {"dataset": "fixture-small",
                                "algorithm": ["tpl", "mstpl", "kaze", "brisk", "akaze", "orb"],
                                "threshold": 0.8, "timeout_sec": 2}})
    run, jobs, _ = wait_terminal(base, triggers(base, pid))
    j = jobs[0]
    check("job=timedout", j["status"] == "timedout", f"job={j['status']} err={j.get('error')}")
    check("run=timedout", run["status"] == "timedout", f"run={run['status']}")
    rj = rep_of(base, j["id"])
    check("report.status=timedout", rj.get("status") == "timedout")
    check("锁无残留(超时)", held_locks(db) == 0, f"held={held_locks(db)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8001")
    ap.add_argument("--db", default=os.path.join(BASE_DIR, "data_verify_matcheval", "db.sqlite"))
    args = ap.parse_args()
    base, db = args.base, args.db
    print(f"[verify_matcheval] base={base}\ndb={db}")

    for fn in (scenario_single, scenario_bad_params, scenario_script_fail,
               scenario_three, scenario_cancel, scenario_timeout):
        print("\n# ===== " + fn.__name__ + " =====")
        fn(base, db)

    print("\n========== 结果 ==========")
    print("ALL PASS" if not FAILED[0] else "HAS FAILURE")
    print(f"执行机锁残留(应全程为0) held={held_locks(db)}")
    return 1 if FAILED[0] else 0


if __name__ == "__main__":
    sys.exit(main())