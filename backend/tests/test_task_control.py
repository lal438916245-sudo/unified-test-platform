"""任务控制加固 · 单元测试：Job 状态机、PlanRun 聚合规则、重试链折叠。

运行：python -m pytest backend/tests/test_task_control.py -q
与 verify_control.py 的端到端集成验证互补；本文件只测纯函数/状态语义，不依赖服务。
"""
from __future__ import annotations

from app.domain import (STATUS_CANCELLED, STATUS_CANCELLING, STATUS_FAILED, STATUS_PARTIAL,
                        STATUS_QUEUED, STATUS_RUNNING, STATUS_SKIPPED, STATUS_SUCCESS,
                        STATUS_TIMEDOUT, aggregate_run_status, latest_attempt_statuses)
from app.orchestration.states import can_transition, is_terminal


# ---------- 状态机 ----------
def test_cancelling_is_not_terminal():
    assert not is_terminal("cancelling")
    assert "skipped" in {STATUS_SKIPPED}
    assert is_terminal("cancelled")
    assert is_terminal("timedout")
    assert is_terminal("skipped")


def test_transitions():
    assert can_transition(STATUS_QUEUED, STATUS_RUNNING)
    assert can_transition(STATUS_QUEUED, STATUS_CANCELLED)   # 未启动直接取消
    assert can_transition(STATUS_QUEUED, STATUS_SKIPPED)     # 失败即停
    assert can_transition(STATUS_RUNNING, STATUS_CANCELLING)  # 取消中间态
    assert can_transition(STATUS_CANCELLING, STATUS_CANCELLED)  # 回收完成后落 cancelled
    assert can_transition(STATUS_CANCELLING, STATUS_TIMEDOUT)
    assert can_transition(STATUS_RUNNING, STATUS_SUCCESS)
    # 非法：终态不可再迁移 / running 不可直达终态取消（必须经 cancelling）
    assert not can_transition("cancelled", "running")
    assert not can_transition(STATUS_RUNNING, STATUS_CANCELLED)


# ---------- PlanRun 聚合 ----------
def test_aggregate_priority_timedout_wins():
    assert aggregate_run_status([STATUS_SUCCESS, STATUS_TIMEDOUT]) == STATUS_TIMEDOUT


def test_aggregate_failed():
    assert aggregate_run_status([STATUS_SUCCESS, STATUS_FAILED]) == STATUS_FAILED
    # 单纯 success + skipped（无 failed）视作 partial；有 failed 则取 failed
    assert aggregate_run_status([STATUS_SUCCESS, STATUS_SKIPPED]) == STATUS_PARTIAL
    assert aggregate_run_status([STATUS_SUCCESS, STATUS_SKIPPED, STATUS_FAILED]) == STATUS_FAILED


def test_aggregate_partial():
    # 有成功 + 有被取消/跳过（无失败/超时）→ partial
    assert aggregate_run_status([STATUS_SUCCESS, STATUS_CANCELLED]) == STATUS_PARTIAL
    assert aggregate_run_status([STATUS_SUCCESS, STATUS_SKIPPED]) == STATUS_PARTIAL


def test_aggregate_cancelled():
    assert aggregate_run_status([STATUS_CANCELLED]) == STATUS_CANCELLED
    assert aggregate_run_status([STATUS_SUCCESS, STATUS_CANCELLED]) == STATUS_PARTIAL


def test_aggregate_success_and_empty():
    assert aggregate_run_status([]) == STATUS_QUEUED
    assert aggregate_run_status([STATUS_SUCCESS]) == STATUS_SUCCESS


# ---------- 重试链折叠（原失败 + 重试成功 => success） ----------
def _job(jid, status, retry_of=None, attempt=1):
    return {"id": jid, "status": status, "retry_of": retry_of, "attempt": attempt}


def test_retry_folding_success():
    jobs = [_job(1, STATUS_FAILED, None, 1), _job(2, STATUS_SUCCESS, 1, 2)]
    assert latest_attempt_statuses(jobs) == [STATUS_SUCCESS]
    assert aggregate_run_status(latest_attempt_statuses(jobs)) == STATUS_SUCCESS


def test_retry_folding_still_failed():
    jobs = [_job(1, STATUS_FAILED, None, 1), _job(2, STATUS_FAILED, 1, 2)]
    assert aggregate_run_status(latest_attempt_statuses(jobs)) == STATUS_FAILED


def test_retry_folding_latest_wins():
    # 三次尝试，取 attempt 最大者
    jobs = [_job(1, STATUS_FAILED, None, 1), _job(2, STATUS_CANCELLED, 1, 2),
            _job(3, STATUS_SUCCESS, 1, 3)]
    assert latest_attempt_statuses(jobs) == [STATUS_SUCCESS]