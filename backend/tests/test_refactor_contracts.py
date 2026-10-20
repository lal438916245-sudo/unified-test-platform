"""Behavioral contracts for shared payload, process and JUnit helpers."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.payloads import asset_payload, environment_payload, executor_payload
from app.executors import base
from app.executors.junit import pytest_result
from app.executors.pytest_runner import PytestRunner


@pytest.mark.parametrize("normalize", [environment_payload, executor_payload, asset_payload])
def test_partial_payload_preserves_omission_and_explicit_null(normalize):
    assert normalize({}, partial=True) == {}
    assert normalize({"name": None, "enabled": None}, partial=True) == {}
    assert normalize({"enabled": False, "notes": "", "unknown": "ignored"}, partial=True) == {
        "notes": "",
        "enabled": 0,
    }


def test_create_payloads_keep_legacy_columns_and_independent_lists():
    first = executor_payload({})
    assert first["python_executable"] == first["cwd"] == ""
    assert "python_executable" not in executor_payload({}, partial=True)
    first["allowed_runners"].append("pytest")
    assert executor_payload({})["allowed_runners"] == []
    first = environment_payload({})
    first["labels"].append("local")
    assert environment_payload({})["labels"] == []
    assert environment_payload({"port": "2567"})["port"] == 2567
    assert executor_payload({"max_concurrency": "2"})["max_concurrency"] == 2


@pytest.mark.parametrize("normalize", [environment_payload, executor_payload, asset_payload])
def test_invalid_integer_payloads_still_raise(normalize):
    with pytest.raises(ValueError):
        normalize({"enabled": "invalid"})


@pytest.mark.parametrize("exit_code", [0, 1, 5])
def test_junit_results_preserve_failures_errors_skips_and_schema(tmp_path, exit_code):
    path = tmp_path / "results.xml"
    path.write_text(
        '<testsuites><testsuite tests="5" failures="1" errors="1" skipped="1" time="0.125"/>'
        '<testsuite tests="2" failures="0" errors="0" skipped="0" time="0.002"/></testsuites>',
        encoding="utf-8",
    )
    result = pytest_result(exit_code, str(path))
    assert result.status == ("success" if exit_code == 0 else "failed")
    assert result.summary == {
        "tests": 7,
        "passed": 4,
        "failed": 1,
        "errors": 1,
        "skipped": 1,
        "duration_ms": 127,
    }
    assert result.metrics["failed"] == 2
    assert result.engine_data == {"exit_code": exit_code}
    assert result.engine_data_schema == "pytest/1.0"
    assert result.artifacts == [
        {
            "name": "junit.xml",
            "type": "application/xml",
            "path": "results.xml",
            "size": path.stat().st_size,
        }
    ]


@pytest.mark.parametrize("content", [None, "<broken", '<testsuite tests="0"/>'])
def test_junit_missing_broken_or_empty_is_not_success(tmp_path, content):
    path = tmp_path / "junit.xml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    result = pytest_result(0, str(path))
    assert result.status == "failed"
    assert result.summary["tests"] == 0
    assert "tests==0" in result.error
    assert bool(result.artifacts) == (content is not None)
    if content == "<broken":
        assert "_parse_error" in result.summary


def test_runner_process_settings_and_registration_order(monkeypatch):
    events = []
    proc = object()

    def popen(command, **kwargs):
        events.append(("spawn", command, kwargs))
        return proc

    monkeypatch.setattr(base.subprocess, "Popen", popen)
    command = ["python", "-m", "pytest", "tests with spaces"]
    env = {"PYTHONIOENCODING": "utf-8"}
    result = base.start_proc(
        command,
        cwd="project",
        env=env,
        register_proc=lambda child: events.append(("register", child)),
    )
    assert result is proc
    assert events == [
        (
            "spawn",
            command,
            {
                "cwd": "project",
                "env": env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
            },
        ),
        ("register", proc),
    ]


def test_runner_spawn_failure_does_not_register(monkeypatch):
    registered = []

    def fail(*args, **kwargs):
        raise FileNotFoundError("missing interpreter")

    monkeypatch.setattr(base.subprocess, "Popen", fail)
    with pytest.raises(FileNotFoundError, match="missing interpreter"):
        base.start_proc(["missing"], cwd="project", env={}, register_proc=registered.append)
    assert registered == []


def test_pytest_result_keeps_runner_parser_and_artifact_overrides():
    class CustomRunner(PytestRunner):
        def _parse_junit(self, path):
            return {
                "tests": 1,
                "passed": 1,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "duration_ms": 7,
            }

        def _junit_artifact(self, path):
            return [{"name": path}]

    result = CustomRunner()._finalize(0, "custom.xml")
    assert result.status == "success"
    assert result.metrics["passed"] == 1
    assert result.metrics["duration_ms"] == 7
    assert result.artifacts == [{"name": "custom.xml"}]
