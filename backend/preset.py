"""首启/增量预置：Environment / Executor / AssetSource 配置实体 + 可演示计划。

配置中心约束（与冻结规格一致）：
- 预置数据只使用"受控 key"：解释器引用（python_ref）、受控工作目录根（cwd_root）、
  受控 dataset / Locust 场景引用。绝不写入任意命令、明文素材目录或密钥。
- 真实地址/凭据/资产路径必须由用户后续录入；本文件不擅自指向真实 Colyseus/Unity/生产环境。
- 旧库按需回填受控引用（只填空值，不覆盖已有配置），绝不改写历史 Job / report.json。
"""
from __future__ import annotations

import os

from app import db
from app.config_center import (DEFAULT_PYTHON, infer_interpreter_ref,
                               infer_workdir_root, resolve_interpreter)
from app.domain import Plan, PlanStep

BACKEND = os.path.dirname(os.path.abspath(__file__))        # .../platform/backend
DEMO_CWD = BACKEND
ROOT = os.path.dirname(BACKEND)                              # .../platform
GAME_PROJ = os.path.join(os.path.dirname(ROOT), "GameAutoTest-Pro")
LOCUST_VENV_PY = os.path.join(ROOT, ".venv-locust", "Scripts", "python.exe")
TRAE_PY = os.path.join(os.environ.get("APPDATA", ""), "TRAE SOLO CN", "ModularData",
                       "ai-agent", "vm", "tools", "python", "python.exe")

# 默认解释器统一取自 config_center（环境相关常量集中一处，可用 PLATFORM_DEFAULT_PYTHON 覆盖）

# 受控工作目录根 key（见 config_center.WORKDIR_ROOTS）
WORKDIR_BACKEND = "backend-demo"
WORKDIR_GAME = "gameautotest"


def _py() -> str:
    return os.environ.get("PLATFORM_PYTHON", DEFAULT_PYTHON)


def _locust_py() -> str:
    """隔离的 locust venv python；不存在时返回 ''（由调用方决定是否跳过 locust 预置）。"""
    return os.environ.get("PLATFORM_LOCUST_PYTHON") or (
        LOCUST_VENV_PY if os.path.exists(LOCUST_VENV_PY) else "")


def _matcheval_py() -> str:
    """MatchEval 解释器：与配置中心解析保持一致（优先能导入 cv2+numpy 的受控候选）。"""
    return resolve_interpreter("matcheval")


# ---------- 配置实体：按名称幂等预置（只引用受控 key） ----------
def _ensure_environment(db_path: str, name: str, host: str, port: int, *,
                        kind: str = "http", protocol: str = "http",
                        labels: list | None = None) -> int:
    for e in db.list_environments(db_path):
        if e["name"] == name:
            return e["id"]
    return db.seed_environment(db_path, name, host, port, kind=kind,
                               protocol=protocol, labels=labels or [])


def _ensure_executor(db_path: str, name: str, python: str, cwd: str, *,
                     python_ref: str, cwd_root: str, runners: list[str]) -> int:
    x = db.get_executor_by_name(db_path, name)
    if x:
        return x["id"]
    return db.seed_executor(db_path, name, python, cwd, python_ref=python_ref,
                            cwd_root=cwd_root, allowed_runners=list(runners))


def _ensure_asset_source(db_path: str, name: str, kind: str, **refs) -> int:
    a = db.get_asset_source_by_name(db_path, name)
    if a:
        return a["id"]
    data = {"name": name, "kind": kind, "enabled": 1}
    data.update({k: v for k, v in refs.items() if v})
    return db.create_asset_source(db_path, data)


def _plan_exists(db_path: str, name: str) -> bool:
    return any(p["name"] == name for p in db.list_plans(db_path))


def seed_if_empty(db_path: str, self_url: str | None = None) -> None:
    """增量补齐配置实体与计划（幂等：已有则跳过）。self_url 为本次平台实际绑定地址，
    供隔离 fixture 的 Locust 目标使用（平台自身 API 在跑即为可达）。"""
    # ---- Environment：联机环境（Colyseus）、离线评估环境、Locust 隔离 fixture ----
    env_id = _ensure_environment(db_path, "local", "localhost", 2567,
                                 kind="colyseus", labels=["colyseus", "online"])
    offline_env_id = _ensure_environment(db_path, "offline-eval", "", 0,
                                        kind="offline", labels=["offline", "matcheval"])

    # ---- Executor：受控解释器引用 + 受控工作目录根 + 允许的 Runner ----
    exec_id = _ensure_executor(db_path, "anaconda-python", _py(), DEMO_CWD,
                               python_ref="anaconda", cwd_root=WORKDIR_BACKEND,
                               runners=["pytest"])
    ui_exec_id = _ensure_executor(db_path, "anaconda-python-ui", _py(), DEMO_CWD,
                                  python_ref="anaconda", cwd_root=WORKDIR_BACKEND,
                                  runners=["pytest"])

    # ---- AssetSource：只引用受控 key；真实识别资产 / 真实场景须用户录入后再新增 ----
    asset_match_small = _ensure_asset_source(
        db_path, "matcheval-fixture-small", "matcheval", dataset_ref="fixture-small")
    _ensure_asset_source(db_path, "matcheval-fixture-empty", "matcheval",
                         dataset_ref="fixture-empty")
    asset_locust_http = _ensure_asset_source(
        db_path, "locust-http-fixture", "locust", scenario_ref="http-fixture")
    asset_locust_colyseus = _ensure_asset_source(
        db_path, "locust-colyseus", "locust", scenario_ref="colyseus-bot")

    # ---- 原有两个真实链路计划 ----
    if not _plan_exists(db_path, "平台自检 (green demo)"):
        db.create_plan(db_path, Plan(
            name="平台自检 (green demo)",
            description="自包含 pytest 冒烟：证明 计划→子进程→日志→报告 纵向闭环，不依赖被测服务",
            environment_id=env_id, executor_id=exec_id, fail_fast=True,
            steps=[PlanStep(
                engine="pytest", name="demo-smoke", requires_exclusive=False,
                params={"timeout_sec": 120, "cwd_root": WORKDIR_BACKEND,
                        "args": ["demo/test_platform_demo.py", "-o", "addopts=", "--tb=short"]},
            )],
        ))

    if not _plan_exists(db_path, "GameAutoTest API 冒烟 (需服务端)"):
        db.create_plan(db_path, Plan(
            name="GameAutoTest API 冒烟 (需服务端)",
            description="运行 GameAutoTest-Pro 的纯 API 冒烟测试；被测服务不可达会记录为 failed 并归档",
            environment_id=env_id, executor_id=exec_id, fail_fast=True,
            steps=[PlanStep(
                engine="pytest", name="api-smoke", requires_exclusive=False,
                params={"timeout_sec": 120, "cwd_root": WORKDIR_GAME,
                        "args": ["test_cases/test_api_check.py", "-o", "addopts=", "--tb=short"]},
            )],
        ))

    # 任务控制加固验证计划（engine 仍为 pytest，仅用于自动化验证控制语义）
    for plan in _CONTROL_PLANS(env_id, exec_id, ui_exec_id):
        if not _plan_exists(db_path, plan["name"]):
            db.create_plan(db_path, Plan(**plan))

    # ---- 第二引擎：Locust（仅当隔离 venv 可用时预置后续对象） ----
    locust_exec_id = loc_env_id = None
    locust_py = _locust_py()
    if locust_py:
        locust_exec_id = _ensure_executor(db_path, "anaconda-locust", locust_py, DEMO_CWD,
                                          python_ref="locust-venv", cwd_root=WORKDIR_BACKEND,
                                          runners=["pytest", "locust"])

        # 隔离 fixture 环境：host 指向平台自身绑定地址（平台在跑即可达）
        if self_url:
            host, port = "127.0.0.1", 8000
            try:
                from urllib.parse import urlparse
                u = urlparse(self_url)
                host = u.hostname or "127.0.0.1"
                port = u.port or 8000
            except Exception:  # noqa: BLE001
                pass
            loc_env_id = _ensure_environment(db_path, "locust-fixture", host, port,
                                             kind="http", labels=["fixture"])

        _SEED_LOCUST_PLANS(db_path, env_id, locust_exec_id, loc_env_id,
                           asset_locust_http, asset_locust_colyseus)

    # ---- 第三引擎：MatchEval（仅当解释器可用时预置；离线算法评估，用 offline 环境） ----
    # 执行机只声明它真正具备的能力：matcheval 解释器具备 cv2/numpy，但不等于具备 locust，
    # 故 allowed_runners 只含 pytest+matcheval，避免"声明能跑 locust 却缺依赖"的误导配置。
    mat_exec_id = None
    matcheval_py = _matcheval_py()
    if matcheval_py:
        mat_exec_id = _ensure_executor(db_path, "anaconda-matcheval", matcheval_py, DEMO_CWD,
                                       python_ref="matcheval", cwd_root=WORKDIR_BACKEND,
                                       runners=["pytest", "matcheval"])
        _SEED_MATCHEVAL_PLANS(db_path, offline_env_id, mat_exec_id, asset_match_small)

    # ---- 多引擎执行机：三引擎组合需要一个"同时具备 pytest+locust+cv2/numpy"的解释器。
    # 受控引用中只有 locust venv 同时满足三引擎依赖，故单列一台执行机；
    # 单一执行机若不满足依赖，locust 步骤会以 Runner 报错掩盖配置缺陷，故不混用。 ----
    full_exec_id = None
    if locust_py:
        full_exec_id = _ensure_executor(db_path, "three-engine-venv", locust_py, DEMO_CWD,
                                        python_ref="locust-venv", cwd_root=WORKDIR_BACKEND,
                                        runners=["pytest", "locust", "matcheval"])

    # ---- 三引擎组合：pytest + Locust + MatchEval 全部就绪才预置（串行 + 失败即停） ----
    if full_exec_id and loc_env_id:
        _SEED_THREE_ENGINE(db_path, loc_env_id, full_exec_id)

    # ---- 旧库回填：把迁移前直接存绝对路径/无资产引用的记录收敛为受控引用 ----
    _backfill_config(db_path)


def _SEED_LOCUST_PLANS(db_path: str, env_id: int, exec_id: int, loc_env_id: int | None,
                       asset_http: int, asset_colyseus: int) -> None:
    if loc_env_id is None:
        return
    if not _plan_exists(db_path, "冒烟+Locust(fixture 压测)"):
        db.create_plan(db_path, Plan(
            name="冒烟+Locust(fixture 压测)",
            description="组合计划：pytest 冒烟通过后，Locust(fixture) 对平台自身 API 压测；串行 + 失败即停",
            environment_id=loc_env_id, executor_id=exec_id, asset_source_id=asset_http,
            fail_fast=True,
            steps=[
                PlanStep(engine="pytest", name="smoke", requires_exclusive=False,
                         params={"timeout_sec": 120, "cwd_root": WORKDIR_BACKEND,
                                 "args": ["demo/test_platform_demo.py", "-o", "addopts=", "--tb=short"]}),
                PlanStep(engine="locust", name="locust-fixture", requires_exclusive=False,
                         params={"locustfile": "http-fixture", "users": 5, "spawn_rate": 2,
                                 "run_time": "15s", "csv_full_history": True, "timeout_sec": 180}),
            ],
        ))
    if not _plan_exists(db_path, "Locust-Colyseus(需服务端 2567)"):
        db.create_plan(db_path, Plan(
            name="Locust-Colyseus(需服务端 2567)",
            description="真实引擎：对 Colyseus 服务端(localhost:2567)跑 locustfile.py；服务不可达会诚实归档为 failed",
            environment_id=env_id, executor_id=exec_id, asset_source_id=asset_colyseus,
            fail_fast=True,
            steps=[PlanStep(
                engine="locust", name="colyseus-bot", requires_exclusive=False,
                params={"locustfile": "colyseus-bot", "users": 3, "spawn_rate": 1,
                        "run_time": "10s", "csv_full_history": False, "timeout_sec": 120},
            )],
        ))


def _SEED_MATCHEVAL_PLANS(db_path: str, env_id: int, exec_id: int, asset_id: int) -> None:
    if not _plan_exists(db_path, "MatchEval(小样本 fixture 评估)"):
        db.create_plan(db_path, Plan(
            name="MatchEval(小样本 fixture 评估)",
            description="保守单引擎评估：真实 match_eval 算法对隔离小样本 fixture 实跑，产出 precision/recall/F1 与热力图",
            environment_id=env_id, executor_id=exec_id, asset_source_id=asset_id, fail_fast=True,
            steps=[PlanStep(engine="matcheval", name="match-fixture", requires_exclusive=False,
                            params={"dataset": "fixture-small", "algorithm": ["tpl", "mstpl"],
                                    "threshold": 0.8, "output_level": "full", "timeout_sec": 180})],
        ))


def _SEED_THREE_ENGINE(db_path: str, env_id: int, exec_id: int) -> None:
    if not _plan_exists(db_path, "三引擎组合(pytest+Locust+MatchEval)"):
        db.create_plan(db_path, Plan(
            name="三引擎组合(pytest+Locust+MatchEval)",
            description="验证三引擎编排：pytest 冒烟 → Locust(fixture 压测) → MatchEval(fixture 评估)；串行 + 失败即停",
            environment_id=env_id, executor_id=exec_id, fail_fast=True,
            steps=[
                PlanStep(engine="pytest", name="smoke", requires_exclusive=False,
                         params={"timeout_sec": 120, "cwd_root": WORKDIR_BACKEND,
                                 "args": ["demo/test_platform_demo.py", "-o", "addopts=", "--tb=short"]}),
                PlanStep(engine="locust", name="locust-fixture", requires_exclusive=False,
                         params={"locustfile": "http-fixture", "users": 4, "spawn_rate": 2,
                                 "run_time": "12s", "csv_full_history": False, "timeout_sec": 120}),
                PlanStep(engine="matcheval", name="match-fixture", requires_exclusive=False,
                         params={"dataset": "fixture-small", "algorithm": ["tpl", "mstpl"],
                                 "threshold": 0.8, "output_level": "full", "timeout_sec": 180}),
            ],
        ))


def _CONTROL_PLANS(env_id: int, exec_id: int, ui_exec_id: int) -> list[dict]:
    def step(engine="pytest", name="", req_ex=False, args=(), timeout=120) -> PlanStep:
        return PlanStep(engine=engine, name=name, requires_exclusive=req_ex,
                        params={"timeout_sec": timeout, "cwd_root": WORKDIR_BACKEND,
                                "args": [*args, "-o", "addopts=", "--tb=short"]})

    return [
        dict(name="CTRL 成功", description="正常成功：全部 case 通过，PlanRun 聚合为 success",
             environment_id=env_id, executor_id=exec_id, fail_fast=True,
             steps=[step(name="pass", args=("demo/test_demo_ctl_pass.py",))]),
        dict(name="CTRL 失败", description="故意失败：case 断言失败，PlanRun 聚合为 failed",
             environment_id=env_id, executor_id=exec_id, fail_fast=True,
             steps=[step(name="fail", args=("demo/test_demo_ctl_fail.py",))]),
        dict(name="CTRL 长跑(可取消)", description="长跑用例，供 运行中取消 验证（cancelling→cancelled）",
             environment_id=env_id, executor_id=exec_id, fail_fast=True,
             steps=[step(name="slow", args=("demo/test_demo_ctl_slow.py",), timeout=600)]),
        dict(name="CTRL 超时", description="短超时看门狗：timeout=4s 强制杀进程树 → timedout",
             environment_id=env_id, executor_id=exec_id, fail_fast=True,
             steps=[step(name="timeout", args=("demo/test_demo_ctl_slow.py",), timeout=4)]),
        dict(name="CTRL 重试", description="失败后可重试成功：默认失败，置绿 flag 后重试通过",
             environment_id=env_id, executor_id=exec_id, fail_fast=True,
             steps=[step(name="retry", args=("demo/test_demo_ctl_retry.py",))]),
        dict(name="CTRL 互斥", description="同 Executor 两个 UI 独占 Job：前者结束后者才开始，锁不泄漏",
             environment_id=env_id, executor_id=ui_exec_id, fail_fast=False,
             steps=[step(name="ui-wait", req_ex=True, args=("demo/test_demo_ctl_wait.py",), timeout=30),
                    step(name="ui-pass", req_ex=True, args=("demo/test_demo_ctl_pass.py",), timeout=30)]),
    ]


# ---------- 旧库回填（迁移策略：只填空值，不覆盖、不改写历史） ----------
def _executor_engines(db_path: str, xid: int) -> set[str]:
    """由引用该执行机的计划步骤反推"允许的 Runner"，避免回填时误限引擎。"""
    out: set[str] = set()
    for p in db.list_plans(db_path):
        if p.get("executor_id") != xid:
            continue
        for s in p.get("steps") or []:
            if s.get("engine"):
                out.add(s["engine"])
    return out


def _plan_asset_source(db_path: str, plan: dict) -> int:
    """按计划步骤引用的受控 key 匹配资产源（只认 key，不落路径）。无匹配返回 0。"""
    assets = [a for a in db.list_asset_sources(db_path) if int(a.get("enabled", 1))]
    for s in plan.get("steps") or []:
        eng = s.get("engine")
        params = s.get("params") or {}
        key = params.get("dataset") if eng == "matcheval" else (
            params.get("locustfile") if eng == "locust" else None)
        if not key:
            continue
        for a in assets:
            if eng == "matcheval" and a.get("dataset_ref") == key:
                return a["id"]
            if eng == "locust" and a.get("scenario_ref") == key:
                return a["id"]
    return 0


def _backfill_config(db_path: str) -> None:
    """把旧库（迁移前直接存绝对路径 / 无资产引用）收敛为受控引用。
    只填空值，绝不覆盖已有配置；只改 plans/executors 配置行，绝不触碰历史 Job / report.json。"""
    for x in db.list_executors(db_path):
        upd: dict = {}
        if not x.get("python_ref"):
            ref = infer_interpreter_ref(x.get("python_executable") or "")
            if ref:
                upd["python_ref"] = ref
        if not x.get("cwd_root"):
            root = infer_workdir_root(x.get("cwd") or "")
            if root:
                upd["cwd_root"] = root
        if not (x.get("allowed_runners") or "").strip("[] "):
            engines = _executor_engines(db_path, x["id"])
            if engines:
                upd["allowed_runners"] = sorted(engines)
        if upd:
            db.update_executor(db_path, x["id"], upd)

    for p in db.list_plans(db_path):
        if p.get("asset_source_id"):
            continue
        aid = _plan_asset_source(db_path, p)
        if aid:
            # 迁移回填非用户编辑：不递增 revision，避免污染计划版本号
            db.update_plan(db_path, p["id"], {"asset_source_id": aid}, bump_revision=False)