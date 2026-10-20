"""最小 API：计划列表、触发、PlanRun/Job 状态与日志、报告查询、产物访问、资源与环境配置中心。

配置中心约束：前端与 Plan 只提交 ID/key；API 一律返回脱敏视图；
不返回明文密钥/绝对路径；非法 ID/key 直接拒绝。
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import db, project_registry as registry
from ..config_center import (
    ASSET_KINDS,
    ENGINES,
    build_config_snapshot,
    build_content_fingerprint,
    build_plan_snapshot,
    plan_snapshot_name,
    precheck_plan,
    resolve_workdir,
    suite_fingerprint,
    test_case_fingerprint,
    validate_plan_refs,
    validate_step_params,
)
from ..domain import Job, Plan, PlanStep
from ..orchestration.scheduler import DATA_DIR
from ..ui_config import device_binding_errors, get_device, pytest_ui_errors
from .configuration import router as configuration_router

router = APIRouter(prefix="/api")
router.include_router(configuration_router)


def _app(request: Request):
    return request.app.state


# ---------- TestCase（v0.2 定义层） ----------
# 只描述"要跑什么"；不绑定 Environment / Executor / AssetSource（那三项属 Plan）。
# 参数校验复用 Plan 步骤的同一套白名单，绝不新造第二套规则。


class TestCaseIn(BaseModel):
    name: str
    description: str = ""
    engine: str
    params: dict = {}
    requires_exclusive: bool = False
    asset_kind: str = ""
    owner: str = "local"
    tags: list[str] = []
    enabled: bool = True


# 引擎 -> 允许的资产种类（'' = 不需要受控资产）
_ASSET_KIND_BY_ENGINE = {
    "pytest": "",
    "locust": "locust",
    "matcheval": "matcheval",
    "airtest": "airtest",
}


def _tc_payload(body: TestCaseIn) -> dict:
    return {
        "name": (body.name or "").strip(),
        "description": body.description or "",
        "engine": body.engine,
        "params": dict(body.params or {}),
        "requires_exclusive": body.engine == "airtest" or bool(body.requires_exclusive),
        "asset_kind": (body.asset_kind or ("airtest" if body.engine == "airtest" else "")).strip(),
        "owner": body.owner or "local",
        "tags": list(body.tags or []),
        "enabled": int(bool(body.enabled)),
    }


def _tc_errors(request: Request, body: TestCaseIn, cid: Optional[int] = None) -> list[str]:
    """TestCase 保存校验。与 Plan 步骤**共用** validate_step_params，不新造规则。"""
    st = _app(request)
    p = _tc_payload(body)
    errs: list[str] = []
    if not p["name"]:
        errs.append("name 必填")
    elif len(p["name"]) > 120:
        errs.append("name 过长（<=120 字符）")
    if p["engine"] not in ENGINES:
        errs.append(f"engine 必须是 {list(ENGINES)} 之一")
    else:
        expect = _ASSET_KIND_BY_ENGINE.get(p["engine"], "")
        if p["asset_kind"]:
            if p["asset_kind"] not in ASSET_KINDS:
                errs.append(f"asset_kind 必须是 '' 或 {list(ASSET_KINDS)} 之一")
            elif p["asset_kind"] != expect:
                errs.append(
                    f"asset_kind={p['asset_kind']!r} 与 engine={p['engine']!r} 不相容"
                    f"（该引擎应为 {expect!r}）"
                )
        # asset_source_id 属计划/执行级，不是用例级 —— 显式拒绝，避免与计划级兜底注入打架
        if "asset_source_id" in p["params"]:
            errs.append(
                "params 不允许包含 asset_source_id（属计划/执行级，请在 Plan 上指定 AssetSource）"
            )
        if p["engine"] in ENGINES:
            errs += validate_step_params(p["engine"], p["params"], st.db_path)
    if not isinstance(p["tags"], list) or any(not isinstance(t, str) for t in p["tags"]):
        errs.append("tags 必须是字符串数组")
    # 名称唯一：全库无 UNIQUE 约束，沿用应用层校验的既有风格
    dup = db.get_test_case_by_name(st.db_path, p["name"])
    if dup and (cid is None or int(dup["id"]) != int(cid)):
        errs.append(f"已存在同名 TestCase：{p['name']}（id={dup['id']}）")
    return errs


def _tc_fingerprint(p: dict) -> str:
    return test_case_fingerprint(
        p["name"], p["engine"], p["params"], p["requires_exclusive"], p["asset_kind"]
    )


@router.get("/test-cases")
def list_test_cases(request: Request, engine: str = "", enabled: int = -1):
    st = _app(request)
    return db.list_test_cases(st.db_path, engine=engine, enabled_only=(enabled == 1))


@router.get("/test-cases/{cid}")
def get_test_case(cid: int, request: Request):
    tc = db.get_test_case(_app(request).db_path, cid)
    if not tc:
        raise HTTPException(404, "test case not found")
    return tc


@router.post("/test-cases")
@registry.locked
def create_test_case(body: TestCaseIn, request: Request):
    st = _app(request)
    errs = _tc_errors(request, body)
    if errs:
        raise HTTPException(400, "；".join(errs))
    p = _tc_payload(body)
    p["fingerprint"] = _tc_fingerprint(p)
    cid = db.create_test_case(st.db_path, p)
    return db.get_test_case(st.db_path, cid)


@router.put("/test-cases/{cid}")
@registry.locked
def update_test_case(cid: int, body: TestCaseIn, request: Request):
    """编辑 TestCase。

    v0.2 冻结规则：**只有定义字段（name / engine / params / requires_exclusive / asset_kind）
    变化才 revision+1 并重算 fingerprint**；description / tags / enabled / owner 的变化
    不改变"测试定义内容"，因此 **不 bump revision**。
    判定方式就是比较新旧定义指纹，而不是逐字段手写判断。
    """
    st = _app(request)
    old = db.get_test_case(st.db_path, cid)
    if not old:
        raise HTTPException(404, "test case not found")
    errs = _tc_errors(request, body, cid=cid)
    if errs:
        raise HTTPException(400, "；".join(errs))
    p = _tc_payload(body)
    new_fp = _tc_fingerprint(p)
    defining_changed = new_fp != (old.get("fingerprint") or "")
    p["fingerprint"] = new_fp
    db.update_test_case(st.db_path, cid, p, bump_revision=defining_changed)
    return db.get_test_case(st.db_path, cid)


@router.post("/test-cases/{cid}/toggle")
def toggle_test_case(cid: int, request: Request):
    """启停：可用性开关，不属"定义"，因此不递增 revision。"""
    st = _app(request)
    tc = db.get_test_case(st.db_path, cid)
    if not tc:
        raise HTTPException(404, "test case not found")
    db.set_test_case_enabled(st.db_path, cid, 0 if int(tc.get("enabled") or 0) else 1)
    return db.get_test_case(st.db_path, cid)


def _plans_referencing_case(st, cid: int) -> list[int]:
    out: list[int] = []
    for p in db.list_plans(st.db_path):
        for s in p.get("steps") or []:
            cr = s.get("case_ref") or {}
            if cr and int(cr.get("id") or 0) == int(cid):
                out.append(p["id"])
                break
    return out


@router.delete("/test-cases/{cid}")
def delete_test_case(cid: int, request: Request):
    """删除 TestCase。被任一 Plan 或 TestSuite 引用时拒绝（409）——
    它们都按 (id, revision, fingerprint) 固定引用，删除会让引用方无法展开。

    ⚠️ Plan 引用检查与其错误文案**保持 v0.2 一阶段的原文**（不改既有 Plan 行为）；
    下面新增的 Suite 检查是**补充**，只在 Plan 未引用时才会走到。"""
    st = _app(request)
    if not db.get_test_case(st.db_path, cid):
        raise HTTPException(404, "test case not found")
    refs = _plans_referencing_case(st, cid)
    if refs:
        raise HTTPException(
            409, f"该 TestCase 仍被计划引用：{refs}；请先解除引用，或改用停用（toggle）"
        )
    # v0.2 TestSuite：补齐引用保护的另一半。
    # 此前只扫 plans，Suite 引用用例后删掉该用例会留下指向不存在用例的悬空 case_ref
    # → 那个 Suite 将永久无法展开，且用户看不到原因。
    srefs = db.suites_referencing_case(st.db_path, cid)
    if srefs:
        raise HTTPException(
            409, f"该 TestCase 仍被测试套件引用：{srefs}；请先从套件中移除，或改用停用（toggle）"
        )
    db.delete_test_case(st.db_path, cid)
    return {"deleted": cid}


# ---------- TestCase 展开（执行前解析，唯一入口） ----------
def _expand_case_steps(request: Request, steps: list) -> tuple[list, list[str]]:
    """把 steps 里的 case_ref 展开为完整步骤；返回 (展开后的步骤, 错误列表)。

    v0.2 冻结规则：
      · 无 case_ref → legacy 内联步骤，**原样返回、不加任何键**（保证旧计划 JSON 逐字节不变）
      · 有 case_ref → 校验 id / revision / fingerprint / enabled / engine，
        **任一不符即报错，绝不静默修正**；随后合并 override_params 并
        对 **merged 整体**重跑 validate_step_params（只校验 override 不足以防绕过）。

    展开后的步骤：engine / name / requires_exclusive / params 为**本次执行最终有效值**，
    另附 case_ref 与 override_params 供溯源 —— 这样 _resolve_step_params、
    engine_compare_key、报告、预检都无需因 TestCase 而改造。

    错误非空时调用方必须中止：不得创建 PlanRun、不得创建 Job。
    """
    st = _app(request)
    out: list = []
    errs: list[str] = []
    for i, s in enumerate(steps or []):
        cr = s.get("case_ref")
        if not cr:
            out.append(dict(s))  # legacy：原样透传
            continue
        tag = f"steps[{i}].case_ref"
        if not isinstance(cr, dict):
            errs.append(f"{tag} 必须是对象 {{id, revision, fingerprint}}")
            continue
        try:
            cid = int(cr.get("id"))
        except (TypeError, ValueError):
            errs.append(f"{tag}.id 必须是整数")
            continue
        tc = db.get_test_case(st.db_path, cid)
        if not tc:
            errs.append(f"{tag} 引用的 TestCase 不存在：id={cid}")
            continue
        if not int(tc.get("enabled") or 0):
            errs.append(f"{tag} 引用的 TestCase 已停用：{tc.get('name')}（id={cid}）")
            continue
        rev = cr.get("revision")
        try:
            rev_ok = rev is not None and int(rev) == int(tc.get("revision") or 1)
        except (TypeError, ValueError):
            rev_ok = False
        if not rev_ok:
            errs.append(
                f"{tag}.revision 与用例当前版本不一致：引用={rev} 当前={tc.get('revision')}"
                f"；用例已被修改，请重新保存计划以更新引用"
            )
            continue
        if not cr.get("fingerprint") or str(cr.get("fingerprint")) != str(
            tc.get("fingerprint") or ""
        ):
            errs.append(
                f"{tag}.fingerprint 与用例不一致：引用={cr.get('fingerprint')} "
                f"当前={tc.get('fingerprint')}；用例定义已被修改，请重新保存计划"
            )
            continue
        engine = tc.get("engine")
        if s.get("engine") and s.get("engine") != engine:
            errs.append(
                f"{tag} 所在步骤 engine={s.get('engine')!r} 与用例 engine={engine!r} 不一致"
            )
            continue
        override = s.get("override_params") or {}
        if not isinstance(override, dict):
            errs.append(f"steps[{i}].override_params 必须是对象")
            continue
        banned = [
            k
            for k in ("engine", "asset_source_id", "environment_id", "executor_id")
            if k in override
        ]
        if banned:
            errs.append(f"steps[{i}].override_params 不允许覆盖：{banned}")
            continue
        merged = {**dict(tc.get("params") or {}), **override}
        perr = validate_step_params(engine, merged, st.db_path)
        if perr:
            errs += [f"steps[{i}] {e}" for e in perr]
            continue
        out.append(
            {
                "engine": engine,
                "name": s.get("name") or tc.get("name"),
                "requires_exclusive": engine == "airtest"
                or bool(s.get("requires_exclusive", tc.get("requires_exclusive"))),
                "params": merged,
                "case_ref": {
                    "id": cid,
                    "revision": int(tc.get("revision") or 1),
                    "fingerprint": str(tc.get("fingerprint") or ""),
                },
                "override_params": dict(override),
            }
        )
    return out, errs


# ---------- TestSuite（v0.2 定义层：有序用例集合） ----------
# Suite 只描述"引用哪些用例、按什么顺序"。它 **不** 描述"在哪儿跑/用谁跑/用哪份数据跑"（属 Plan）、
# **不** 参与执行链（PlanRun → Job → Report）、也 **不** 被三个 Runner 感知。
# 本批（Step 2）只提供基础管理能力：CRUD + 引用保护。
# 展开（编辑期）与 provenance（suite_ref）留待后续步骤，**此处不含任何执行入口**。
#
# ⚠️ I1 不变量（架构审计 §2.2）：Suite 不含任何执行作用域字段。
#    下面四个字段**仅为显式拒绝而声明** —— 若不声明，pydantic 会静默丢弃客户端发来的这些键，
#    客户端会误以为生效；这与本项目"绝不静默"的原则冲突，故显式捕获并报 400。


class SuiteIn(BaseModel):
    name: str
    description: str = ""
    # ⚠️ 刻意声明为无类型 list（而非 list[dict]）：若用 list[dict]，pydantic 会以
    #    **422 + 英文错误结构** 抢先拒绝非法元素，与全平台「400 + 中文「；」拼接」的
    #    错误风格不一致。这里放行到业务层，由 _suite_errors 给出可读的中文错误。
    cases: list = []
    owner: str = "local"
    tags: list[str] = []
    enabled: bool = True
    # --- 以下仅用于拒绝：它们属于 Plan，不属于 Suite ---
    environment_id: Optional[int] = None
    executor_id: Optional[int] = None
    asset_source_id: Optional[int] = None
    fail_fast: Optional[bool] = None


_SUITE_FORBIDDEN_KEYS = ("environment_id", "executor_id", "asset_source_id", "fail_fast")


def _suite_payload(body: SuiteIn) -> dict:
    """组 payload。cases 走**存储层唯一规范化实现**（db.normalize_suite_cases），
    保证写入、指纹、读回三者看到的形状完全一致。"""
    return {
        "name": (body.name or "").strip(),
        "description": body.description or "",
        "cases": db.normalize_suite_cases(body.cases),
        "owner": body.owner or "local",
        "tags": list(body.tags or []),
        "enabled": int(bool(body.enabled)),
    }


def _suite_errors(request: Request, body: SuiteIn, sid: Optional[int] = None) -> list[str]:
    """Suite 保存校验。风格与 _tc_errors / _plan_errors 一致：错误用「；」拼接后 400。

    cases 的存在性/启用态检查与 _expand_case_steps 同一套语义与措辞，
    这样"保存套件"与"展开套件"给出的错误是同一句话，用户不用学两套。"""
    st = _app(request)
    p = _suite_payload(body)
    errs: list[str] = []

    # I1：执行作用域字段一律拒绝（显式报错，不静默丢弃）
    banned = [k for k in _SUITE_FORBIDDEN_KEYS if getattr(body, k, None) is not None]
    if banned:
        errs.append(
            f"Suite 不接受执行作用域字段：{banned}"
            f"（环境 / 执行机 / 资产来源 / fail_fast 属 Plan）"
        )

    if not p["name"]:
        errs.append("name 必填")
    elif len(p["name"]) > 120:
        errs.append("name 过长（<=120 字符）")
    if not isinstance(p["tags"], list) or any(not isinstance(t, str) for t in p["tags"]):
        errs.append("tags 必须是字符串数组")

    raw = body.cases if isinstance(body.cases, list) else None
    if raw is None:
        errs.append("cases 必须是数组")
    else:
        if len(p["cases"]) != len(raw):
            errs.append(
                "cases 的元素必须是形如 {id, revision, fingerprint} 的对象"
                "（缺 id 或非对象的条目会被拒绝）"
            )
        seen: set = set()
        for i, c in enumerate(p["cases"]):
            cid = int(c.get("id"))
            if cid in seen:
                errs.append(
                    f"cases[{i}] 重复引用同一用例：id={cid}"
                    f"（如需重复执行同一用例，请在计划中重复添加步骤）"
                )
                continue
            seen.add(cid)
            tc = db.get_test_case(st.db_path, cid)
            if not tc:
                errs.append(f"cases[{i}] 引用的 TestCase 不存在：id={cid}")
                continue
            if not int(tc.get("enabled") or 0):
                errs.append(f"cases[{i}] 引用的 TestCase 已停用：{tc.get('name')}（id={cid}）")

    # 名称唯一：全库无 UNIQUE 约束，沿用应用层校验的既有风格
    dup = db.get_suite_by_name(st.db_path, p["name"])
    if dup and (sid is None or int(dup["id"]) != int(sid)):
        errs.append(f"已存在同名 TestSuite：{p['name']}（id={dup['id']}）")
    return errs


def _suite_fingerprint(p: dict) -> str:
    return suite_fingerprint(p["name"], p["cases"])


def _plans_referencing_suite(st, sid: int) -> list[int]:
    """只读：返回 steps 中带 suite_ref 且指向该 Suite 的 Plan id 列表。

    `suite_ref` 是 **provenance-only 溯源键**（只用于回答"该 Plan 来源于哪个 Suite"，
    执行期永不读取，也不参与步骤展开）。它在后续步骤（编辑期展开）引入，
    当前恒为空 —— 但检查位先就位，一旦开始写 provenance，删除保护自动生效。"""
    out: list[int] = []
    for p in db.list_plans(st.db_path):
        for s in p.get("steps") or []:
            sr = s.get("suite_ref") or {}
            if sr and int(sr.get("id") or 0) == int(sid):
                out.append(p["id"])
                break
    return out


@router.get("/suites")
def list_suites(request: Request, enabled: int = -1):
    """Suite 列表。enabled=1 时只返回启用的。"""
    st = _app(request)
    return db.list_suites(st.db_path, enabled_only=(enabled == 1))


@router.get("/suites/{sid}")
def get_suite(sid: int, request: Request):
    s = db.get_suite(_app(request).db_path, sid)
    if not s:
        raise HTTPException(404, "test suite not found")
    return s


@router.post("/suites")
def create_suite(body: SuiteIn, request: Request):
    """新建 Suite：cases 规范化 + 自动计算定义指纹 + revision 初始为 1。"""
    st = _app(request)
    errs = _suite_errors(request, body)
    if errs:
        raise HTTPException(400, "；".join(errs))
    p = _suite_payload(body)
    p["fingerprint"] = _suite_fingerprint(p)
    sid = db.create_suite(st.db_path, p)
    return db.get_suite(st.db_path, sid)


@router.put("/suites/{sid}")
def update_suite(sid: int, body: SuiteIn, request: Request):
    """编辑 Suite（**整体替换 cases**，不做增量 add / remove / move）。

    v0.2 冻结规则：**只有定义字段（name / cases）变化才 revision+1 并重算 fingerprint**；
    description / tags / owner / enabled 的变化不改变"集合内容"，因此 **不 bump revision**。
    判定方式是比较新旧**定义指纹**，而不是逐字段手写判断 —— 与 TestCase 完全一致。
    """
    st = _app(request)
    old = db.get_suite(st.db_path, sid)
    if not old:
        raise HTTPException(404, "test suite not found")
    errs = _suite_errors(request, body, sid=sid)
    if errs:
        raise HTTPException(400, "；".join(errs))
    p = _suite_payload(body)
    new_fp = _suite_fingerprint(p)
    defining_changed = new_fp != (old.get("fingerprint") or "")
    p["fingerprint"] = new_fp
    db.update_suite(st.db_path, sid, p, bump_revision=defining_changed)
    return db.get_suite(st.db_path, sid)


@router.post("/suites/{sid}/toggle")
def toggle_suite(sid: int, request: Request):
    """启停：可用性开关，不属"定义"，因此不递增 revision（与 TestCase 一致）。"""
    st = _app(request)
    s = db.get_suite(st.db_path, sid)
    if not s:
        raise HTTPException(404, "test suite not found")
    db.set_suite_enabled(st.db_path, sid, 0 if int(s.get("enabled") or 0) else 1)
    return db.get_suite(st.db_path, sid)


@router.delete("/suites/{sid}")
def delete_suite(sid: int, request: Request):
    """删除 Suite：**删除前做基础引用检查**。

    当前 Suite 不进入执行链，展开发生在编辑期且产物是普通 case_ref 步骤，
    因此唯一的引用来源是 provenance 键 `suite_ref`（见 _plans_referencing_suite）。
    检查位先就位；不做复杂关联系统。"""
    st = _app(request)
    if not db.get_suite(st.db_path, sid):
        raise HTTPException(404, "test suite not found")
    refs = _plans_referencing_suite(st, sid)
    if refs:
        raise HTTPException(409, f"该 TestSuite 仍被计划引用（provenance）：{refs}；请先解除引用")
    db.delete_suite(st.db_path, sid)
    return {"deleted": sid}


# ---------- 计划 ----------
@router.get("/plans")
def list_plans(request: Request, status: Literal["active", "archived", "all"] = "active"):
    return db.list_plans(
        _app(request).db_path, {"active": False, "archived": True, "all": None}[status]
    )


@router.post("/plans/{pid}/archive")
def archive_plan(pid: int, request: Request):
    if not db.set_plan_archived(_app(request).db_path, pid, True):
        raise HTTPException(404, "plan not found")
    return db.get_plan(_app(request).db_path, pid)


@router.post("/plans/{pid}/restore")
def restore_plan(pid: int, request: Request):
    if not db.set_plan_archived(_app(request).db_path, pid, False):
        raise HTTPException(404, "plan not found")
    return db.get_plan(_app(request).db_path, pid)


@router.get("/plans/{pid}")
def get_plan(pid: int, request: Request):
    plan = db.get_plan(_app(request).db_path, pid)
    if not plan:
        raise HTTPException(404, "plan not found")
    return plan


class PlanIn(BaseModel):
    name: str
    description: str = ""
    environment_id: int
    executor_id: int
    asset_source_id: int = 0
    steps: list[dict] = []
    fail_fast: bool = True
    owner: str = "local"


def _plan_errors(request: Request, body: PlanIn) -> list[str]:
    st = _app(request)
    errs = validate_plan_refs(
        db, st.db_path, body.environment_id, body.executor_id, body.asset_source_id or None
    )
    if not body.steps:
        errs.append("plan 至少需要一个执行单元")
    # v0.2：先展开 TestCase 引用（含 id/revision/fingerprint/enabled/engine 一致性检查与
    # override 合并后的整体参数校验），再对"本次执行的有效步骤"做引擎与白名单校验。
    expanded, case_errs = _expand_case_steps(request, body.steps)
    if case_errs:
        return errs + case_errs
    for i, s in enumerate(expanded):
        eng = s.get("engine")
        if eng not in ENGINES:
            errs.append(f"steps[{i}].engine 非法：{eng!r}")
            continue
        errs += [
            f"steps[{i}] {e}" for e in validate_step_params(eng, s.get("params") or {}, st.db_path)
        ]
        if eng == "airtest":
            errs += device_binding_errors(
                get_device((s.get("params") or {}).get("device"), st.db_path),
                body.executor_id,
                st.db_path,
            )
        if eng == "airtest" and body.asset_source_id:
            asset = db.get_asset_source(st.db_path, body.asset_source_id) or {}
            if asset.get("kind") != "airtest" or asset.get("scenario_ref") != (
                s.get("params") or {}
            ).get("suite"):
                errs.append("UI 步骤的套件与计划 AssetSource 不一致")
        if eng == "pytest":
            ex = db.get_executor(st.db_path, body.executor_id) or {}
            p = s.get("params") or {}
            root = p.get("cwd_root") or p.get("cwd") or ex.get("cwd_root") or ex.get("cwd", "")
            errs += pytest_ui_errors(p, resolve_workdir(root) or root)
    return errs


def _plan_from_body(body: PlanIn) -> Plan:
    steps = []
    for s in body.steps or []:
        cr = s.get("case_ref") or None
        if cr:
            # 引用形态：name / requires_exclusive 是**可选覆盖**，缺省表示"继承用例"，
            # 因此这里绝不替客户端编造默认值（否则会把用例的取值静默盖掉）。
            steps.append(
                PlanStep(
                    engine=s.get("engine") or "",
                    name=s.get("name") or "",
                    requires_exclusive=s.get("engine") == "airtest"
                    or bool(s.get("requires_exclusive", False)),
                    params=s.get("params") or {},
                    case_ref=dict(cr),
                    override_params=dict(s.get("override_params") or {}),
                    name_set=bool(s.get("name")),
                    exclusive_set=("requires_exclusive" in s),
                )
            )
        else:
            steps.append(
                PlanStep(
                    engine=s.get("engine") or "pytest",
                    name=s.get("name") or s.get("engine") or "job",
                    requires_exclusive=s.get("engine") == "airtest"
                    or bool(s.get("requires_exclusive", False)),
                    params=s.get("params") or {},
                )
            )
    return Plan(
        name=body.name,
        description=body.description,
        environment_id=body.environment_id,
        executor_id=body.executor_id,
        asset_source_id=body.asset_source_id,
        fail_fast=bool(body.fail_fast),
        owner=body.owner or "local",
        steps=steps,
    )


@router.post("/plans")
@registry.locked
def create_plan(body: PlanIn, request: Request):
    """创建计划：只接受受控配置 ID 与白名单步骤参数；仅启用的配置可被引用。"""
    st = _app(request)
    errs = _plan_errors(request, body)
    if errs:
        raise HTTPException(400, "；".join(errs))
    pid = db.create_plan(st.db_path, _plan_from_body(body))
    return {"id": pid}


@router.put("/plans/{pid}")
@registry.locked
def update_plan(pid: int, body: PlanIn, request: Request):
    st = _app(request)
    current = db.get_plan(st.db_path, pid)
    if not current:
        raise HTTPException(404, "plan not found")
    if current.get("archived"):
        raise db.PlanLifecycleConflict("该测试计划已归档，请先恢复后再编辑")
    errs = _plan_errors(request, body)
    if errs:
        raise HTTPException(400, "；".join(errs))
    p = _plan_from_body(body)
    db.update_plan(
        st.db_path,
        pid,
        {
            "name": p.name,
            "description": p.description,
            "environment_id": p.environment_id,
            "executor_id": p.executor_id,
            "asset_source_id": p.asset_source_id or None,
            "fail_fast": int(p.fail_fast),
            "owner": p.owner,
            # 走 PlanStep.to_dict()：保证 case_ref / override_params 不被丢弃（透传点 3/9）
            "steps": [s.to_dict() for s in p.steps],
        },
    )
    return db.get_plan(st.db_path, pid)


class PlanCopyIn(BaseModel):
    name: str = ""


@router.post("/plans/precheck")
def precheck_plan_draft(body: PlanIn, request: Request, realtime: bool = True):
    """草稿计划预检/执行预览：不落库、不创建 PlanRun/Job、不跑测试。

    v0.2：先把 case_ref 展开为有效步骤，再交给 precheck —— 保证预览结果与真实执行一致。"""
    st = _app(request)
    return precheck_plan(db, st.db_path, _precheck_ready_dict(request, body), realtime=realtime)


@router.post("/plans/{pid}/precheck")
def precheck_plan_saved(pid: int, request: Request, realtime: bool = True):
    """已保存计划预检：只读探测业务依赖，不创建运行，不执行用例。

    v0.2：先把 case_ref 展开为有效步骤（含一致性检查），再交给 precheck。"""
    st = _app(request)
    plan = db.get_plan(st.db_path, pid)
    if not plan:
        raise HTTPException(404, "plan not found")
    expanded, errs = _expand_case_steps(request, plan.get("steps") or [])
    if errs:
        raise HTTPException(400, "；".join(errs))
    return precheck_plan(db, st.db_path, {**plan, "steps": expanded}, realtime=realtime)


@router.post("/plans/{pid}/copy")
@registry.locked
def copy_plan(pid: int, request: Request, body: Optional[PlanCopyIn] = None):
    """复制计划：以受控字段创建新计划（revision 重置为 1）。复制仍需通过引用/步骤校验。"""
    st = _app(request)
    src = db.get_plan(st.db_path, pid)
    if not src:
        raise HTTPException(404, "plan not found")
    name = (
        body.name.strip() if body and body.name and body.name.strip() else f"{src['name']} (副本)"
    )
    plan_in = PlanIn(
        name=name,
        description=src.get("description") or "",
        environment_id=src["environment_id"],
        executor_id=src["executor_id"],
        asset_source_id=src.get("asset_source_id") or 0,
        # 原样透传已存的 steps（含 case_ref / override_params）——
        # v0.1 这里重建了 4 个键，会把用例引用丢掉（透传点 5/9）
        steps=[dict(s) for s in (src.get("steps") or [])],
        fail_fast=bool(src.get("fail_fast", 1)),
        owner=src.get("owner") or "local",
    )
    errs = _plan_errors(request, plan_in)
    if errs:
        raise HTTPException(400, "；".join(errs))
    new_id = db.create_plan(st.db_path, _plan_from_body(plan_in))
    return {"id": new_id, "copied_from": pid}


def _plan_dict_from_body(body: PlanIn) -> dict:
    """把请求体规范为与 db.get_plan 同构的 dict（供预检复用，不落库）。
    注意：返回的是**存储形态** steps；预检前调用方必须用展开后的步骤覆盖它。"""
    p = _plan_from_body(body)
    return {
        "id": None,
        "name": p.name,
        "description": p.description,
        "environment_id": p.environment_id,
        "executor_id": p.executor_id,
        "asset_source_id": p.asset_source_id,
        "fail_fast": int(p.fail_fast),
        "owner": p.owner,
        "revision": 0,
        "steps": [s.to_dict() for s in p.steps],
    }


def _precheck_ready_dict(request: Request, body: PlanIn) -> dict:
    """预检用：把草稿展开为"本次执行的有效步骤"，失败则 400（与保存同一套校验）。"""
    d = _plan_dict_from_body(body)
    expanded, errs = _expand_case_steps(request, body.steps)
    if errs:
        raise HTTPException(400, "；".join(errs))
    d["steps"] = expanded
    return d


class RunOut(BaseModel):
    run_id: int


@router.post("/plans/{pid}/run")
def run_plan(pid: int, request: Request) -> RunOut:
    with registry.bindings_lock:
        return _run_plan_bound(pid, request)


def _run_plan_bound(pid: int, request: Request) -> RunOut:
    st = _app(request)
    plan = db.get_plan(st.db_path, pid)
    if not plan:
        raise HTTPException(404, "plan not found")
    if plan.get("archived"):
        raise db.PlanLifecycleConflict("该测试计划已归档，请先恢复后再执行")
    if not plan["steps"]:
        raise HTTPException(400, "plan 没有执行单元")
    # 仅启用的 Environment / Executor / AssetSource 可被引用（只认 ID）
    ref_errors = validate_plan_refs(
        db, st.db_path, plan["environment_id"], plan["executor_id"], plan.get("asset_source_id")
    )
    if ref_errors:
        raise HTTPException(400, "；".join(ref_errors))
    env = db.get_environment(st.db_path, plan["environment_id"])
    exec_ = db.get_executor(st.db_path, plan["executor_id"])
    asset = (
        db.get_asset_source(st.db_path, plan["asset_source_id"])
        if plan.get("asset_source_id")
        else None
    )

    # ---- v0.2：在生成快照之前展开 TestCase 引用 ----
    # 展开失败必须**不产生任何 PlanRun / Job**（与上面的引用校验同等保证）。
    steps, case_errs = _expand_case_steps(request, plan.get("steps") or [])
    if case_errs:
        raise HTTPException(400, "；".join(case_errs))

    # Revalidate legacy inline steps and executor default cwd before creating facts.
    for step in steps:
        eng, p = step.get("engine"), step.get("params") or {}
        if eng == "pytest":
            try:
                p = registry.pytest_params(p)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            step["params"] = p
        errors = validate_step_params(eng, p, st.db_path)
        if eng == "airtest":
            errors += device_binding_errors(
                get_device(p.get("device"), st.db_path), plan["executor_id"], st.db_path
            )
        if eng == "airtest" and asset:
            if asset.get("kind") != "airtest" or asset.get("scenario_ref") != p.get("suite"):
                errors.append("UI 步骤的套件与计划 AssetSource 不一致")
        if eng == "pytest":
            root = (
                p.get("cwd_root") or p.get("cwd") or exec_.get("cwd_root") or exec_.get("cwd", "")
            )
            errors += pytest_ui_errors(p, resolve_workdir(root) or root)
        if errors:
            raise HTTPException(400, "；".join(errors))
        if eng == "airtest":
            step["requires_exclusive"] = True

    # Job 创建时固化脱敏配置快照：后续编辑配置不改变历史 Job / report.json / 比较结论
    base_snapshot = build_config_snapshot(env, exec_, asset)
    # PlanRun 固化"执行时的计划版本 + 不可变计划快照"：后续编辑 Plan 不改变本次执行。
    # 快照存**展开后的有效步骤**（另附 case_ref/override_params 供溯源）→ 自包含、不可变。
    plan_snapshot = build_plan_snapshot({**plan, "steps": steps})

    run_id = db.create_run(
        st.db_path, pid, plan_revision=plan.get("revision") or 1, plan_snapshot=plan_snapshot
    )
    for step in steps:
        engine = step.get("engine", "pytest")
        params = _resolve_step_params(step, engine, base_snapshot, asset)
        # v0.2 内容指纹：本次执行实际面对的受控脚本/素材内容。
        # 只在 PlanRun 创建阶段计算（按受控 key + 廉价签名做进程内缓存，不重复读盘），
        # 与 TestCase 的"定义指纹"严格分离、互不代替。
        snapshot = dict(base_snapshot)
        registrations = registry.snapshot(params, exec_)
        if registrations:
            snapshot["registration"] = registrations
        if engine == "airtest":
            snapshot["ui_device"] = dict(get_device(params.get("device"), st.db_path) or {})
            from ..runtime_requirements import resolve_requirement

            snapshot["runtime_requirements"] = resolve_requirement(params, st.db_path)
        snapshot["content"] = build_content_fingerprint(
            engine, params, st.db_path, snapshot.get("ui_device")
        )
        job = Job(
            plan_run_id=run_id,
            plan_id=pid,
            engine=engine,
            name=step.get("name") or engine,
            environment_id=plan["environment_id"],
            executor_id=plan["executor_id"],
            timeout_sec=int(params.get("timeout_sec", 300)),
            requires_exclusive=engine == "airtest" or bool(step.get("requires_exclusive", 0)),
            params=params,
            status="queued",
            config_snapshot=snapshot,
        )
        db.insert_job(st.db_path, job)
    return RunOut(run_id=run_id)


def _resolve_step_params(step: dict, engine: str, snapshot: dict, asset) -> dict:
    """把步骤参数规范为运行参数：受控 cwd_root -> 绝对 cwd（仅后端）；按需注入受控资产 key。"""
    params = dict(step.get("params") or {})
    if engine == "pytest":
        params = registry.pytest_params(params)
        if params.get("cwd") and not params.get("cwd_root"):
            from ..config_center import WORKDIR_ROOTS, infer_workdir_root

            root = (
                params["cwd"]
                if params["cwd"] in WORKDIR_ROOTS
                else infer_workdir_root(params["cwd"])
            )
            if root:
                params["cwd_root"] = root
                params.pop("cwd")
    # Keep stable references for both registered and legacy roots. Resolve only
    # under the scheduler's binding lock, so pending jobs cannot switch paths.
    asset_id = params.pop("asset_source_id", None)
    key = ""
    if asset_id and asset and int(asset_id) == int(asset.get("id") or -1):
        key = _asset_key_for(asset, engine)
    elif asset:
        key = _asset_key_for(asset, engine)
    if key:
        if engine == "matcheval" and not params.get("dataset"):
            params["dataset"] = key
        if engine == "locust" and not params.get("locustfile"):
            params["locustfile"] = key
    if engine == "matcheval":
        params.setdefault("dataset", "fixture-small")
    elif engine == "locust":
        params.setdefault("locustfile", "http-fixture")
    return params


def _asset_key_for(asset: dict, engine: str) -> str:
    if engine == "matcheval" and asset.get("kind") == "matcheval":
        return asset.get("dataset_ref") or ""
    if engine == "locust" and asset.get("kind") == "locust":
        return asset.get("scenario_ref") or ""
    return ""


# ---------- PlanRun / Job ----------
@router.get("/runs")
def list_runs(request: Request, limit: int = 10, offset: int = 0):
    """运行记录列表（倒序）。limit/offset 供前端「加载更多」浏览更早的历史运行。"""
    st = _app(request)
    rows = []
    for d in db.list_runs_meta(st.db_path, limit, offset):
        # 计划名优先取执行时的 PlanRun 快照：后续改名不影响历史执行记录展示
        snap_name = plan_snapshot_name(d.get("plan_snapshot"))
        d["plan_name"] = snap_name or (
            (db.get_plan(st.db_path, d["plan_id"]) or {}).get("name") or f"plan#{d['plan_id']}"
        )
        d.pop("plan_snapshot", None)
        rows.append(d)
    return rows


@router.get("/runs/{run_id}")
def get_run(run_id: int, request: Request):
    st = _app(request)
    run = db.get_run(st.db_path, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    jobs = db.list_jobs_by_run(st.db_path, run_id)
    plan = db.get_plan(st.db_path, run["plan_id"])
    return {"run": run, "jobs": jobs, "plan_archived": bool((plan or {}).get("archived"))}


@router.get("/jobs/{job_id}")
def get_job(job_id: int, request: Request):
    return db.get_job(_app(request).db_path, job_id)


@router.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: int, request: Request):
    st = _app(request)
    lines = st.orchestrator.get_logs(job_id)
    return {"job_id": job_id, "logs": lines}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, request: Request):
    st = _app(request)
    st.orchestrator.request_cancel(job_id)
    return {"ok": True, "job_id": job_id}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, request: Request):
    st = _app(request)
    job = db.get_job(st.db_path, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    try:
        new_id = st.orchestrator.retry(job)
    except db.PlanLifecycleConflict:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "new_job_id": new_id}


# ---------- 报告 ----------
@router.get("/reports/{job_id}")
def get_report(job_id: int, request: Request):
    st = _app(request)
    report = db.get_report(st.db_path, job_id)
    if not report:
        raise HTTPException(404, "report not found")
    return report


# ---------- 报告中心（读模型查询 + 筛选分页 + 对比 + 趋势） ----------
@router.get("/report-center")
def report_center(
    request: Request,
    plan_id: int = 0,
    engine: str = "",
    status: str = "",
    env_name: str = "",
    time_from: str = "",
    time_to: str = "",
    page: int = 1,
    page_size: int = 20,
):
    st = _app(request)
    return db.list_report_index(
        st.db_path,
        {
            "plan_id": plan_id or None,
            "engine": engine or None,
            "status": status or None,
            "env_name": env_name or None,
            "time_from": time_from or None,
            "time_to": time_to or None,
        },
        page,
        page_size,
    )


@router.get("/report-center/options")
def report_center_options(request: Request):
    return db.report_index_options(_app(request).db_path)


@router.get("/report-center/{job_id}")
def report_center_item(job_id: int, request: Request):
    st = _app(request)
    idx = db.get_report_index(st.db_path, job_id)
    if not idx:
        raise HTTPException(404, "report index not found")
    return idx


class CompareReq(BaseModel):
    baseline_job_id: int
    candidate_job_id: int


@router.post("/report-center/compare")
def report_center_compare(req: CompareReq, request: Request):
    """严格对比：仅同 engine/schema主版本/场景/环境/关键输入 才有效对比。
    返回兼容性结论 + 引擎专项指标差值 + 历史趋势。"""
    from ..report_center import build_trend, compare_compatibility, metric_deltas

    st = _app(request)
    a = db.get_report_index(st.db_path, req.baseline_job_id)
    b = db.get_report_index(st.db_path, req.candidate_job_id)
    if not a or not b:
        raise HTTPException(404, "baseline 或 candidate 报告索引不存在")
    compat = compare_compatibility(a, b)
    deltas = (
        metric_deltas(a, b)
        if compat["compatible"]
        else {"engine": b.get("engine"), "items": [], "skipped": True}
    )
    history = db.report_index_history(st.db_path, a.get("comparator") or "", limit=30)
    trend = build_trend(history)
    return {
        "compatible": compat["compatible"],
        "reasons": compat["reasons"],
        "summary": compat["summary"],
        "baseline": _summarize_idx(a),
        "candidate": _summarize_idx(b),
        "deltas": deltas,
        "trend": trend,
    }


def _summarize_idx(idx: dict) -> dict:
    return {
        "job_id": idx.get("job_id"),
        "plan_name": idx.get("plan_name"),
        "engine": idx.get("engine"),
        "status": idx.get("status"),
        "env_name": idx.get("env_name"),
        "started_at": idx.get("started_at"),
        "schema_main_version": idx.get("schema_main_version"),
        "metadata_incomplete": idx.get("metadata_incomplete"),
        "metrics": idx.get("metrics") or {},
    }


@router.get("/artifacts/{job_id}/{filename:path}")
def get_artifact(job_id: int, filename: str, request: Request):
    st = _app(request)
    job = db.get_job(st.db_path, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    from pathlib import Path

    root = Path(DATA_DIR, "reports", f"run_{job['plan_run_id']}", f"job_{job_id}").resolve()
    parts = filename.replace("\\", "/").split("/")
    if any(p.startswith(".") or ":" in p for p in parts):
        raise HTTPException(404, "artifact not found")
    target = root.joinpath(*parts).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, "artifact not found")
    path = str(target)
    if not os.path.exists(path):
        raise HTTPException(404, "artifact not found")
    return FileResponse(path)
