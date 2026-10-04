"""测试计划编辑器 + 不可变执行快照 · 隔离验证（自包含，无需外部服务）。

在独立临时数据目录 + 独立 SQLite 中启动"真实 uvicorn 服务端 + 编排线程"（非 8000 端口），
经真实 HTTP 覆盖：
  计划元数据与旧库迁移 / 编辑器创建·编辑·revision 递增·复制 / 步骤排序 /
  非法参数与非法引擎拒绝 / 停用配置不可引用 / 预检·执行预览无副作用 /
  执行时固化 plan_revision + plan_snapshot 且后续编辑原计划不改变历史 /
  三步骤组合计划（pytest→locust→matcheval）实跑顺序与聚合。

安全：只引用受控配置 ID/key 与隔离 fixture 资产；绝不连接真实 Colyseus/Unity/生产环境。
用法：python verify_plan_editor.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# 先落隔离参数，再导入任何会读取它们的模块（与 run.py 的启动顺序一致）
TMP = tempfile.mkdtemp(prefix="verify_plan_editor_")
os.environ["PLATFORM_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["PLATFORM_DB"] = os.path.join(TMP, "db.sqlite")

FAILED = [False]
JOB_TERMINAL = ("success", "failed", "cancelled", "timedout", "skipped")
RUN_SETTLED = ("success", "failed", "cancelled", "timedout", "partial", "skipped")


def check(name, cond, extra=""):
    if not cond:
        FAILED[0] = True
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(base, path, method="GET", data=None, expect=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(base + path, data=body, method=method)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=40) as resp:
            raw = resp.read().decode()
            return (json.loads(raw) if raw else None), resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        if expect is not None and e.code == expect:
            return raw, e.code
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {raw[:300]}")


# ---------- 常用受控实体 ----------
def find_by_name(base, path, name):
    rows, _ = req(base, path)
    for r in rows:
        if r.get("name") == name:
            return r
    return None


def pytest_step(args=("demo/test_platform_demo.py", "-o", "addopts=", "--tb=short"), **kw):
    p = {"timeout_sec": 120, "cwd_root": "backend-demo", "args": list(args)}
    p.update(kw)
    return {"engine": "pytest", "name": "smoke", "requires_exclusive": False, "params": p}


def locust_step(**kw):
    p = {"locustfile": "http-fixture", "users": 4, "spawn_rate": 2,
         "run_time": "12s", "csv_full_history": False, "timeout_sec": 120}
    p.update(kw)
    return {"engine": "locust", "name": "locust-fixture", "requires_exclusive": False, "params": p}


def matcheval_step(**kw):
    p = {"dataset": "fixture-small", "algorithm": ["tpl", "mstpl"], "threshold": 0.8,
         "output_level": "full", "timeout_sec": 180}
    p.update(kw)
    return {"engine": "matcheval", "name": "match-fixture", "requires_exclusive": False, "params": p}


def count_rows(db_path, table):
    import sqlite3
    with sqlite3.connect(db_path) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def wait_terminal(base, run_id, deadline=300):
    end = time.time() + deadline
    while time.time() < end:
        d, _ = req(base, f"/api/runs/{run_id}")
        run, jobs = d["run"], d["jobs"]
        if all(j["status"] in JOB_TERMINAL for j in jobs) and run["status"] in RUN_SETTLED:
            return run, jobs
        time.sleep(0.4)
    raise TimeoutError(f"run {run_id} 未在 {deadline}s 内结束")


# ---------- 旧库兼容迁移（无 revision / plan_revision / plan_snapshot） ----------
def scenario_migration():
    import sqlite3
    from app import db
    print("== 0. 旧库兼容迁移（只补列，不破坏历史） ==")
    old = os.path.join(TMP, "old", "db.sqlite")
    os.makedirs(os.path.dirname(old), exist_ok=True)
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE plans(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                  "steps TEXT DEFAULT '[]', fail_fast INTEGER DEFAULT 1)")
        c.execute("INSERT INTO plans(name,steps,fail_fast) VALUES('legacy','[]',1)")
        c.execute("CREATE TABLE plan_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "plan_id INTEGER, status TEXT)")
        c.execute("INSERT INTO plan_runs(plan_id,status) VALUES(1,'success')")
        c.commit()
    db.init_db(old)
    p = db.get_plan(old, 1)
    check("旧库迁移：plans 补 revision 且默认 1", p and p["revision"] == 1, f"rev={p and p['revision']}")
    r = db.get_run(old, 1)
    check("旧库迁移：plan_runs 补 plan_revision/plan_snapshot 且允许为空",
          r and r["plan_revision"] is None and r["plan_snapshot"] is None)
    check("旧库迁移：历史行未被改写", p and p["name"] == "legacy")


def main() -> int:
    import uvicorn

    from app import db
    from app.api import build_app
    from app.orchestration.scheduler import Orchestrator
    from app.report_center import rebuild_report_index
    from preset import seed_if_empty

    scenario_migration()

    db_path = os.environ["PLATFORM_DB"]
    db.init_db(db_path)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    seed_if_empty(db_path, self_url=base)
    rebuild_report_index(db_path)

    frontend = os.path.join(os.path.dirname(BACKEND), "frontend")
    app = build_app(frontend)
    app.state.db_path = db_path
    orch = Orchestrator(db_path)
    orch.start()
    app.state.orchestrator = orch

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        try:
            req(base, "/api/plans")
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    print(f"[verify_plan_editor] base={base}\ndb={db_path}")

    try:
        run_all(base, db_path)
    finally:
        orch.shutdown()
        server.should_exit = True

    print("\n========== 结果 ==========")
    print("ALL PASS" if not FAILED[0] else "HAS FAILURE")
    return 1 if FAILED[0] else 0


def run_all(base, db_path):
    from app.report_center import rebuild_report_index

    env = find_by_name(base, "/api/environments", "local")
    ex = find_by_name(base, "/api/executors", "anaconda-python")
    asset = find_by_name(base, "/api/asset-sources", "matcheval-fixture-small")
    assert env and ex and asset, "预置配置缺失"
    env_id, ex_id, asset_id = env["id"], ex["id"], asset["id"]

    print("== 1. 计划元数据（revision/created_at/updated_at 兼容补齐） ==")
    plans, _ = req(base, "/api/plans")
    check("预置计划全部带 revision>=1", all(int(p.get("revision") or 0) >= 1 for p in plans),
          f"({len(plans)} 个)")
    check("预置计划带 created_at/updated_at",
          all(p.get("created_at") and p.get("updated_at") for p in plans))
    demo = next(p for p in plans if "自检" in p["name"])
    check("预置计划初始 rev=1（迁移不递增版本）", int(demo["revision"]) == 1)

    print("== 2. 编辑器：创建 / 编辑 / revision 递增 ==")
    body = {"name": "PE-edit", "description": "编辑器验证", "environment_id": env_id,
            "executor_id": ex_id, "asset_source_id": 0, "fail_fast": True,
            "steps": [pytest_step()]}
    created, _ = req(base, "/api/plans", "POST", body)
    pid = created["id"]
    p0, _ = req(base, f"/api/plans/{pid}")
    check("新建计划 rev=1", int(p0["revision"]) == 1, f"rev={p0['revision']}")
    check("新建计划带时间戳", bool(p0["created_at"]) and bool(p0["updated_at"]))

    upd, _ = req(base, f"/api/plans/{pid}", "PUT", {**body, "name": "PE-edit-r2"})
    check("编辑返回体已递增 revision", int(upd["revision"]) == 2, f"rev={upd['revision']}")
    p1, _ = req(base, f"/api/plans/{pid}")
    check("持久化 revision=2 且名称已更新",
          int(p1["revision"]) == 2 and p1["name"] == "PE-edit-r2")
    check("updated_at 随编辑刷新", p1["updated_at"] >= p0["updated_at"])
    req(base, f"/api/plans/{pid}", "PUT", {**body, "name": "PE-edit-r3"})
    p2, _ = req(base, f"/api/plans/{pid}")
    check("连续编辑 revision 继续递增=3", int(p2["revision"]) == 3, f"rev={p2['revision']}")

    print("== 3. 编辑器：复制（独立副本，rev 重置为 1） ==")
    cp, _ = req(base, f"/api/plans/{pid}/copy", "POST", {})
    cid = cp["id"]
    check("复制产生新计划 ID", cid != pid, f"new={cid} from={cp['copied_from']}")
    c0, _ = req(base, f"/api/plans/{cid}")
    check("副本 rev=1", int(c0["revision"]) == 1)
    check("副本名称带(副本)", c0["name"].endswith("(副本)"), c0["name"])
    check("副本步骤与源一致",
          [s["engine"] for s in c0["steps"]] == [s["engine"] for s in p2["steps"]])
    req(base, f"/api/plans/{cid}", "PUT", {**body, "name": "PE-copy-edited",
                                           "fail_fast": False})
    src_after, _ = req(base, f"/api/plans/{pid}")
    check("编辑副本不影响源计划",
          int(src_after["revision"]) == 3 and src_after["name"] == "PE-edit-r3")
    cp2, _ = req(base, f"/api/plans/{pid}/copy", "POST", {"name": "PE-显式命名"})
    named, _ = req(base, f"/api/plans/{cp2['id']}")
    check("复制支持显式命名", named["name"] == "PE-显式命名")

    print("== 4. 编辑器：步骤顺序（增删/上下移等价于整表重排） ==")
    order_body = {**body, "name": "PE-order",
                  "steps": [pytest_step(), locust_step(), matcheval_step()]}
    o, _ = req(base, "/api/plans", "POST", order_body)
    oid = o["id"]
    got, _ = req(base, f"/api/plans/{oid}")
    check("创建时步骤顺序保持", [s["engine"] for s in got["steps"]] == ["pytest", "locust", "matcheval"])
    req(base, f"/api/plans/{oid}", "PUT",
        {**order_body, "steps": [matcheval_step(), pytest_step(), locust_step()]})
    got2, _ = req(base, f"/api/plans/{oid}")
    check("重排后顺序生效", [s["engine"] for s in got2["steps"]] == ["matcheval", "pytest", "locust"])
    req(base, f"/api/plans/{oid}", "PUT",
        {**order_body, "steps": [pytest_step(), matcheval_step()]})
    got3, _ = req(base, f"/api/plans/{oid}")
    check("删除步骤后长度=2", len(got3["steps"]) == 2)

    print("== 5. 非法参数 / 非法引擎被后端拒绝（前端仅改善体验） ==")
    def reject(name, steps, needle=None):
        _, code = req(base, "/api/plans", "POST",
                      {**body, "name": "PE-bad", "steps": steps}, expect=400)
        check(name, code == 400, f"-> {code}")

    reject("拒绝未注册引擎", [{"engine": "shell", "name": "x", "params": {}}])
    reject("拒绝任意命令参数键", [pytest_step(command="rm -rf /")])
    reject("拒绝绝对路径 args", [pytest_step(args=[r"C:\evil.py"])])
    reject("拒绝 shell 元字符 args", [pytest_step(args=["demo/a.py; rm -rf /"])])
    reject("拒绝可执行文件 args", [pytest_step(args=["tool.exe"])])
    reject("拒绝非受控 cwd_root", [pytest_step(cwd_root="anywhere")])
    reject("拒绝非受控 locustfile", [locust_step(locustfile=r"C:\evil.py")])
    reject("拒绝非受控 dataset", [matcheval_step(dataset="real-assets")])
    reject("拒绝超范围 threshold", [matcheval_step(threshold=1.5)])
    reject("拒绝非数值 threshold", [matcheval_step(threshold="abc")])
    reject("拒绝非白名单 algorithm", [matcheval_step(algorithm=["tpl", "yolo"])])
    reject("拒绝非法 output_level", [matcheval_step(output_level="debug")])
    reject("拒绝空步骤", [])
    ok, _ = req(base, "/api/plans", "POST", {**body, "name": "PE-legal",
                                             "steps": [pytest_step()]})
    check("合法相对选择器 args 被接受", isinstance(ok, dict) and "id" in ok)

    print("== 6. 停用 / 不存在的配置不可被引用 ==")
    t_env, _ = req(base, "/api/config/environments", "POST",
                   {"name": "pe-env", "kind": "http", "protocol": "http",
                    "host": "127.0.0.1", "port": 9})
    t_ex, _ = req(base, "/api/config/executors", "POST",
                  {"name": "pe-ex", "python_ref": "anaconda", "cwd_root": "backend-demo",
                   "allowed_runners": ["pytest"]})
    t_as, _ = req(base, "/api/config/asset-sources", "POST",
                  {"name": "pe-as", "kind": "matcheval", "dataset_ref": "fixture-small"})
    for kind, ent in (("environments", t_env), ("executors", t_ex), ("asset-sources", t_as)):
        req(base, f"/api/config/{kind}/{ent['id']}/toggle", "POST", {})
    bad, code = req(base, "/api/plans", "POST",
                    {"name": "PE-off", "environment_id": t_env["id"], "executor_id": t_ex["id"],
                     "asset_source_id": t_as["id"], "steps": [pytest_step()]}, expect=400)
    check("引用停用配置被拒（400 且提示停用）", code == 400 and "停用" in str(bad), str(bad)[:120])
    for kind, ent in (("environments", t_env), ("executors", t_ex), ("asset-sources", t_as)):
        req(base, f"/api/config/{kind}/{ent['id']}/toggle", "POST", {})
    _, code2 = req(base, "/api/plans", "POST",
                   {"name": "PE-nope", "environment_id": 999999, "executor_id": ex_id,
                    "steps": [pytest_step()]}, expect=400)
    check("引用不存在 Environment 被拒", code2 == 400)

    print("== 7. 预检 / 执行预览：复用执行校验，零副作用 ==")
    runs_before = count_rows(db_path, "plan_runs")
    jobs_before = count_rows(db_path, "jobs")
    draft, _ = req(base, "/api/plans/precheck", "POST",
                   {"name": "PE-pre", "environment_id": env_id, "executor_id": ex_id,
                    "asset_source_id": 0, "steps": [pytest_step(), locust_step()]})
    check("草稿预检返回结构完整",
          all(k in draft for k in ("steps", "config", "missing", "next_steps",
                                   "concurrency", "fail_fast", "fingerprint", "no_side_effects")))
    check("预检步骤顺序与解析引擎一致",
          [s["engine"] for s in draft["steps"]] == ["pytest", "locust"])
    check("预检标注引擎注册/参数/执行机允许/依赖就绪",
          all(k in draft["steps"][0] for k in ("engine_registered", "params_ok",
                                               "executor_allows", "deps_ready", "ready")))
    check("预检声明无副作用", draft["no_side_effects"] is True)
    check("预检给出 fail_fast 预期行为", "enabled" in draft["fail_fast"] and "behavior" in draft["fail_fast"])
    check("预检给出并发/锁提示", "exclusive_lock_held" in draft["concurrency"])

    bad_draft, _ = req(base, "/api/plans/precheck", "POST",
                       {"name": "PE-pre-bad", "environment_id": env_id, "executor_id": ex_id,
                        "steps": [{"engine": "shell", "name": "x", "params": {}}]})
    check("预检识别未注册引擎（ready=False）",
          bad_draft["steps"][0]["engine_registered"] is False
          and bad_draft["steps"][0]["ready"] is False)
    check("预检对未注册引擎给出缺失项/下一步",
          bool(bad_draft["missing"]) and bool(bad_draft["next_steps"]))

    saved, _ = req(base, f"/api/plans/{pid}/precheck", "POST")
    check("已保存计划预检可用", "steps" in saved and saved.get("revision") == 3)
    _, code404 = req(base, "/api/plans/999999/precheck", "POST", expect=404)
    check("不存在计划预检 → 404", code404 == 404)
    check("预检未创建任何 PlanRun", count_rows(db_path, "plan_runs") == runs_before)
    check("预检未创建任何 Job", count_rows(db_path, "jobs") == jobs_before)

    print("== 8. 执行快照：固化 plan_revision + plan_snapshot ==")
    snap_body = {"name": "PE-snap-A", "description": "快照原定义", "environment_id": env_id,
                 "executor_id": ex_id, "asset_source_id": 0, "fail_fast": True,
                 "steps": [pytest_step(), locust_step()]}
    s0, _ = req(base, "/api/plans", "POST", snap_body)
    spid = s0["id"]
    run, _ = req(base, f"/api/plans/{spid}/run", "POST", {})
    rid = run["run_id"]
    rd, _ = req(base, f"/api/runs/{rid}")
    r_run, r_jobs = rd["run"], rd["jobs"]
    check("PlanRun 固化 plan_revision=1", int(r_run["plan_revision"]) == 1, f"rev={r_run['plan_revision']}")
    snap = r_run["plan_snapshot"]
    check("PlanRun 固化完整 plan_snapshot",
          bool(snap) and snap["name"] == "PE-snap-A"
          and [s["engine"] for s in snap["steps"]] == ["pytest", "locust"]
          and snap["fail_fast"] is True)
    check("快照带非敏感指纹", bool(snap.get("snapshot_fingerprint")))
    check("执行按步骤顺序创建 Job",
          [j["engine"] for j in r_jobs] == ["pytest", "locust"])
    job0 = r_jobs[0]
    fp0 = job0["config_snapshot"]["snapshot_fingerprint"]

    # 后续编辑原计划：改名 / 换步骤 / 关 fail_fast / 换引用
    req(base, f"/api/plans/{spid}", "PUT",
        {"name": "PE-snap-A-renamed", "description": "被改过的定义", "environment_id": env_id,
         "executor_id": ex_id, "asset_source_id": asset_id, "fail_fast": False,
         "steps": [matcheval_step()]})
    cur, _ = req(base, f"/api/plans/{spid}")
    check("原计划确已编辑（rev=2，定义变化）",
          int(cur["revision"]) == 2 and cur["name"] == "PE-snap-A-renamed"
          and [s["engine"] for s in cur["steps"]] == ["matcheval"])

    rd2, _ = req(base, f"/api/runs/{rid}")
    snap2 = rd2["run"]["plan_snapshot"]
    check("编辑原计划后 PlanRun 版本不变", int(rd2["run"]["plan_revision"]) == 1)
    check("编辑原计划后计划快照不变",
          snap2["name"] == "PE-snap-A"
          and [s["engine"] for s in snap2["steps"]] == ["pytest", "locust"]
          and snap2["fail_fast"] is True
          and snap2["snapshot_fingerprint"] == snap["snapshot_fingerprint"])
    check("编辑原计划后 Job 配置快照不漂移",
          req(base, f"/api/jobs/{job0['id']}")[0]["config_snapshot"]["snapshot_fingerprint"] == fp0)
    runs_list, _ = req(base, "/api/runs")
    entry = next(r for r in runs_list if r["id"] == rid)
    check("执行列表计划名取自执行时快照", entry["plan_name"] == "PE-snap-A", entry["plan_name"])

    # 先等本 run 落定（Runner 归档真实报告后再验证报告中心索引）
    wait_terminal(base, rid)

    # 报告中心索引的计划名同样取自执行时快照（后续改名不漂移）
    _, rep_code = req(base, f"/api/reports/{job0['id']}", expect=200)
    check("执行已归档真实报告", rep_code == 200)
    rebuild_report_index(db_path)
    idx, _ = req(base, f"/api/report-center/{job0['id']}")
    check("报告中心计划名取自执行时快照", idx["plan_name"] == "PE-snap-A", idx.get("plan_name"))

    print("== 9. 三步骤组合计划（pytest→locust→matcheval）实跑 ==")
    loc_env = find_by_name(base, "/api/environments", "locust-fixture")
    full_ex = find_by_name(base, "/api/executors", "three-engine-venv")
    if not (loc_env and full_ex):
        check("三引擎执行机与 fixture 环境就绪", False,
              "缺 three-engine-venv 执行机或 locust-fixture 环境（请确认 .venv-locust 存在）")
    else:
        combo = {"name": "PE-三步骤组合", "description": "编辑器创建的三引擎组合计划",
                 "environment_id": loc_env["id"], "executor_id": full_ex["id"],
                 "asset_source_id": 0, "fail_fast": True,
                 "steps": [pytest_step(), locust_step(), matcheval_step()]}
        c, _ = req(base, "/api/plans", "POST", combo)
        cpid = c["id"]
        pre, _ = req(base, f"/api/plans/{cpid}/precheck", "POST")
        check("组合计划预检：三步骤全部就绪", pre["ok"] is True,
              f"missing={pre['missing']}")
        run2, _ = req(base, f"/api/plans/{cpid}/run", "POST", {})
        r2, jobs2 = wait_terminal(base, run2["run_id"])
        check("组合计划聚合为 success", r2["status"] == "success", f"status={r2['status']}")
        check("组合计划三 Job 顺序 pytest→locust→matcheval",
              [j["engine"] for j in jobs2] == ["pytest", "locust", "matcheval"],
              str([j["engine"] for j in jobs2]))
        check("组合计划三 Job 全部成功",
              all(j["status"] == "success" for j in jobs2),
              str([(j["engine"], j["status"]) for j in jobs2]))
        check("组合计划每 Job 产出报告",
              all(req(base, f"/api/reports/{j['id']}", expect=200)[1] == 200 for j in jobs2))
        r2d, _ = req(base, f"/api/runs/{run2['run_id']}")
        check("组合执行固化计划快照（rev=1，三步骤）",
              int(r2d["run"]["plan_revision"]) == 1
              and [s["engine"] for s in r2d["run"]["plan_snapshot"]["steps"]]
              == ["pytest", "locust", "matcheval"])


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)