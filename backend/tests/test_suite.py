"""v0.2 TestSuite 定义层 · 单元 + 接口测试（Step 2）。

覆盖：
  A Suite CRUD 流程（创建 → 查询 → 修改 → 删除）
  B revision / fingerprint 语义（定义字段变才 +1；enabled/描述/标签/负责人不变）
  C 引用保护（Suite 引用 Case → 删除 Case 必须失败；Plan 引用行为保持不变）
  D 校验与 I1 不变量（拒绝执行作用域字段、不存在/停用/重复用例、同名、缺失）
  E 指纹本身的性质（顺序敏感、元素键序不敏感、非定义字段无关）
  F 冻结回归（legacy inline 计划与 case_ref 计划行为不变）

全部使用**隔离临时 SQLite**，不依赖运行中的服务，不发起任何执行/压测，
不触碰真实运行库 `platform/data/db.sqlite`。

运行：python -m pytest backend/tests/test_suite.py -q
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app import config_center as cc  # noqa: E402
from app import db  # noqa: E402
from app.api import build_app  # noqa: E402


def _rmtree_retry(path: str, tries: int = 8) -> None:
    """删除临时目录（带 gc + 重试）。

    ⚠️ Windows 上 sqlite 句柄释放有滞后：db.py 的连接靠引用计数回收（`with get_conn(...)`
    是**事务**上下文管理器，并不 close），因此紧接着 rmtree 常常失败、静默留下一个库文件。
    先 gc.collect() 再重试即可稳定删掉，避免每次跑测试都留一地临时库。
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
        self.tmp = tempfile.mkdtemp(prefix="pe_suite_")
        self.db_path = os.path.join(self.tmp, "db.sqlite")
        db.init_db(self.db_path)
        app = build_app(os.path.join(self.tmp, "no_static"))
        app.state.db_path = self.db_path
        self.client = TestClient(app)

    # ---- 便捷构造 ----
    def case(self, name, engine="pytest", params=None, enabled=True):
        r = self.client.post("/api/test-cases", json={
            "name": name, "description": "", "engine": engine,
            "params": params or {"timeout_sec": 60}, "requires_exclusive": False,
            "asset_kind": "", "owner": "local", "tags": [], "enabled": enabled})
        assert r.status_code == 200, r.text
        return r.json()

    def ref(self, case_obj):
        return {"id": case_obj["id"], "revision": case_obj["revision"],
                "fingerprint": case_obj["fingerprint"]}

    def post_suite(self, name, cases, **kw):
        body = {"name": name, "cases": cases}
        body.update(kw)
        return self.client.post("/api/suites", json=body)

    def suite(self, name, cases, **kw):
        r = self.post_suite(name, cases, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def put_suite(self, sid, name, cases, **kw):
        body = {"name": name, "cases": cases}
        body.update(kw)
        return self.client.put(f"/api/suites/{sid}", json=body)

    def plan_env(self):
        env = db.create_environment(self.db_path, {
            "name": "env1", "host": "127.0.0.1", "port": 8000, "kind": "offline",
            "protocol": "http", "labels": [], "enabled": 1})
        ex = db.create_executor(self.db_path, {
            "name": "ex1", "python_executable": sys.executable, "cwd": self.tmp,
            "python_ref": "", "cwd_root": "",
            "allowed_runners": ["pytest", "locust", "matcheval"],
            "max_concurrency": 1, "enabled": 1})
        return env, ex

    def close(self):
        _rmtree_retry(self.tmp)


def _detail(r):
    """取 HTTPException 的 detail 文本。"""
    try:
        return r.json().get("detail", "")
    except Exception:  # noqa: BLE001
        return r.text


# ================================================================ A. CRUD


def test_a1_suite_crud_roundtrip():
    c = Ctx()
    try:
        c1 = c.case("用例甲")
        c2 = c.case("用例乙")
        payload = [c.ref(c1), c.ref(c2)]

        # --- 创建 ---
        r = c.post_suite("冒烟套件", payload, description="第一步烟测",
                         owner="local", tags=["smoke", "v0.2"])
        assert r.status_code == 200, r.text
        s = r.json()
        assert s["id"] > 0
        assert s["revision"] == 1
        assert s["enabled"] == 1
        assert s["fingerprint"] and len(s["fingerprint"]) == 12
        assert s["cases"] == payload, "cases 应原样规范化往返"
        assert s["tags"] == ["smoke", "v0.2"]
        assert s["description"] == "第一步烟测"
        assert s["created_at"] and s["updated_at"]
        # 响应字段与要求一致
        for k in ("id", "name", "description", "cases", "fingerprint", "revision",
                  "enabled", "owner", "tags", "created_at", "updated_at"):
            assert k in s, f"响应缺字段 {k}"

        # --- 查询 ---
        assert c.client.get(f"/api/suites/{s['id']}").json() == s
        lst = c.client.get("/api/suites").json()
        assert [x["id"] for x in lst] == [s["id"]]
        assert c.client.get("/api/suites?enabled=1").json()[0]["id"] == s["id"]

        # --- 修改（整体替换 cases：删掉一条）---
        r = c.put_suite(s["id"], "冒烟套件", [c.ref(c1)], description="第一步烟测")
        assert r.status_code == 200, r.text
        s2 = r.json()
        assert [x["id"] for x in s2["cases"]] == [c1["id"]]
        assert s2["revision"] == 2, "定义变化必须 revision+1"
        assert s2["fingerprint"] != s["fingerprint"]

        # --- 删除 ---
        assert c.client.delete(f"/api/suites/{s['id']}").json() == {"deleted": s["id"]}
        assert c.client.get(f"/api/suites/{s['id']}").status_code == 404
        assert c.client.get("/api/suites").json() == []
    finally:
        c.close()


def test_a2_suite_404s():
    c = Ctx()
    try:
        assert c.client.get("/api/suites/999").status_code == 404
        assert c.put_suite(999, "x", []).status_code == 404
        assert c.client.post("/api/suites/999/toggle").status_code == 404
        assert c.client.delete("/api/suites/999").status_code == 404
    finally:
        c.close()


# ================================================================ B. revision


def test_b1_definition_change_bumps_revision():
    c = Ctx()
    try:
        c1 = c.case("甲")
        c2 = c.case("乙")
        s = c.suite("套件", [c.ref(c1)])
        assert s["revision"] == 1

        # 改 name → +1
        r = c.put_suite(s["id"], "套件改名", [c.ref(c1)])
        assert r.json()["revision"] == 2

        # 改 cases（追加）→ +1
        r = c.put_suite(s["id"], "套件改名", [c.ref(c1), c.ref(c2)])
        assert r.json()["revision"] == 3

        # 只调换顺序 → +1（顺序改变执行语义）
        r = c.put_suite(s["id"], "套件改名", [c.ref(c2), c.ref(c1)])
        assert r.json()["revision"] == 4
        assert r.status_code == 200
    finally:
        c.close()


def test_b2_non_definition_change_keeps_revision():
    c = Ctx()
    try:
        c1 = c.case("甲")
        cases = [c.ref(c1)]
        s = c.suite("套件", cases, description="原说明", tags=["a"], owner="local")
        fp0, rev0 = s["fingerprint"], s["revision"]

        # 只改 description / tags / owner → revision 不变，指纹不变
        for kw in ({"description": "改过的说明"}, {"tags": ["b", "c"]}, {"owner": "someone"}):
            r = c.put_suite(s["id"], "套件", cases, **kw)
            assert r.status_code == 200, r.text
            got = r.json()
            assert got["revision"] == rev0, f"{kw} 不应 bump revision"
            assert got["fingerprint"] == fp0, f"{kw} 不应改变定义指纹"

        # 只改 enabled（经 PUT）→ revision 不变
        r = c.put_suite(s["id"], "套件", cases, enabled=False)
        got = r.json()
        assert got["enabled"] == 0
        assert got["revision"] == rev0, "enabled 不属定义，不应 bump revision"
        assert got["fingerprint"] == fp0

        # 经 toggle → revision 不变
        r = c.client.post(f"/api/suites/{s['id']}/toggle")
        got = r.json()
        assert got["enabled"] == 1
        assert got["revision"] == rev0, "toggle 不应 bump revision"

        # 定义完全相同的 PUT（幂等）→ revision 不变
        r = c.put_suite(s["id"], "套件", cases, description="改过的说明",
                        tags=["b", "c"], owner="someone")
        assert r.json()["revision"] == rev0, "定义未变化的重复保存不应版本漂移"
    finally:
        c.close()


# ============================================================ C. 引用保护


def test_c1_suite_reference_blocks_case_delete():
    c = Ctx()
    try:
        c1 = c.case("被套件引用的用例")
        c2 = c.case("没人引用的用例")
        s = c.suite("套件", [c.ref(c1)])

        # 被 Suite 引用 → 删除失败 409，且错误里指明套件 id
        r = c.client.delete(f"/api/test-cases/{c1['id']}")
        assert r.status_code == 409, r.text
        assert "测试套件" in _detail(r)
        assert str(s["id"]) in _detail(r)
        assert db.get_test_case(c.db_path, c1["id"]) is not None, "用例必须仍在"

        # 未被引用 → 删除成功
        assert c.client.delete(f"/api/test-cases/{c2['id']}").status_code == 200

        # 从套件移除后 → 删除成功
        c.put_suite(s["id"], "套件", [])
        assert c.client.delete(f"/api/test-cases/{c1['id']}").status_code == 200
    finally:
        c.close()


def test_c2_plan_reference_behavior_unchanged():
    """回归：Plan 引用检查的**行为与错误文案**保持 v0.2 一阶段原样。"""
    c = Ctx()
    try:
        env, ex = c.plan_env()
        c1 = c.case("被计划引用的用例")
        r = c.client.post("/api/plans", json={
            "name": "P", "description": "", "environment_id": env, "executor_id": ex,
            "asset_source_id": 0, "fail_fast": True, "owner": "local",
            "steps": [{"case_ref": c.ref(c1), "override_params": {}}]})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]

        d = c.client.delete(f"/api/test-cases/{c1['id']}")
        assert d.status_code == 409
        assert "仍被计划引用" in _detail(d), "既有 Plan 错误文案不应改变"
        assert str(pid) in _detail(d)

        # 同时被 Plan 与 Suite 引用时，仍先报 Plan（不改变既有 Plan 行为）
        c.suite("套件", [c.ref(c1)])
        d = c.client.delete(f"/api/test-cases/{c1['id']}")
        assert d.status_code == 409
        assert "仍被计划引用" in _detail(d)
    finally:
        c.close()


# ============================================================ D. 校验 / I1


def test_d1_reject_execution_scope_fields():
    """I1 不变量：Suite 不接受任何执行作用域字段（显式报错，不静默丢弃）。"""
    c = Ctx()
    try:
        c1 = c.case("甲")
        for bad in ({"environment_id": 1}, {"executor_id": 1},
                    {"asset_source_id": 1}, {"fail_fast": True}):
            r = c.post_suite("S", [c.ref(c1)], **bad)
            assert r.status_code == 400, f"{bad} 应被拒绝，实得 {r.status_code}"
            assert "执行作用域字段" in _detail(r)
        # 正常创建不受影响
        assert c.post_suite("S", [c.ref(c1)]).status_code == 200
    finally:
        c.close()


def test_d2_reject_bad_cases():
    c = Ctx()
    try:
        c1 = c.case("甲")
        c2 = c.case("乙")

        # 不存在的用例
        r = c.post_suite("S", [{"id": 999, "revision": 1, "fingerprint": "x"}])
        assert r.status_code == 400
        assert "不存在" in _detail(r)

        # 停用的用例
        off = c.case("停用用例", enabled=False)
        r = c.post_suite("S", [c.ref(off)])
        assert r.status_code == 400
        assert "已停用" in _detail(r)

        # 重复引用同一用例
        r = c.post_suite("S", [c.ref(c1), c.ref(c1)])
        assert r.status_code == 400
        assert "重复引用" in _detail(r)

        # 缺 id / 非对象
        r = c.post_suite("S", [{"revision": 1}])
        assert r.status_code == 400
        r = c.post_suite("S", ["不是对象"])
        assert r.status_code == 400

        # 空 name / 同名
        assert c.post_suite("", [c.ref(c1)]).status_code == 400
        assert c.post_suite("S", [c.ref(c1)]).status_code == 200
        r = c.post_suite("S", [c.ref(c2)])
        assert r.status_code == 400
        assert "同名" in _detail(r)

        # tags 非字符串数组：pydantic 在模型层即以 422 拒绝。
        # ⚠️ 这与既有 TestCaseIn.tags: list[str] 的行为**完全一致**（属平台既有约定，
        #    非 Suite 新引入的不一致），故此处断言 422 而非 400。
        assert c.post_suite("S2", [c.ref(c1)], tags=[1, 2]).status_code == 422
        # 对照：TestCase 在同样的入参下也是 422 —— 证明两者行为一致
        assert c.client.post("/api/test-cases", json={
            "name": "tags 非法", "engine": "pytest", "params": {}, "tags": [1, 2]}
        ).status_code == 422

        # 空 cases 允许（MVP：集合可以为空，便于"先建后填"）
        r = c.post_suite("空套件", [])
        assert r.status_code == 200
        assert r.json()["cases"] == []
    finally:
        c.close()


def test_d3_cases_normalization_on_write():
    """缺省 revision 补 1；id 字符串数字纠正；缺 id / 非 dict 条目丢弃。"""
    c = Ctx()
    try:
        c1 = c.case("甲")
        r = c.post_suite("S", [
            {"id": c1["id"], "fingerprint": c1["fingerprint"]},          # 缺 revision → 1
            {"revision": 9},                                              # 缺 id → 丢弃
            "junk",                                                       # 丢弃
        ])
        assert r.status_code == 400, "缺 id 的条目会导致长度不一致 → 必须报错而非静默截断"
        r = c.post_suite("S", [{"id": str(c1["id"]), "fingerprint": c1["fingerprint"]}])
        assert r.status_code == 200, r.text
        assert r.json()["cases"] == [{"id": c1["id"], "revision": 1,
                                      "fingerprint": c1["fingerprint"]}]
    finally:
        c.close()


# ============================================================ E. 指纹性质


def test_e1_fingerprint_order_sensitive_and_meta_insensitive():
    c = Ctx()
    try:
        c1 = c.case("甲")
        c2 = c.case("乙")
        a, b = c.ref(c1), c.ref(c2)

        f_ab = cc.suite_fingerprint("S", [a, b])
        f_ba = cc.suite_fingerprint("S", [b, a])
        assert f_ab != f_ba, "顺序必须参与指纹（顺序=执行语义）"

        # 元素内键序不影响（sort_keys）
        assert cc.suite_fingerprint("S", [{"fingerprint": a["fingerprint"],
                                           "revision": a["revision"], "id": a["id"]}, b]) == f_ab

        # name 参与
        assert cc.suite_fingerprint("S2", [a, b]) != f_ab

        # 与 API 计算一致
        s = c.suite("S", [a, b])
        assert s["fingerprint"] == f_ab
    finally:
        c.close()


def test_e2_fingerprint_independent_per_suite():
    c = Ctx()
    try:
        c1 = c.case("甲")
        s1 = c.suite("同名不同", [c.ref(c1)])
        s2 = c.suite("其它", [c.ref(c1)])
        assert s1["fingerprint"] != s2["fingerprint"], "name 不同 → 指纹不同"
    finally:
        c.close()


# ============================================================ F. 冻结回归


def test_f1_legacy_inline_plan_unchanged():
    """回归：Suite 的引入**不得**影响 legacy inline 计划的 steps 形态。"""
    c = Ctx()
    try:
        env, ex = c.plan_env()
        c1 = c.case("甲")
        c.suite("套件", [c.ref(c1)])                      # Suite 存在也应无影响

        r = c.client.post("/api/plans", json={
            "name": "legacy", "description": "", "environment_id": env, "executor_id": ex,
            "asset_source_id": 0, "fail_fast": True, "owner": "local",
            "steps": [{"engine": "pytest", "name": "demo", "requires_exclusive": False,
                       "params": {"timeout_sec": 60}}]})
        assert r.status_code == 200, r.text
        plan = c.client.get(f"/api/plans/{r.json()['id']}").json()
        assert plan["steps"] == [{"engine": "pytest", "name": "demo",
                                  "requires_exclusive": False,
                                  "params": {"timeout_sec": 60}}], "legacy 四键形态不应变化"

        # case_ref 计划同样不受影响
        r = c.client.post("/api/plans", json={
            "name": "ref", "description": "", "environment_id": env, "executor_id": ex,
            "asset_source_id": 0, "fail_fast": True, "owner": "local",
            "steps": [{"case_ref": c.ref(c1), "override_params": {"timeout_sec": 30}}]})
        assert r.status_code == 200, r.text
        plan = c.client.get(f"/api/plans/{r.json()['id']}").json()
        assert plan["steps"] == [{"case_ref": c.ref(c1),
                                  "override_params": {"timeout_sec": 30}}]
    finally:
        c.close()


def test_f2_no_suite_execution_surface():
    """架构断言：Suite 不得出现任何执行入口 / Job / Report 关联（本批只做定义层）。"""
    c = Ctx()
    try:
        c1 = c.case("甲")
        s = c.suite("套件", [c.ref(c1)])
        # 不存在"执行套件"的入口
        assert c.client.post(f"/api/suites/{s['id']}/run").status_code == 404
        # 不产生任何 PlanRun / Job
        assert db.list_runs_meta(c.db_path, 50) == []
        assert c.client.get("/api/runs").json() == []
        # Suite 表不含执行作用域列
        import sqlite3
        cols = {r[1] for r in sqlite3.connect(c.db_path).execute(
            "PRAGMA table_info(test_suites)")}
        for bad in ("environment_id", "executor_id", "asset_source_id", "fail_fast",
                    "engine", "params", "timeout_sec"):
            assert bad not in cols, f"test_suites 不应有列 {bad}"
    finally:
        c.close()


def test_f3_normalizer_single_implementation():
    """cases 列的规范形态只有一份实现（db.normalize_suite_cases）。"""
    raw = json.dumps([{"id": "5", "revision": "2", "fingerprint": 7},
                      {"id": 6}, {"revision": 3}, "junk", None])
    assert db.normalize_suite_cases(raw) == [
        {"id": 5, "revision": 2, "fingerprint": "7"},
        {"id": 6, "revision": 1, "fingerprint": ""},
    ]
    # 传入已规范化的列表应为幂等
    once = db.normalize_suite_cases(raw)
    assert db.normalize_suite_cases(once) == once
