"""Job 状态机与迁移校验（对齐冻结状态机）。"""
from __future__ import annotations

from ..domain import TRANSITIONS, TERMINAL


def can_transition(frm: str, to: str) -> bool:
    if frm == to:
        return True
    return to in TRANSITIONS.get(frm, [])


def ensure_transition(frm: str, to: str, ctx: str = "") -> None:
    if not can_transition(frm, to):
        raise ValueError(f"非法状态迁移 {frm} -> {to} {ctx}")


def is_terminal(status: str) -> bool:
    return status in TERMINAL