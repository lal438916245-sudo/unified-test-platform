"""配置中心 · 隔离验证（不依赖任何外部服务）。

在独立临时数据目录 + 独立 SQLite 中启动进程内 TestClient，覆盖：
  三类配置实体 CRUD / 启停 / 预检 / 引用约束 / 非法 ID-key 拒绝 /
  脱敏（无明文绝对路径、无密钥值）/ Job 配置快照不可变 /
  配置实体变更后历史报告仍保留原比较指纹。
只做受控低风险探测，绝不连接真实 Colyseus/Unity/生产环境，绝不发起压测或完整测试。

用法：python verify_config_center.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# 先落隔离参数，再导入任何会读取它们的模块（与 run.py 的启动顺序一致）
TMP = tempfile.mkdtemp(prefix="verify_cfg_")
os.environ["PLATFORM_DATA_DIR"] = os.path.join(TMP, "data")
os.environ["PLATFORM_DB"] = os.path.join(TMP, "db.sqlite")
# 已知密钥值：用于断言任何对外响应/索引都不泄露它
SECRET_VALUE = "super-secret-token-value-xyz"
os.environ["CC_VERIFY_SECRET"] = SECRET_VALUE

FAILED = [False]
ABS_MARKERS = ("F:\\", "C:\\", "/usr/", "/home/")


def check(name, cond, extra=""):
    if not cond:
        FAILED[0] = True
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")


def has_abs(raw: str) -> bool:
    return any(m in raw for m in ABS_MARKERS)


def main() -> int:
    from fastapi.testclient import TestClient

    from app import db
    from app.api import build_app
    from app.report_center import rebuild_report_index
    from preset import seed_if_empty

    db_path = os.environ["PLATFORM_DB"]
    db.init_db(db_path)
    seed_if_empty(db_path, self_url="http://127.0.0.1:8123")
    rebuild_report_index(db_path)

    frontend = os.path.join(os.path.dirname(BACKEND), "frontend")
    app = build_app(frontend)
    app.state.db_path = db_path
    app.state.orchestrator = None          # 本脚本只验证配置链路，不执行 Job
    client = TestClient(app)

    def api(method, path, body=None, expect=None):
        r = client.request(method, path, json=body) if body is not None \
            else client.request(method, path)
        if expect is not None and r.status_code != expect:
            raise AssertionError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r

    print("== 1. 受控目录 catalog（只暴露 key/label，绝不暴露路径/密钥） ==")
    cat = api("GET", "/api/config/catalog", expect=200).json()
    raw = json.dumps(cat, ensure_ascii=False)
    check("catalog 无明文绝对路径", not has_abs(raw))
    check("catalog 暴露受控 key（解释器/资产）",
          bool(cat["interpreters"]) and bool(cat["asset"]["datasets"]))
    check("catalog 标注弃用环境变量（过渡层）",
          bool(cat["deprecated_env"]["names"]) and "弃用" in cat["deprecated_env"]["note"])

    print("== 2. 引擎就绪度（可运行 / 缺少什么配置，不发起执行） ==")
    ready = api("GET", "/api/config/readiness", expect=200).json()["engines"]
    check("就绪度覆盖三引擎",
          {e["engine"] for e in ready} == {"pytest", "locust", "matcheval"})
    check("就绪度含缺失项与下一步字段",
          all("runnable" in e and "missing" in e and "next_steps" in e for e in ready))
    for e in ready:
        print(f"   · {e['engine']}: runnable={e['runnable']} missing={e['missing']}")

    print("== 3. Environment：CRUD / 预检 / 脱敏 / 非法输入 ==")
    e = api("POST", "/api/config/environments", {
        "name": "verify-http", "kind": "http", "protocol": "http",
        "host": "127.0.0.1", "port": 9, "labels": ["verify"],
        "secret_ref": "CC_VERIFY_SECRET", "secret_kind": "env_var"}, expect=200).json()
    eid = e["id"]
    check("环境视图不含密钥值", SECRET_VALUE not in json.dumps(e))
    check("secret 仅暴露可用性（可用）", e["secret_available"] is True)
    u = api("PUT", f"/api/config/environments/{eid}",
            {"labels": ["verify", "updated"]}, expect=200).json()
    check("环境编辑生效", "updated" in u["labels"])
    pc = api("POST", f"/api/config/environments/{eid}/precheck", expect=200).json()
    check("环境预检：TCP 不可达→缺失项+下一步",
          pc["ok"] is False and bool(pc["missing"]) and bool(pc["next_steps"]),
          f"missing={pc['missing']}")
    check("环境停用生效",
          api("POST", f"/api/config/environments/{eid}/toggle", expect=200).json()["enabled"] == 0)
    api("POST", f"/api/config/environments/{eid}/toggle", expect=200)
    off = api("POST", "/api/config/environments",
              {"name": "verify-offline", "kind": "offline", "protocol": "http"},
              expect=200).json()
    pco = api("POST", f"/api/config/environments/{off['id']}/precheck", expect=200).json()
    check("offline 环境无需连通性检查（ok=None）", pco["ok"] is None)
    check("拒绝路径型 host",
          api("POST", "/api/config/environments",
              {"name": "bad", "kind": "http", "host": r"C:\game\server", "port": 1}
              ).status_code == 400)
    check("拒绝非法 kind",
          api("POST", "/api/config/environments",
              {"name": "bad", "kind": "nope", "host": "h", "port": 1}).status_code == 400)
    check("拒绝非法 secret 引用名",
          api("POST", "/api/config/environments",
              {"name": "bad", "kind": "http", "host": "h", "port": 1,
               "secret_kind": "env_var", "secret_ref": "bad-name!"}).status_code == 400)
    check("不存在的环境 → 404",
          api("PUT", "/api/config/environments/99999", {"name": "x"}).status_code == 404)
    check("不存在环境的预检 → 404",
          api("POST", "/api/config/environments/99999/precheck").status_code == 404)

    print("== 4. Executor：CRUD / 预检 / 缺配置给可行动提示（不误报 Runner 缺陷） ==")
    x = api("POST", "/api/config/executors", {
        "name": "verify-exec", "python_ref": "anaconda", "cwd_root": "backend-demo",
        "allowed_runners": ["pytest"], "max_concurrency": 1}, expect=200).json()
    xid = x["id"]
    check("执行机视图清空明文路径",
          x["python_executable"] == "" and x["cwd"] == "" and not has_abs(json.dumps(x)))
    check("执行机解释器可用", x["python_available"] is True, f"masked={x['python_masked']}")
    pcx = api("POST", f"/api/config/executors/{xid}/precheck", expect=200).json()
    check("执行机预检：可运行", pcx["ok"] is True, f"missing={pcx['missing']}")

    nx = api("POST", "/api/config/executors", {
        "name": "verify-exec-nopy", "python_ref": "", "allowed_runners": ["matcheval"]},
        expect=200).json()
    pcn = api("POST", f"/api/config/executors/{nx['id']}/precheck", expect=200).json()
    check("缺解释器→可行动提示",
          pcn["ok"] is False and any("解释器" in m for m in pcn["missing"])
          and bool(pcn["next_steps"]), f"missing={pcn['missing']}")
    check("缺配置不误报 Runner 缺陷",
          "runner-error" not in json.dumps(pcn) and "Runner 缺陷" not in json.dumps(pcn))

    mx = api("POST", "/api/config/executors", {
        "name": "verify-exec-match", "python_ref": "anaconda", "allowed_runners": ["matcheval"]},
        expect=200).json()
    pcm = api("POST", f"/api/config/executors/{mx['id']}/precheck", expect=200).json()
    check("MatchEval 依赖：可运行 或 给出可行动依赖提示",
          pcm["ok"] is True or (bool(pcm["next_steps"]) and any("依赖" in m for m in pcm["missing"])),
          f"ok={pcm['ok']} missing={pcm['missing']}")
    check("拒绝路径型 python_ref",
          api("POST", "/api/config/executors",
              {"name": "bad", "python_ref": r"C:\Anaconda3\python.exe"}).status_code == 400)
    check("拒绝非受控 allowed_runners",
          api("POST", "/api/config/executors",
              {"name": "bad", "allowed_runners": ["nope"]}).status_code == 400)

    print("== 5. AssetSource：CRUD / 预检 / 指纹 / 非法引用 ==")
    a = api("POST", "/api/config/asset-sources", {
        "name": "verify-match-assets", "kind": "matcheval", "dataset_ref": "fixture-small"},
        expect=200).json()
    check("资产视图脱敏（无明文目录）", not has_abs(json.dumps(a)) and bool(a["dataset_masked"]))
    pca = api("POST", f"/api/config/asset-sources/{a['id']}/precheck", expect=200).json()
    check("matcheval fixture 资产预检通过", pca["ok"] is True, f"missing={pca['missing']}")
    check("资产预检产出非敏感指纹",
          bool(pca["fingerprint"]) and bool(pca.get("dir_fingerprint")))

    l = api("POST", "/api/config/asset-sources", {
        "name": "verify-locust-assets", "kind": "locust", "scenario_ref": "http-fixture"},
        expect=200).json()
    pcl = api("POST", f"/api/config/asset-sources/{l['id']}/precheck", expect=200).json()
    check("locust 场景资产预检通过", pcl["ok"] is True, f"missing={pcl['missing']}")
    check("拒绝非受控 dataset 引用",
          api("POST", "/api/config/asset-sources",
              {"name": "bad", "kind": "matcheval", "dataset_ref": "real-assets"}
              ).status_code == 400)
    check("拒绝路径型场景引用",
          api("POST", "/api/config/asset-sources",
              {"name": "bad", "kind": "locust", "scenario_ref": r"C:\x\locustfile.py"}
              ).status_code == 400)

    ax = api("POST", "/api/config/asset-sources", {
        "name": "verify-airtest-assets", "kind": "matcheval",
        "dataset_ref": "fixture-small", "airtest_ref": "airtest-master"}, expect=200).json()
    pcax = api("POST", f"/api/config/asset-sources/{ax['id']}/precheck", expect=200).json()
    if any("Airtest" in m for m in pcax["missing"]):
        check("缺 Airtest 资产→可行动提示（须用户录入）",
              bool(pcax["next_steps"]) and "录入" in "".join(pcax["next_steps"]),
              f"next={pcax['next_steps']}")
    else:
        check("Airtest 资产已就位（无需提示）", True)

    print("== 6. Plan 引用约束 + 步骤参数白名单（只认受控 ID/key） ==")
    api("POST", f"/api/config/executors/{xid}/toggle", expect=200)   # 停用
    bad = api("POST", "/api/plans", {
        "name": "verify-bad-ref", "environment_id": eid, "executor_id": xid,
        "steps": [{"engine": "pytest", "name": "s",
                   "params": {"args": ["demo/test_platform_demo.py"]}}]})
    check("引用停用执行机被拒", bad.status_code == 400 and "停用" in bad.text)
    api("POST", f"/api/config/executors/{xid}/toggle", expect=200)   # 恢复
    check("拒绝任意命令型步骤参数",
          api("POST", "/api/plans", {
              "name": "verify-bad-step", "environment_id": eid, "executor_id": xid,
              "steps": [{"engine": "pytest", "name": "s",
                         "params": {"command": "rm -rf /"}}]}).status_code == 400)
    check("拒绝绝对路径型步骤参数",
          api("POST", "/api/plans", {
              "name": "verify-bad-step2", "environment_id": eid, "executor_id": xid,
              "steps": [{"engine": "pytest", "name": "s",
                         "params": {"args": [r"C:\evil.py"]}}]}).status_code == 400)
    check("拒绝任意场景路径步骤参数",
          api("POST", "/api/plans", {
              "name": "verify-bad-step3", "environment_id": eid, "executor_id": xid,
              "steps": [{"engine": "locust", "name": "s",
                         "params": {"locustfile": r"C:\evil.py"}}]}).status_code == 400)

    print("== 7. Job 配置快照不可变 + 历史比较指纹不漂移 ==")
    plan = api("POST", "/api/plans", {
        "name": "verify-snap-plan", "environment_id": eid, "executor_id": xid,
        "asset_source_id": l["id"],
        "steps": [{"engine": "locust", "name": "l",
                   "params": {"locustfile": "http-fixture", "users": 4, "spawn_rate": 2,
                              "run_time": "12s", "timeout_sec": 120}}]}, expect=200).json()
    run = api("POST", f"/api/plans/{plan['id']}/run", {}, expect=200).json()
    jobs = api("GET", f"/api/runs/{run['run_id']}", expect=200).json()["jobs"]
    job = jobs[0]
    snap = job["config_snapshot"]
    check("Job 固化配置快照", bool(snap) and snap["environment"]["name"] == "verify-http")
    check("快照脱敏（无明文路径/密钥）",
          not has_abs(json.dumps(snap)) and SECRET_VALUE not in json.dumps(snap))
    fp0 = snap["snapshot_fingerprint"]

    api("PUT", f"/api/config/environments/{eid}",
        {"name": "verify-http-renamed", "host": "10.9.9.9"}, expect=200)
    job2 = api("GET", f"/api/jobs/{job['id']}", expect=200).json()
    check("编辑配置实体不改历史 Job 快照",
          job2["config_snapshot"]["snapshot_fingerprint"] == fp0
          and job2["config_snapshot"]["environment"]["name"] == "verify-http")

    # 走真实迁移路径：把已归档事实报告派生为 report_index（与 run.py 启动一致）
    db.save_report(db_path, job["id"], run["run_id"], "reports/verify.json", {
        "engine": "locust", "status": "success", "schema_version": "1.0",
        "engine_data_schema": "locust/1.0", "started_at": "2026-09-20T10:00:00",
        "ended_at": "2026-09-20T10:01:00", "summary": {"duration_ms": 60000},
        "metrics": {"rps": 12.5, "failure_rate": 0.0, "p95_ms": 80},
        "artifacts": [], "engine_data": {}})
    rebuild_report_index(db_path)
    idx0 = api("GET", f"/api/report-center/{job['id']}", expect=200).json()
    check("历史报告环境身份取自 Job 快照", idx0["env_name"] == "verify-http")
    api("PUT", f"/api/config/environments/{eid}",
        {"name": "verify-http-again", "host": "10.1.1.1"}, expect=200)
    rebuild_report_index(db_path)
    idx1 = api("GET", f"/api/report-center/{job['id']}", expect=200).json()
    check("配置变更后历史比较指纹不漂移",
          idx1["comparator"] == idx0["comparator"] and idx1["env_name"] == "verify-http")
    check("报告索引不泄露密钥/明文路径",
          SECRET_VALUE not in json.dumps(idx1) and not has_abs(json.dumps(idx1)))

    print("== 8. 列表接口总检：一律脱敏 ==")
    for path in ("/api/environments", "/api/executors", "/api/asset-sources"):
        body = api("GET", path, expect=200).text
        check(f"{path} 无密钥值", SECRET_VALUE not in body)
        check(f"{path} 无明文绝对路径", not has_abs(body))

    print("\n========== 结果 ==========")
    print("ALL PASS" if not FAILED[0] else "HAS FAILURE")
    return 1 if FAILED[0] else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)