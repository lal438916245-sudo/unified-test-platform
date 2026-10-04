"""报告中心 · 隔离集成验证。

在隔离 SQLite（独立数据目录）中种入"已归档事实报告"(jobs+reports/report.json)，
走真实启动迁移路径 rebuild_report_index() 派生 report_index（读模型，不改事实源），
再经 HTTP API 校验：筛选/分页/options、严格对比兼容性、引擎专项指标方向、历史趋势、
旧记录降级展示。全程不引入新引擎，fixture 与真实资产识别报告不做有效对比。

约定：隔离平台实例运行于 --base（默认 http://127.0.0.1:8002），
     DB 为 --db（默认 platform/data_verify_reportcenter/db.sqlite）。
用法：python verify_report_center.py [--base http://127.0.0.1:8002] [--db <隔离库>]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # platform
FAILED = [False]

import urllib.request  # noqa: E402

from app import db  # noqa: E402
from app.report_center import rebuild_report_index  # noqa: E402


def check(name, cond, extra=""):
    if not cond:
        FAILED[0] = True
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def retry(times):
    """带退避重试：隔离服务端与脚本并发写同一库时有瞬时 SQLite 文件锁竞争。"""
    def deco(fn):
        def wrapper(*a, **k):
            import time
            for i in range(times):
                try:
                    return fn(*a, **k)
                except sqlite3.OperationalError:
                    time.sleep(1.0)
            return fn(*a, **k)
        return wrapper
    return deco


def req(base, path, method="GET", data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(base + path, data=body, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


# ---------- 事实报告种子（模拟已归档，绝不当作真实性能结论） ----------
T = "2026-09-20T10:00:00"


def env_id(c, name):
    r = c.execute("SELECT id FROM environments WHERE name=?", (name,)).fetchone()
    return r[0] if r else None


def seed_facts(db_path):
    """种入 job + reports(report.json) 作为事实源。返回 {job_id: report} 供校验。"""
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT OR IGNORE INTO environments(name,host,port) VALUES('rc_local','127.0.0.1',8001)")
        c.execute("INSERT OR IGNORE INTO environments(name,host,port) VALUES('rc_staging','127.0.0.1',9001)")
        c.commit()
    with sqlite3.connect(db_path) as c:
        el = env_id(c, "rc_local"); es = env_id(c, "rc_staging")
        pid = c.execute("SELECT id FROM plans LIMIT 1").fetchone()
        pid = pid[0] if pid else None
        c.execute("INSERT OR IGNORE INTO plans(name,description,fail_fast,owner) VALUES(?,?,1,'verify')",
                  ("报告中心验证计划", "隔离验证用"))
        plan = c.execute("SELECT id FROM plans WHERE name='报告中心验证计划'").fetchone()
        pid = plan[0]

        def add(jid, engine, name, envid, params, status, schema, metrics,
                started=None, ended=None, summary=None, schema_version=None):
            if envid is None:
                envid = el
            c.execute(
                "INSERT OR REPLACE INTO jobs(id,plan_id,engine,name,environment_id,status,started_at,ended_at,params)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (jid, pid, engine, name, envid, status, started or T, ended or T,
                 json.dumps(params, ensure_ascii=False)))
            rep = {"engine": engine, "engine_data_schema": schema or "",
                   "status": status, "started_at": ended or started or T, "ended_at": ended or T,
                   "metrics": metrics,
                   "summary": summary or {"duration_ms": (metrics or {}).get("duration_ms")}}
            if schema_version:
                rep["schema_version"] = schema_version
            c.execute(
                "INSERT OR REPLACE INTO reports(job_id,plan_run_id,report_path,report_json,created_at)"
                " VALUES(?,?,?,?,?)",
                (jid, None, f"run_{jid}/job_{jid}/report.json",
                 json.dumps(rep, ensure_ascii=False), T))

        L = {"locustfile": "http-fixture", "users": 4, "spawn_rate": 2, "run_time": "12s"}
        add(1001, "locust", "L1-fixture", el, L, "success", "locust/1.0",
            {"rps": 10.0, "requests": 500, "failure_rate": 0.02, "avg_ms": 15.0,
             "p95_ms": 100.0, "p99_ms": 140.0, "duration_ms": 12000}, "2026-09-20T10:00:00")
        add(1002, "locust", "L2-fixture", el, L, "success", "locust/1.0",
            {"rps": 12.0, "requests": 600, "failure_rate": 0.01, "avg_ms": 13.0,
             "p95_ms": 90.0, "p99_ms": 130.0, "duration_ms": 12000}, "2026-09-21T10:00:00")
        Lu = dict(L, users=8)
        add(1003, "locust", "L3-users8", el, Lu, "success", "locust/1.0",
            {"rps": 20.0, "failure_rate": 0.05, "duration_ms": 12000}, "2026-09-21T11:00:00")
        add(1004, "locust", "L4-staging", es, L, "success", "locust/1.0",
            {"rps": 11.0, "failure_rate": 0.02, "duration_ms": 12000}, "2026-09-21T12:00:00")
        add(1005, "locust", "L5-schema2", el, L, "success", "locust/2.0",
            {"rps": 9.0, "failure_rate": 0.03, "duration_ms": 12000}, "2026-09-21T13:00:00")

        M = {"dataset": "fixture-small", "algorithm": ["tpl"], "threshold": 0.8}
        add(2001, "matcheval", "M1-fixture", el, M, "success", "matcheval/v1",
            {"precision": 0.90, "recall": 0.70, "f1": 0.78, "tp": 90, "fp": 10,
             "fn": 20, "sample_total": 120, "duration_ms": 5000}, "2026-09-20T12:00:00")
        add(2002, "matcheval", "M2-fixture", el, M, "success", "matcheval/v1",
            {"precision": 0.92, "recall": 0.72, "f1": 0.80, "tp": 92, "fp": 8,
             "fn": 18, "sample_total": 120, "duration_ms": 5000}, "2026-09-21T12:00:00")
        Mr = {"dataset": "real-assets", "algorithm": ["tpl"], "threshold": 0.8}
        add(2003, "matcheval", "M3-real", el, Mr, "success", "matcheval/v1",
            {"precision": 0.98, "fingerprint": "real-ds", "duration_ms": 8000},
            "2026-09-21T13:00:00")
        Mt = {"dataset": "fixture-small", "algorithm": ["tpl"], "threshold": 0.7}
        add(2004, "matcheval", "M4-threshold", el, Mt, "success", "matcheval/v1",
            {"precision": 0.85, "duration_ms": 5000}, "2026-09-21T14:00:00")

        add(3001, "pytest", "P1", el, {"args": ["demo/test_platform_demo.py"]}, "success", "",
            {"passed": 5, "failed": 0, "duration_ms": 1000}, "2026-09-20T14:00:00")
        add(3002, "pytest", "P2", el, {"args": ["demo/test_other.py"]}, "success", "",
            {"passed": 4, "failed": 1, "duration_ms": 900}, "2026-09-21T14:00:00")
        add(3999, "pytest", "P_old", el, {}, "success", "",
            {"passed": 2, "failed": 0, "duration_ms": 500}, "2026-09-19T14:00:00")
        c.commit()


def scenario_migration(db_path):
    print("== 场景1 数据迁移：rebuild_report_index 派生索引，旧记录标记不完整 ==")
    run_mig = retry(8)(rebuild_report_index)
    mig = run_mig(db_path)
    check("重建总数=12", mig["rebuilt"] == 12, f"rebuilt={mig['rebuilt']}")
    check("不完整(旧记录)=1", mig["incomplete"] == 1, f"incomplete={mig['incomplete']}")
    check("跳过=0", mig["skipped"] == 0, f"skipped={mig['skipped']}")
    # 事实源未被改写：reports 表行数不变
    with sqlite3.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    check("事实源 reports 未被改写", n == 12, f"reports={n}")
    old = db.get_report_index(db_path, 3999)
    check("旧记录 metadata_incomplete=1", int(old.get("metadata_incomplete") or 0) == 1)
    ok = db.get_report_index(db_path, 1002)
    check("足字段记录 metadata_incomplete=0", int(ok.get("metadata_incomplete")) == 0)


def scenario_filter(base):
    print("== 场景2 筛选 + 分页 + options ==")
    # 按引擎
    d = req(base, "/api/report-center?engine=locust")
    check("engine=locust 命中 5 条", d["total"] == 5 and all(r["engine"] == "locust" for r in d["rows"]),
          f"total={d['total']}")
    # 按状态
    d = req(base, "/api/report-center?status=success")
    check("status=success 命中全部 12 条", d["total"] == 12, f"total={d['total']}")
    # 按环境
    d = req(base, "/api/report-center?env_name=rc_staging")
    check("env_name=rc_staging 命中 locust#1004", d["total"] == 1 and d["rows"][0]["job_id"] == 1004,
          f"total={d['total']}")
    # 分页：page_size=4 第二页 → 第 5~8 条
    d1 = req(base, "/api/report-center?page=1&page_size=4")
    d2 = req(base, "/api/report-center?page=2&page_size=4")
    check("分页 total=12", d1["total"] == 12)
    check("page1 取 4 条", len(d1["rows"]) == 4)
    check("page2 取 4 条且不重叠", len(d2["rows"]) == 4 and not {r["job_id"] for r in d1["rows"]} & {r["job_id"] for r in d2["rows"]})
    # 时间范围
    d = req(base, "/api/report-center?time_from=2026-09-21&time_to=2026-09-21T13:00:00")
    check("时间范围 09-21 → 09-21T13 有结果", d["total"] > 0, f"total={d['total']}")
    # options
    opt = req(base, "/api/report-center/options")
    check("options.engines 含三引擎", set(opt["engines"]) >= {"pytest", "locust", "matcheval"})
    check("options.envs 含两环境", {e["name"] for e in opt["envs"]} >= {"rc_local", "rc_staging"})
    # 单条详情
    idx = req(base, "/api/report-center/1002")
    check("单条详情可取", idx.get("job_id") == 1002)
    # 列表旧记录降级标记
    d = req(base, "/api/report-center?engine=pytest")
    p_old = next(r for r in d["rows"] if r["job_id"] == 3999)
    check("列表可见旧记录 metadata_incomplete=1(⚠)", int(p_old["metadata_incomplete"]) == 1)


def scenario_compare(base):
    print("== 场景3 严格对比兼容性 ==")
    def cmp(a, b):
        return req(base, "/api/report-center/compare", "POST",
                   {"baseline_job_id": a, "candidate_job_id": b})

    # 兼容：同 locust 配置 1001 vs 1002
    r = cmp(1001, 1002)
    check("locust 同配置 → 有效对比", r["compatible"] is True and r["summary"] == "有效对比",
          f"reasons={r['reasons']}")
    items = {it["key"]: it for it in r["deltas"]["items"]}
    check("RPS 上升=更好(up)", items["rps"]["delta"] == 2.0 and items["rps"]["color"] == "up")
    check("错误率下降=更好，百分比差值 -1.0",
          items["failure_rate"]["delta"] == -1.0 and items["failure_rate"]["color"] == "up")
    check("P95 下降=更好(up)", items["p95_ms"]["delta"] == -10.0 and items["p95_ms"]["color"] == "up")
    check("趋势含 2 个同配置成功点", len(r["trend"]["points"]) == 2,
          f"points={[p['job_id'] for p in r['trend']['points']]}")

    # JSON 响应不落敏感/明文全路径
    raw = json.dumps(r)
    check("对比响应无明文本地路径/host端口泄漏",
          "F:\\\\" not in raw and "token" not in raw.lower(), "")

    r = cmp(1001, 1003)
    check("locust 并发不同 → 拒绝", r["compatible"] is False and any("users" in x for x in r["reasons"]),
          f"reasons={r['reasons']}")
    r = cmp(1001, 1004)
    check("环境不同 → 拒绝", r["compatible"] is False and any("环境不同" in x for x in r["reasons"]),
          f"reasons={r['reasons']}")
    r = cmp(1001, 1005)
    check("schema 主版本不同 → 拒绝", r["compatible"] is False and any("主版本" in x for x in r["reasons"]),
          f"reasons={r['reasons']}")

    r = cmp(2001, 2002)
    check("matcheval 同配置 → 有效对比", r["compatible"] is True, f"reasons={r['reasons']}")
    it = {x["key"]: x for x in r["deltas"]["items"]}
    check("precision 上升=更好(up)", it["precision"]["delta"] == 2.0 and it["precision"]["color"] == "up")
    check("FP 下降=更好(up)", it["fp"]["delta"] == -2.0 and it["fp"]["color"] == "up")
    r = cmp(2001, 2003)
    check("fixture 与真实数据集不混比 → 拒绝", r["compatible"] is False and any("dataset" in x for x in r["reasons"]),
          f"reasons={r['reasons']}")
    r = cmp(2001, 2004)
    check("阈值不同 → 拒绝", r["compatible"] is False and any("阈值" in x for x in r["reasons"]),
          f"reasons={r['reasons']}")

    r = cmp(3001, 3002)
    check("pytest 测试选择器不同 → 拒绝", r["compatible"] is False and any("选择器" in x for x in r["reasons"]),
          f"reasons={r['reasons']}")
    r = cmp(3001, 3999)
    check("旧记录元数据不足 → 拒绝降级展示", r["compatible"] is False and any("不完整" in x for x in r["reasons"]),
          f"reasons={r['reasons']}")
    r = cmp(1001, 2001)
    check("跨引擎(不做总分) → 拒绝", r["compatible"] is False and any("引擎不同" in x for x in r["reasons"]),
          f"reasons={r['reasons']}")

    # 找不到的报告索引 → 404
    try:
        req(base, "/api/report-center/compare", "POST",
            {"baseline_job_id": 99999, "candidate_job_id": 1002})
        check("不存在的基线 → 404", False)
    except urllib.error.HTTPError as e:
        check("不存在的基线 → 404", e.code == 404, f"code={e.code}")


def scenario_preseed_effect(dbdict):
    """默认模式下的只读校验：确认启动迁移已把事实派生进索引。"""
    print("== 场景1(只读) 迁移生效：report_index 已派生，旧记录标记不完整 ==")
    with sqlite3.connect(dbdict["db"]) as c:
        nidx = c.execute("SELECT COUNT(*) FROM report_index WHERE engine IS NOT NULL").fetchone()[0]
        nrep = c.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    check("report_index 已派生(12)", nidx == 12, f"idx={nidx}")
    if nrep != 12:
        FAILED[0] = True
    old = db.get_report_index(dbdict["db"], 3999)
    check("旧记录 metadata_incomplete=1(降级展示)", int(old.get("metadata_incomplete") or 0) == 1)
    ok = db.get_report_index(dbdict["db"], 1002)
    check("足字段记录 metadata_incomplete=0", int(ok.get("metadata_incomplete")) == 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8002")
    ap.add_argument("--db", default=os.path.join(BASE_DIR, "data_verify_reportcenter", "db.sqlite"))
    ap.add_argument("--preseed", action="store_true",
                    help="仅在服务停止时使用：建库+种事实报告+rebuild 迁移并断言统计")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.db), exist_ok=True)
    print(f"[verify_report_center] base={args.base}\ndb={args.db}")

    if args.preseed:
        db.init_db(args.db)
        retry(8)(seed_facts)(args.db)
        print("\n# ===== scenario_migration (preseed) =====")
        scenario_migration(args.db)
    else:
        dbd = {"db": args.db}
        try:
            req(args.base, "/api/report-center")
        except Exception as e:  # noqa: BLE001
            print(f"[verify_report_center] 需要先以 `--preseed` 造数据、再启动隔离服务端({args.base})后运行本模式: {e}")
            return 1
        scenario_preseed_effect(dbd)
        scenario_filter(args.base)
        scenario_compare(args.base)

    print("\n========== 结果 ==========")
    print("ALL PASS" if not FAILED[0] else "HAS FAILURE")
    return 1 if FAILED[0] else 0


if __name__ == "__main__":
    sys.exit(main())