"""Shared JUnit parsing and pytest results for API and UI runners."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from typing import Optional

from .base import RunnerResult


def parse_junit(junit_path: str) -> dict:
    summary = {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "duration_ms": 0}
    if not junit_path or not os.path.exists(junit_path):
        return summary
    root = ET.parse(junit_path).getroot()
    for suite in root.iter("testsuite"):
        attrs = suite.attrib
        tests = int(attrs.get("tests", "0"))
        failed = int(attrs.get("failures", "0"))
        errors = int(attrs.get("errors", "0"))
        skipped = int(attrs.get("skipped", "0"))
        time_s = float(attrs.get("time", "0"))
        summary["tests"] += tests
        summary["failed"] += failed
        summary["errors"] += errors
        summary["skipped"] += skipped
        summary["duration_ms"] += int(time_s * 1000)
    summary["passed"] = max(
        0, summary["tests"] - summary["failed"] - summary["errors"] - summary["skipped"]
    )
    return summary


def junit_artifacts(junit_path: str) -> list[dict]:
    if junit_path and os.path.exists(junit_path):
        return [
            {
                "name": "junit.xml",
                "type": "application/xml",
                "path": os.path.basename(junit_path),
                "size": os.path.getsize(junit_path),
            }
        ]
    return []


def pytest_result(
    exit_code: int, junit_path: str, parse_summary=None, collect_artifacts=None
) -> RunnerResult:
    summary = {"tests": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "duration_ms": 0}
    status = "success" if exit_code == 0 else "failed"
    err: Optional[str] = None
    try:
        summary = (parse_summary or parse_junit)(junit_path)
    except Exception as e:  # noqa: BLE001 —— 报告解析失败不能中断流程
        summary["_parse_error"] = str(e)

    # 诚实归档兜底：退出码 0 但**一个用例都没执行**（如 --collect-only / --fixtures /
    # --help），绝不能判 success —— 那等于「测试没跑却归档成功」，是平台立身之本的反面。
    # 注意：正常"零用例"时 pytest 退出码是 5，不是 0，所以这里不会误伤。
    if status == "success" and int(summary.get("tests") or 0) == 0:
        status = "failed"
        err = (
            "pytest 退出码 0 但未执行任何用例（tests==0）；"
            "疑似传入 --collect-only / --fixtures / --help 等不执行用例的开关"
        )

    metrics = {
        "tests": summary["tests"],
        "passed": summary["passed"],
        "failed": summary["failed"] + summary["errors"],
        "skipped": summary["skipped"],
        "duration_ms": summary["duration_ms"],
    }
    artifacts = (collect_artifacts or junit_artifacts)(junit_path)
    return RunnerResult(
        status=status,
        exit_code=exit_code,
        summary=summary,
        metrics=metrics,
        artifacts=artifacts,
        engine_data={"exit_code": exit_code},
        engine_data_schema="pytest/1.0",
        error=err,
    )
