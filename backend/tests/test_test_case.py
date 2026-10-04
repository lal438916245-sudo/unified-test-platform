"""v0.2 TestCase 定义层 · 单元 + 接口测试。

覆盖（对应实施要求 §25 A–I）：
  A TestCase CRUD        B 定义指纹           C revision
  D case_ref 一致性       E override 规则      F legacy 强兼容
  G 往返保真              H 混合步骤展开        I 内容指纹（same-size content change）

运行：python -m pytest backend/tests/test_test_case.py -q
全部使用隔离临时 SQLite，不依赖运行中的服务，不发起任何执行/压测，不触碰真实运行库。
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from app import config_center as cc
from app import db
from app.api import build_app
from app.api.router import _expand_case_steps, _plan_errors, _plan_from_body
from app.config_center import build_plan_snapshot, dir_content_fingerprint
from app.api.router import PlanIn


def _rmtree_retry(path: str, tries: int = 8) -> None:
    """删除临时目录（带 gc + 重试）。

    ⚠️ Windows 上 sqlite 句柄释放有滞后：db.py 的连接靠引用计数回收（`with get_conn(...)`
    是**事务**上下文管理器，并不 close），因此紧接着 rmtree 常常失败、静默留下一个库文件。
    """
    for _ in range(tries):
        gc.collect()
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.2)


# ---------------------------------------------------------------- 测试装置
class Ctx:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="pe_tc_")
        self.db_path = os.path.join(self.tmp, "db.sqlite")
        db.init_db(self.db_path)
        app = build_app(os.path.join(self.tmp, "no_static"))
        app.state.db_path = self.db_path
        self.client = TestClient(app)
        # 最小受控配置：Plan 引用校验需要"存在且启用"
        self.env = db.create_environment(self.db_path, {
            "name": "offline-eval", "host": "127.0.0.1", "port": 8000,
            "kind": "offline", "protocol": "http", "labels": [], "enabled": 1})
        self.exec_ = db.create_executor(self.db_path, {
            "name": "anaconda", "python_executable": sys.executable, "cwd": self.tmp,
            "python_ref": "", "cwd_root": "", "allowed_runners": ["pytest", "locust", "matcheval"],
            "max_concurrency": 1, "enabled": 1})
        self.asset = db.create_asset_source(self.db_path, {
            "name": "locust-colyseus", "kind": "locust", "scenario_ref": "colyseus-bot",
            "enabled": 1})

    def close(self):
        _rmtree_retry(self.tmp)

    def steps_raw(self, pid):
        con = sqlite3.connect(self.db_path)
        row = con.execute("SELECT steps FROM plans WHERE id=?", (pid,)).fetchone()
        con.close()
        return row[0]


def _case(**kw):
    base = {"name": "用例A", "engine": "pytest", "params": {"args": ["demo/x.py", "-o", "addopts="],
                                                            "timeout_sec": 60}}
    base.update(kw)
    return base


def _create_case(ctx, **kw):
    r = ctx.client.post("/api/test-cases", json=_case(**kw))
    assert r.status_code == 200, r.text
    return r.json()


def _plan_body(ctx, steps, **kw):
    b = {"name": kw.get("name", "计划"), "description": "", "environment_id": ctx.env,
         "executor_id": ctx.exec_, "asset_source_id": ctx.asset, "steps": steps,
         "fail_fast": True, "owner": "local"}
    b.update({k: v for k, v in kw.items() if k != "name"})
    return b


LEGACY_STEP = {"engine": "pytest", "name": "legacy-step", "requires_exclusive": False,
               "params": {"args": ["demo/test_platform_demo.py", "-o", "addopts=", "--tb=short"],
                          "timeout_sec": 120}}


# ================================================================ A. CRUD
def test_a_create_get_list_update_toggle_delete():
    ctx = Ctx()
    try:
        tc = _create_case(ctx)
        assert tc["revision"] == 1
        assert tc["enabled"] == 1
        assert tc["fingerprint"]
        assert tc["params"]["timeout_sec"] == 60

        assert ctx.client.get(f"/api/test-cases/{tc['id']}").json()["name"] == "用例A"
        lst = ctx.client.get("/api/test-cases").json()
        assert [x["id"] for x in lst] == [tc["id"]]
        assert ctx.client.get("/api/test-cases?enabled=1").json()[0]["id"] == tc["id"]

        upd = ctx.client.put(f"/api/test-cases/{tc['id']}",
                             json=_case(name="用例A改")).json()
        assert upd["name"] == "用例A改"

        tog = ctx.client.post(f"/api/test-cases/{tc['id']}/toggle").json()
        assert tog["enabled"] == 0
        assert ctx.client.get("/api/test-cases?enabled=1").json() == []
        assert ctx.client.post(f"/api/test-cases/{tc['id']}/toggle").json()["enabled"] == 1

        assert ctx.client.delete(f"/api/test-cases/{tc['id']}").status_code == 200
        assert ctx.client.get(f"/api/test-cases/{tc['id']}").status_code == 404
    finally:
        ctx.close()


def test_a_rejects_unknown_engine_bad_params_and_duplicate_name():
    ctx = Ctx()
    try:
        assert ctx.client.post("/api/test-cases", json=_case(engine="shell")).status_code == 400
        r = ctx.client.post("/api/test-cases",
                            json=_case(params={"command": "rm -rf /"}))
        assert r.status_code == 400 and "不接受的参数键" in r.text
        r = ctx.client.post("/api/test-cases", json=_case(params={"args": [r"C:\evil.py"]}))
        assert r.status_code == 400
        _create_case(ctx)
        r = ctx.client.post("/api/test-cases", json=_case())
        assert r.status_code == 400 and "同名" in r.text
    finally:
        ctx.close()


def test_a_rejects_asset_source_id_in_params_and_bad_asset_kind():
    ctx = Ctx()
    try:
        r = ctx.client.post("/api/test-cases", json=_case(engine="locust",
                          params={"locustfile": "colyseus-bot", "asset_source_id": 3}))
        assert r.status_code == 400 and "asset_source_id" in r.text
        # asset_kind 与 engine 不相容
        r = ctx.client.post("/api/test-cases", json=_case(engine="locust", asset_kind="matcheval",
                          params={"locustfile": "colyseus-bot"}))
        assert r.status_code == 400 and "不相容" in r.text
        # pytest 不接受 asset_kind
        r = ctx.client.post("/api/test-cases", json=_case(asset_kind="locust"))
        assert r.status_code == 400
    finally:
        ctx.close()


def test_a_delete_refused_when_referenced_by_plan():
    ctx = Ctx()
    try:
        tc = _create_case(ctx)
        ref = {"case_ref": {"id": tc["id"], "revision": tc["revision"],
                            "fingerprint": tc["fingerprint"]}, "override_params": {}}
        assert ctx.client.post("/api/plans", json=_plan_body(ctx, [ref])).status_code == 200
        r = ctx.client.delete(f"/api/test-cases/{tc['id']}")
        assert r.status_code == 409 and "仍被计划引用" in r.text
    finally:
        ctx.close()


# ================================================================ B. 定义指纹
def test_b_fingerprint_changes_only_on_defining_fields():
    base = dict(name="N", engine="locust",
                params={"locustfile": "colyseus-bot", "users": 3},
                requires_exclusive=False, asset_kind="locust")
    fp0 = cc.test_case_fingerprint(**base)
    for k, v in (("name", "N2"), ("engine", "matcheval"),
                 ("params", {"locustfile": "colyseus-bot", "users": 9}),
                 ("requires_exclusive", True), ("asset_kind", "matcheval")):
        d = dict(base); d[k] = v
        assert cc.test_case_fingerprint(**d) != fp0, f"定义字段 {k} 变化未反映到指纹"


def test_b_fingerprint_identical_for_same_definition_regardless_of_key_order():
    a = cc.test_case_fingerprint("N", "locust", {"users": 3, "run_time": "60s"},
                              False, "locust")
    b = cc.test_case_fingerprint("N", "locust", {"run_time": "60s", "users": 3},
                              False, "locust")
    assert a == b, "params 键序不同不应改变定义指纹"


def test_b_non_defining_fields_do_not_change_api_fingerprint():
    ctx = Ctx()
    try:
        tc = _create_case(ctx, description="d1", tags=["a"])
        fp = tc["fingerprint"]
        for patch in ({"description": "d2"}, {"tags": ["a", "b"]}, {"owner": "someone"}):
            body = _case()
            body.update({"description": tc["description"], "tags": tc["tags"],
                         "owner": tc["owner"]})
            body.update(patch)
            got = ctx.client.put(f"/api/test-cases/{tc['id']}", json=body).json()
            assert got["fingerprint"] == fp, f"{patch} 不应改变定义指纹"
    finally:
        ctx.close()


# ================================================================ C. revision
def test_c_revision_increments_only_when_definition_changes():
    ctx = Ctx()
    try:
        tc = _create_case(ctx, description="d0", tags=[])
        assert tc["revision"] == 1

        # 非定义字段 → revision 不变
        body = _case(description="d1", tags=["x"], owner="bob")
        got = ctx.client.put(f"/api/test-cases/{tc['id']}", json=body).json()
        assert got["revision"] == 1, "description/tags/owner 变化不应 bump revision"

        # 定义字段 → revision +1（以及指纹重算）
        body = _case(name="用例A", description="d1", tags=["x"], owner="bob",
                     params={"args": ["demo/y.py", "-o", "addopts="], "timeout_sec": 60})
        got = ctx.client.put(f"/api/test-cases/{tc['id']}", json=body).json()
        assert got["revision"] == 2
        assert got["fingerprint"] != tc["fingerprint"]

        # enabled 切换 → revision 不变
        got = ctx.client.post(f"/api/test-cases/{tc['id']}/toggle").json()
        assert got["revision"] == 2
    finally:
        ctx.close()


def test_c_revision_not_part_of_fingerprint():
    """同名同参同引擎但 revision 不同，定义指纹必须一致（职责分离）。"""
    ctx = Ctx()
    try:
        tc = _create_case(ctx, description="x")
        body = _case(description="y")          # 只改 description
        got = ctx.client.put(f"/api/test-cases/{tc['id']}", json=body).json()
        assert got["revision"] == 1 and got["fingerprint"] == tc["fingerprint"]
    finally:
        ctx.close()


# ================================================================ D. case_ref 一致性
def test_d_case_ref_roundtrip_and_consistency_checks():
    ctx = Ctx()
    try:
        tc = _create_case(ctx, engine="locust", asset_kind="locust",
                          params={"locustfile": "colyseus-bot", "users": 3, "run_time": "60s"})
        good = {"case_ref": {"id": tc["id"], "revision": tc["revision"],
                             "fingerprint": tc["fingerprint"]},
                "override_params": {"run_time": "30s"}, "name": "短跑对照"}
        r = ctx.client.post("/api/plans", json=_plan_body(ctx, [good]))
        assert r.status_code == 200, r.text

        # 不存在的用例
        bad = {"case_ref": {"id": 9999, "revision": 1, "fingerprint": "x"}}
        assert ctx.client.post("/api/plans", json=_plan_body(ctx, [bad])).status_code == 400

        # revision 不一致
        bad = {"case_ref": {"id": tc["id"], "revision": 99, "fingerprint": tc["fingerprint"]}}
        r = ctx.client.post("/api/plans", json=_plan_body(ctx, [bad]))
        assert r.status_code == 400 and "revision" in r.text

        # fingerprint 不一致
        bad = {"case_ref": {"id": tc["id"], "revision": tc["revision"], "fingerprint": "deadbeef"}}
        r = ctx.client.post("/api/plans", json=_plan_body(ctx, [bad]))
        assert r.status_code == 400 and "fingerprint" in r.text

        # engine 不一致（步骤显式写 engine 且与用例不同）
        bad = {"engine": "pytest", "case_ref": {"id": tc["id"], "revision": tc["revision"],
                                                "fingerprint": tc["fingerprint"]}}
        r = ctx.client.post("/api/plans", json=_plan_body(ctx, [bad]))
        assert r.status_code == 400 and "engine" in r.text

        # 停用后不允许被引用
        ctx.client.post(f"/api/test-cases/{tc['id']}/toggle")
        r = ctx.client.post("/api/plans", json=_plan_body(ctx, [good]))
        assert r.status_code == 400 and "已停用" in r.text
    finally:
        ctx.close()


def test_d_case_edited_after_plan_saved_makes_plan_stale_and_run_is_refused():
    ctx = Ctx()
    try:
        tc = _create_case(ctx, engine="locust", asset_kind="locust",
                          params={"locustfile": "colyseus-bot", "users": 3})
        ref = {"case_ref": {"id": tc["id"], "revision": tc["revision"],
                            "fingerprint": tc["fingerprint"]}}
        pid = ctx.client.post("/api/plans", json=_plan_body(ctx, [ref])).json()["id"]
        assert ctx.client.post(f"/api/plans/{pid}/run").status_code == 200

        # 修改用例定义 → 计划引用变陈旧
        ctx.client.put(f"/api/test-cases/{tc['id']}",
                       json=_case(name="用例A", engine="locust", asset_kind="locust",
                                  params={"locustfile": "colyseus-bot", "users": 7}))
        r = ctx.client.post(f"/api/plans/{pid}/run")
        assert r.status_code == 400
        assert "revision" in r.text or "fingerprint" in r.text
        # 且**没有**新增 PlanRun / Job
        con = sqlite3.connect(ctx.db_path)
        assert con.execute("SELECT COUNT(*) FROM plan_runs").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        con.close()
    finally:
        ctx.close()


# ================================================================ E. override
def test_e_override_merges_and_is_validated_as_a_whole():
    ctx = Ctx()
    try:
        tc = _create_case(ctx, engine="locust", asset_kind="locust",
                          params={"locustfile": "colyseus-bot", "users": 3,
                                  "spawn_rate": 1, "run_time": "60s", "timeout_sec": 180})
        ref = {"case_ref": {"id": tc["id"], "revision": tc["revision"],
                            "fingerprint": tc["fingerprint"]},
               "override_params": {"run_time": "30s"}}
        pid = ctx.client.post("/api/plans", json=_plan_body(ctx, [ref])).json()["id"]

        class _S: db_path = ctx.db_path
        class _A: state = _S()
        class _R: app = _A()

        plan = db.get_plan(ctx.db_path, pid)
        expanded, errs = _expand_case_steps(_R(), plan["steps"])
        assert errs == []
        p = expanded[0]["params"]
        assert p["run_time"] == "30s" and p["users"] == 3 and p["locustfile"] == "colyseus-bot"
        assert expanded[0]["case_ref"]["id"] == tc["id"]
        assert expanded[0]["override_params"] == {"run_time": "30s"}
        assert expanded[0]["engine"] == "locust"

        # 非法 override：白名单外的键
        r = ctx.client.post("/api/plans", json=_plan_body(ctx, [
            {**ref, "override_params": {"host": "http://evil"}}]))
        assert r.status_code == 400

        # 非法 override：禁止覆盖 engine / asset_source_id
        for banned in ("engine", "asset_source_id"):
            r = ctx.client.post("/api/plans", json=_plan_body(ctx, [
                {**ref, "override_params": {banned: "x"}}]))
            assert r.status_code == 400, banned

        # 非法 override：合并后整体越界（threshold 只属 matcheval）
        r = ctx.client.post("/api/plans", json=_plan_body(ctx, [
            {**ref, "override_params": {"threshold": 0.9}}]))
        assert r.status_code == 400
    finally:
        ctx.close()


def test_e_override_may_fill_a_whitelisted_key_not_declared_by_case():
    """白名单内、用例未声明的键允许被计划补齐（冻结设计的取舍）。"""
    ctx = Ctx()
    try:
        tc = _create_case(ctx, engine="locust", asset_kind="locust",
                          params={"locustfile": "colyseus-bot", "users": 3})
        ref = {"case_ref": {"id": tc["id"], "revision": tc["revision"],
                            "fingerprint": tc["fingerprint"]},
               "override_params": {"csv_full_history": True}}
        r = ctx.client.post("/api/plans", json=_plan_body(ctx, [ref]))
        assert r.status_code == 200, r.text
    finally:
        ctx.close()


# ================================================================ F. legacy 强兼容
def test_f_legacy_step_serialization_is_byte_identical():
    """PlanStep.to_dict() 对 legacy 步骤必须与 v0.1 的 4 键形态逐字节一致。"""
    legacy = dict(LEGACY_STEP)
    ctx = Ctx()
    try:
        plan = _plan_from_body(PlanIn(**_plan_body(ctx, [legacy])))
        got = json.dumps([s.to_dict() for s in plan.steps], ensure_ascii=False)
        want = json.dumps([{"engine": legacy["engine"], "name": legacy["name"],
                            "requires_exclusive": legacy["requires_exclusive"],
                            "params": legacy["params"]}], ensure_ascii=False)
        assert got == want
        assert "case_ref" not in got and "override_params" not in got
    finally:
        ctx.close()


def test_f_legacy_plan_steps_json_unchanged_through_lifecycle():
    ctx = Ctx()
    try:
        pid = ctx.client.post("/api/plans", json=_plan_body(ctx, [LEGACY_STEP])).json()["id"]
        raw1 = ctx.steps_raw(pid)
        assert "case_ref" not in raw1

        got = ctx.client.get(f"/api/plans/{pid}").json()
        # 直接回存 GET 的结果（模拟编辑器"加载→保存"）
        assert ctx.client.put(f"/api/plans/{pid}", json=got).status_code == 200
        raw2 = ctx.steps_raw(pid)
        assert raw1 == raw2, "legacy steps JSON 在加载→保存往返后发生了变化"

        # 复制计划也必须是同形态
        dup = ctx.client.post(f"/api/plans/{pid}/copy", json={}).json()["id"]
        assert ctx.steps_raw(dup) == raw1
    finally:
        ctx.close()


# 真实运行库 plan#1–8 的 pytest 步骤带 params={"timeout_sec":..,"cwd":"<受控根下的绝对路径>","args":[..]}。
# 注意 `cwd` 在后端白名单里（_STEP_PARAM_KEYS["pytest"] 含 cwd），但**前端没有对应控件**
# （PL_MANAGED_KEYS["pytest"] 只有 args/cwd_root/timeout_sec）——所以它是"表单不管理"的键。
# 若后端在"加载→保存"往返中重建 params，这类键会静默消失、键序也会被重排。
# `cwd` 必须是**受控工作目录根之下的绝对路径**（否则 validate_step_params 直接 400），
# 因此从本文件位置派生，绝不硬编码本机盘符 —— 本文件会进入仓库，须对任意 runner 可用。
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # platform/backend

UNMANAGED_STEP = {
    "engine": "pytest", "name": "legacy-with-unmanaged", "requires_exclusive": False,
    "params": {"timeout_sec": 60, "cwd": _BACKEND_DIR,
               "args": ["demo/test_platform_demo.py"], "asset_source_id": 7},
}


def test_f_unmanaged_params_survive_roundtrip():
    """表单不管理的 params 键（cwd / asset_source_id）经 API 往返必须原样保留，且键序不变。"""
    ctx = Ctx()
    try:
        pid = ctx.client.post("/api/plans", json=_plan_body(ctx, [UNMANAGED_STEP])).json()["id"]
        raw1 = ctx.steps_raw(pid)
        p1 = json.loads(raw1)[0]["params"]
        assert p1 == UNMANAGED_STEP["params"], p1
        assert list(p1.keys()) == list(UNMANAGED_STEP["params"].keys()), list(p1.keys())

        got = ctx.client.get(f"/api/plans/{pid}").json()
        assert ctx.client.put(f"/api/plans/{pid}", json=got).status_code == 200
        raw2 = ctx.steps_raw(pid)
        assert raw1 == raw2, f"未管理键在往返后丢失或键序改变：\n 前={raw1}\n 后={raw2}"
        assert "cwd" in raw2 and "asset_source_id" in raw2
    finally:
        ctx.close()


def test_f_legacy_plan_snapshot_fingerprint_stable_across_runs():
    ctx = Ctx()
    try:
        pid = ctx.client.post("/api/plans", json=_plan_body(ctx, [LEGACY_STEP])).json()["id"]
        plan = db.get_plan(ctx.db_path, pid)
        fp1 = build_plan_snapshot(plan)["snapshot_fingerprint"]
        fp2 = build_plan_snapshot(db.get_plan(ctx.db_path, pid))["snapshot_fingerprint"]
        assert fp1 == fp2
        # 两次 run 的 plan_snapshot 必须一致（运行不改动快照内容）
        assert ctx.client.post(f"/api/plans/{pid}/run").status_code == 200
        assert ctx.client.post(f"/api/plans/{pid}/run").status_code == 200
        con = sqlite3.connect(ctx.db_path)
        snaps = {r[0] for r in con.execute(
            "SELECT plan_snapshot FROM plan_runs ORDER BY id")}
        con.close()
        assert len(snaps) == 1
        assert json.loads(snaps.pop())["snapshot_fingerprint"] == fp1
    finally:
        ctx.close()


# ================================================================ G. 往返保真
def test_g_case_ref_survives_post_get_put_get():
    ctx = Ctx()
    try:
        tc = _create_case(ctx, engine="locust", asset_kind="locust",
                          params={"locustfile": "colyseus-bot", "users": 3, "run_time": "60s"})
        ref = {"case_ref": {"id": tc["id"], "revision": tc["revision"],
                            "fingerprint": tc["fingerprint"]},
               "override_params": {"run_time": "45s"}, "name": "自定义名"}
        pid = ctx.client.post("/api/plans", json=_plan_body(ctx, [ref])).json()["id"]

        got = ctx.client.get(f"/api/plans/{pid}").json()
        s = got["steps"][0]
        assert s["case_ref"] == ref["case_ref"]
        assert s["override_params"] == {"run_time": "45s"}
        assert s["name"] == "自定义名"
        assert "engine" not in s and "params" not in s, "引用形态不应写 engine/params"

        # 编辑器"加载 → 保存"再取回
        assert ctx.client.put(f"/api/plans/{pid}", json=got).status_code == 200
        got2 = ctx.client.get(f"/api/plans/{pid}").json()
        assert got2["steps"][0]["case_ref"] == ref["case_ref"]
        assert got2["steps"][0]["override_params"] == {"run_time": "45s"}
    finally:
        ctx.close()


# ================================================================ H. 混合步骤
def test_h_mixed_steps_expand_in_order():
    ctx = Ctx()
    try:
        tc1 = _create_case(ctx, name="用例1", engine="pytest",
                           params={"args": ["demo/a.py", "-o", "addopts="]})
        tc2 = _create_case(ctx, name="用例2", engine="locust", asset_kind="locust",
                           params={"locustfile": "colyseus-bot", "users": 5})
        steps = [
            {"case_ref": {"id": tc1["id"], "revision": tc1["revision"],
                          "fingerprint": tc1["fingerprint"]}},
            dict(LEGACY_STEP),
            {"case_ref": {"id": tc2["id"], "revision": tc2["revision"],
                          "fingerprint": tc2["fingerprint"]},
             "override_params": {"users": 2}},
        ]
        pid = ctx.client.post("/api/plans", json=_plan_body(ctx, steps)).json()["id"]
        plan = db.get_plan(ctx.db_path, pid)

        class _S: db_path = ctx.db_path
        class _A: state = _S()
        class _R: app = _A()

        expanded, errs = _expand_case_steps(_R(), plan["steps"])
        assert errs == []
        assert [e["engine"] for e in expanded] == ["pytest", "pytest", "locust"]
        assert expanded[0]["name"] == tc1["name"]
        assert expanded[1]["name"] == LEGACY_STEP["name"]
        assert expanded[1]["params"] == LEGACY_STEP["params"]
        assert expanded[2]["params"]["users"] == 2

        # 快照里：case 步骤带溯源键，legacy 步骤不带
        snap = build_plan_snapshot({**plan, "steps": expanded})
        assert "case_ref" in snap["steps"][0] and "case_ref" not in snap["steps"][1]
        assert snap["steps"][0]["params"]["args"] == ["demo/a.py", "-o", "addopts="]
    finally:
        ctx.close()


def test_h_expansion_happens_before_snapshot_and_jobs_get_effective_params():
    ctx = Ctx()
    try:
        tc = _create_case(ctx, engine="locust", asset_kind="locust",
                          params={"locustfile": "colyseus-bot", "users": 3, "run_time": "60s"})
        ref = {"case_ref": {"id": tc["id"], "revision": tc["revision"],
                            "fingerprint": tc["fingerprint"]},
               "override_params": {"run_time": "30s"}}
        pid = ctx.client.post("/api/plans", json=_plan_body(ctx, [ref])).json()["id"]
        rid = ctx.client.post(f"/api/plans/{pid}/run").json()["run_id"]

        con = sqlite3.connect(ctx.db_path)
        job_params = json.loads(con.execute(
            "SELECT params FROM jobs WHERE plan_run_id=?", (rid,)).fetchone()[0])
        snap = json.loads(con.execute(
            "SELECT plan_snapshot FROM plan_runs WHERE id=?", (rid,)).fetchone()[0])
        snap_cfg = json.loads(con.execute(
            "SELECT config_snapshot FROM jobs WHERE plan_run_id=?", (rid,)).fetchone()[0])
        con.close()

        assert job_params["run_time"] == "30s"          # 有效值进入 Job
        s0 = snap["steps"][0]
        assert s0["params"]["run_time"] == "30s"        # 有效值进入快照
        assert s0["case_ref"]["id"] == tc["id"]         # 同时保留溯源
        assert s0["override_params"] == {"run_time": "30s"}
        # 内容指纹进入 config_snapshot（与定义指纹分离）
        assert "content" in snap_cfg and snap_cfg["content"]["fingerprint"]
        assert snap_cfg["content"]["engine"] == "locust"
        assert snap_cfg["content"]["sources"]["locust_script"]["key"] == "colyseus-bot"
    finally:
        ctx.close()


# ================================================================ I. 内容指纹
def test_i_dir_content_fingerprint_detects_same_size_content_change():
    """P0：v0.1 的 dir_fingerprint 只算 name+size，同尺寸换内容测不出来；新机制必须能测出。"""
    tmp = tempfile.mkdtemp(prefix="pe_cfp_")
    try:
        p1 = os.path.join(tmp, "t.png"); p2 = os.path.join(tmp, "s.png")
        with open(p1, "wb") as f:
            f.write(b"AAAAAAAA")
        with open(p2, "wb") as f:
            f.write(b"BBBBBBBB")

        old1 = cc.dir_fingerprint(tmp)               # v0.1 口径
        new1 = dir_content_fingerprint(tmp)["fp"]    # v0.2 口径
        assert old1 and new1

        # 同尺寸（8B）换内容
        with open(p1, "wb") as f:
            f.write(b"CCCCCCCC")
        old2 = cc.dir_fingerprint(tmp)
        new2 = dir_content_fingerprint(tmp)["fp"]

        assert old1 == old2, "前提：v0.1 口径确实测不出同尺寸内容变化"
        assert new1 != new2, "v0.2 内容指纹必须能测出同尺寸内容变化"
    finally:
        _rmtree_retry(tmp)


def test_i_file_content_fingerprint_changes_with_content_and_is_path_agnostic():
    tmp1 = tempfile.mkdtemp(prefix="pe_fcfp_a_")
    tmp2 = tempfile.mkdtemp(prefix="pe_fcfp_b_")
    try:
        f1 = os.path.join(tmp1, "x.py"); f2 = os.path.join(tmp2, "x.py")
        for p, data in ((f1, b"print(1)\n"), (f2, b"print(1)\n")):
            with open(p, "wb") as f:
                f.write(data)
        a = cc.file_content_fingerprint(f1)["fp"]
        b = cc.file_content_fingerprint(f2)["fp"]
        assert a == b, "同内容不同目录应得同一指纹（指纹不含绝对路径）"
        with open(f1, "wb") as f:
            f.write(b"print(2)\n")
        assert cc.file_content_fingerprint(f1)["fp"] != a
    finally:
        _rmtree_retry(tmp1)
        _rmtree_retry(tmp2)


def test_i_content_fingerprint_scope_excludes_pytest_and_is_cached():
    a = cc.build_content_fingerprint("pytest", {"cwd_root": "privahigh-backend"})
    assert a["sources"] == {} and a["fingerprint"], "pytest 不纳入内容指纹（v0.2 冻结范围）"
    b = cc.build_content_fingerprint("locust", {"locustfile": "no-such-scenario"})
    assert b["sources"]["locust_script"]["exists"] is False
    assert b["fingerprint"] != a["fingerprint"]
