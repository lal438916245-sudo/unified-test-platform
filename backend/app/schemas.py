"""report.json 协议：严格按已冻结 schema 构建与校验。"""
from __future__ import annotations

import json
import os
from typing import Any

from .domain import REPORT_SCHEMA_VERSION

REQUIRED_TOP_LEVEL = [
    "schema_version",
    "plan_run_id",
    "job_id",
    "engine",
    "status",
    "started_at",
    "ended_at",
    "environment_snapshot",
    "summary",
    "metrics",
    "artifacts",
]


def build_report(*,
                 plan_run_id: int,
                 job_id: int,
                 engine: str,
                 status: str,
                 started_at: str,
                 ended_at: str,
                 environment_snapshot: dict,
                 summary: dict,
                 metrics: dict,
                 artifacts: list[dict],
                 engine_data: dict | None = None,
                 engine_data_schema: str = "pytest/1.0") -> dict:
    """构造一份结构完整、合法的 report.json（不写文件）。"""
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "plan_run_id": f"pr_{plan_run_id}",
        "job_id": f"job_{job_id}",
        "engine": engine,
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "environment_snapshot": environment_snapshot,
        "summary": summary,
        "metrics": metrics,
        "artifacts": artifacts,
    }
    if engine_data is not None:
        report["engine_data"] = engine_data
    if engine_data_schema:
        report["engine_data_schema"] = engine_data_schema
    return report


def validate_report(report: dict) -> list[str]:
    """返回缺少/非法字段的错误列表；空列表表示合法。"""
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["report 必须是对象"]
    for key in REQUIRED_TOP_LEVEL:
        if key not in report:
            errors.append(f"缺少顶层字段: {key}")
    if "schema_version" in report and report["schema_version"] != REPORT_SCHEMA_VERSION:
        errors.append(f"schema_version 应等于 {REPORT_SCHEMA_VERSION}")
    if "plan_run_id" in report and not str(report["plan_run_id"]).startswith("pr_"):
        errors.append("plan_run_id 应以 pr_ 开头")
    if "job_id" in report and not str(report["job_id"]).startswith("job_"):
        errors.append("job_id 应以 job_ 开头")
    if "artifacts" in report:
        for i, a in enumerate(report["artifacts"]):
            if not isinstance(a, dict):
                errors.append(f"artifacts[{i}] 应为对象")
                continue
            for sub in ("name", "type", "path", "size"):
                if sub not in a:
                    errors.append(f"artifacts[{i}] 缺少字段 {sub}")
    return errors


def to_report_dir(base_dir: str, plan_run_id: int, job_id: int) -> str:
    d = os.path.join(base_dir, f"run_{plan_run_id}", f"job_{job_id}")
    os.makedirs(d, exist_ok=True)
    return d


def write_report(base_dir: str, plan_run_id: int, job_id: int, report: dict) -> tuple[str, str]:
    """落盘 report.json，返回 (dir, file_path)。"""
    rdir = to_report_dir(base_dir, plan_run_id, job_id)
    path = os.path.join(rdir, "report.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return rdir, path