"""领域常量与状态定义：严格对齐已冻结的规格（Plan/PlanRun/Job/Report/Environment/Executor）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ---- Job 状态机（任务控制加固后冻结）----
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_CANCELLING = "cancelling"      # 已发起取消、标记者，子进程正在被回收（中间态，非终态）
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_TIMEDOUT = "timedout"
STATUS_SKIPPED = "skipped"            # 失败即停/整体取消导致"未执行"的 Job（终态，可追溯）

# 真正进入终端状态的集合（终态不可再迁移）
TERMINAL = {STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMEDOUT, STATUS_SKIPPED}

# 合法状态迁移：{from: [to]}。MVP 由调度器单线程维护，并在此校验。
TRANSITIONS: dict[str, list[str]] = {
    STATUS_QUEUED: [STATUS_RUNNING, STATUS_CANCELLED, STATUS_SKIPPED],
    STATUS_RUNNING: [STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLING, STATUS_TIMEDOUT],
    STATUS_CANCELLING: [STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMEDOUT],
}


def aggregate_run_status(job_statuses: list[str]) -> str:
    """PlanRun 聚合规则（写入规格）。优先级：timedout > failed > partial > cancelled > success。

    - timedout : 任一 Job 超时 => run=timedout
    - failed   : 任一 Job failed（且无 timedout）=> run=failed（含失败即停：后继被 skipped）
    - partial  : 有 Job 成功，同时存在被取消/被跳过而未完成的 Job（无 failed/timeout）=> run=partial
    - cancelled: 无成功、无失败、无超时，但存在被取消的 Job（如整体取消）=> run=cancelled
    - success  : 其余组合（无 failed/timeout，且不同时含 success 与 skipped）=> run=success
    """
    if not job_statuses:
        return STATUS_QUEUED
    has_success = any(s == STATUS_SUCCESS for s in job_statuses)
    has_failed = any(s == STATUS_FAILED for s in job_statuses)
    has_timedout = any(s == STATUS_TIMEDOUT for s in job_statuses)
    has_cancelled = any(s == STATUS_CANCELLED for s in job_statuses)
    has_skipped = any(s == STATUS_SKIPPED for s in job_statuses)
    if has_timedout:
        return STATUS_TIMEDOUT
    if has_failed:
        return STATUS_FAILED
    if has_success:
        return STATUS_PARTIAL if (has_cancelled or has_skipped) else STATUS_SUCCESS
    if has_cancelled:
        return STATUS_CANCELLED
    if has_skipped:
        # 防御：无 success 却只有 skipped（如手工特殊状态），归为 failed。
        return STATUS_FAILED
    return STATUS_SUCCESS


def latest_attempt_statuses(jobs: list[dict]) -> list[str]:
    """把同一重试链（原 Job + 各次重试）折叠为该链最新一次尝试的状态。

    原 Job 的 retry_of 为空，其后续每次重试的 retry_of 都指向链根（原 job id）。
    故用 root=retry_of or id 归组，每链仅保留 attempt 最大的那次状态参与 PlanRun 聚合，
    保证"原失败后被重试成功"时 run 聚合为 success，且重试失败时仍正确聚合为 failed。
    """
    latest: dict[int, dict] = {}
    for j in jobs:
        root = j.get("retry_of") or j["id"]
        cur = latest.get(root)
        if cur is None or (j.get("attempt") or 1) > (cur.get("attempt") or 1):
            latest[root] = j
    return [j["status"] for j in latest.values()]


REPORT_SCHEMA_VERSION = "1.0"


# run 级别的"部分成功"聚合状态，仅用于 plan_runs.status，不参与 Job 状态机
STATUS_PARTIAL = "partial"
# 终态 run 取值集合
RUN_TERMINAL = {STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED, STATUS_TIMEDOUT, STATUS_PARTIAL}


@dataclass
class Environment:
    id: int
    name: str
    host: str
    port: int
    kind: str = "http"
    protocol: str = "http"
    secret_ref: str = ""
    secret_kind: str = ""
    labels: list[str] = field(default_factory=list)
    notes: str = ""
    enabled: int = 1


@dataclass
class Executor:
    id: int
    name: str
    python_executable: str
    cwd: str
    python_ref: str = ""              # 受控解释器引用 key（首选）
    cwd_root: str = ""                # 受控工作目录根 key
    allowed_runners: list[str] = field(default_factory=list)
    max_concurrency: int = 1
    notes: str = ""
    enabled: int = 1


@dataclass
class AssetSource:
    id: int
    name: str
    kind: str                          # matcheval | locust
    dataset_ref: str = ""
    tpl_ref: str = ""
    scenes_ref: str = ""
    ground_truth_ref: str = ""
    airtest_ref: str = ""
    scenario_ref: str = ""
    notes: str = ""
    enabled: int = 1


@dataclass
class PlanStep:
    engine: str
    name: str
    requires_exclusive: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    # ---- v0.2 TestCase 引用（可选）----
    # case_ref: {"id": int, "revision": int, "fingerprint": str}
    # override_params: 仅覆盖 _STEP_PARAM_KEYS[engine] 内、且不允许 engine/asset_source_id
    case_ref: Optional[dict[str, Any]] = None
    override_params: Optional[dict[str, Any]] = None
    # 仅 case_ref 形态有意义：记录"计划是否**显式**覆盖了 name / requires_exclusive"。
    # 必须区分"未提供"与"提供了 False"，否则 (False) 会被当成显式覆盖，
    # 静默盖掉用例自身的 requires_exclusive —— 这正是 v0.2 要避免的静默行为。
    name_set: bool = False
    exclusive_set: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为 plans.steps[] 的一个元素（**存储形态**，全项目唯一出口）。

        ⚠️ 硬规则：新键 case_ref / override_params **只在 case_ref 存在时写入**。
        legacy 内联步骤必须与 v0.1 逐字节一致（键集合、键顺序、值都不变），
        否则旧计划在"什么都没改"的情况下 steps JSON 会变，进而让
        plan_snapshot 指纹发生漂移 —— 这是 v0.2 的 P0 兼容性约束。

        存储形态（引用）：
            {"case_ref": {...}, "override_params": {...}[, "name"][, "requires_exclusive"]}
          —— 不写 engine / params（它们是用例定义的属性，有效值在展开时产生）；
             name / requires_exclusive 仅在**显式覆盖**时写入，缺省表示"继承用例"。
        存储形态（legacy 内联）：{"engine", "name", "requires_exclusive", "params"}
        """
        if self.case_ref:
            d: dict[str, Any] = {
                "case_ref": dict(self.case_ref),
                "override_params": dict(self.override_params or {}),
            }
            if self.name_set:
                d["name"] = self.name
            if self.exclusive_set:
                d["requires_exclusive"] = bool(self.requires_exclusive)
            return d
        return {
            "engine": self.engine,
            "name": self.name,
            "requires_exclusive": self.requires_exclusive,
            "params": self.params,
        }


@dataclass
class Plan:
    name: str
    description: str
    environment_id: int
    executor_id: int
    steps: list[PlanStep]
    asset_source_id: int = 0
    fail_fast: bool = True
    owner: str = "local"
    id: int = 0


@dataclass
class PlanRun:
    id: int
    plan_id: int
    status: str = STATUS_QUEUED
    started_at: Optional[str] = None
    ended_at: Optional[str] = None


@dataclass
class Job:
    plan_run_id: int
    plan_id: int
    engine: str
    name: str
    environment_id: int
    executor_id: int
    timeout_sec: int = 300
    requires_exclusive: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    config_snapshot: Optional[dict[str, Any]] = None   # Job 创建时固化的脱敏配置快照
    status: str = STATUS_QUEUED
    parent_job_id: Optional[int] = None
    retry_of: Optional[int] = None   # 若为重试 Job，指向被重试的原始 Job id
    attempt: int = 1                 # 第几次尝试（原始=1，重试=原+1）
    cancel_requested: int = 0
    exit_code: Optional[int] = None
    log_path: Optional[str] = None
    report_path: Optional[str] = None
    junit_path: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    error: Optional[str] = None
    id: int = 0


@dataclass
class Report:
    id: int
    job_id: int
    plan_run_id: int
    report_path: str
    report_json: dict[str, Any]
    created_at: str