"""Airtest/Poco adapter: subprocess isolation, static refs, phase and evidence."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from ..ui_config import BACKEND, UI_SUITES, device_fingerprint, get_device, ui_precheck
from .base import BaseRunner, RunnerResult, child_env, drain_proc, reap_proc, start_proc
from .junit import pytest_result


class AirtestRunner(BaseRunner):
    engine = "airtest"

    def run(self, job, env, executor, junit_path, log_sink, is_cancelled, register_proc=None):
        params = job.get("params") or {}
        db_path = job.get("_db_path")
        dev = get_device(params.get("device"), db_path)
        errors = ui_precheck(params, db_path, job.get("executor_id"), dev)
        if (
            UI_SUITES.get(params.get("suite"), {}).get("requires_service")
            and env.get("kind") == "offline"
        ):
            errors.append("该 UI 套件必须选择联机 Environment")
        output = Path(junit_path).parent
        output.mkdir(parents=True, exist_ok=True)
        meta = {
            "phase": "prepare",
            "failure_kind": "infrastructure",
            "suite": params.get("suite"),
            "case": params.get("case"),
            "device": params.get("device"),
        }
        if errors:
            meta["error"] = "UI 准备失败：" + "；".join(errors)
            return RunnerResult(
                status="failed",
                exit_code=-1,
                error=meta["error"],
                engine_data=meta,
                engine_data_schema="airtest/1.0",
            )
        from ..config_center import build_content_fingerprint

        current = build_content_fingerprint("airtest", params, db_path, dev)
        saved = (job.get("config_snapshot") or {}).get("content")
        if saved and saved.get("fingerprint") != current.get("fingerprint"):
            meta["error"] = "UI 套件或设备配置已变化；请新建执行，重试不能复现原快照"
            return RunnerResult(
                status="failed",
                exit_code=-1,
                error=meta["error"],
                engine_data=meta,
                engine_data_schema="airtest/1.0",
            )
        suite = UI_SUITES[params["suite"]]
        from ..runtime_requirements import resolve_requirement

        requirements = resolve_requirement(params, db_path)
        config = {
            "root": suite["root"],
            "case": params["case"],
            "device_config": dev,
            "runtime_requirements": requirements,
            "wait_timeout": float(params.get("wait_timeout", 10)),
            "junit": str(Path(junit_path).resolve()),
            "output": str(output.resolve()),
        }
        # Runtime secrets/paths stay in a temporary private job input, never artifacts.
        input_path = output / ".ui-input.json"
        input_path.write_text(json.dumps(config), encoding="utf-8")
        child = child_env(
            {
                "PYTHONPATH": str(BACKEND),
                "PYTEST_ADDOPTS": "",
                "PYTHONDONTWRITEBYTECODE": "1",
                "POCO_DEVICE": dev.get("uri", ""),
                "POCO_HOST": dev["host"],
                "POCO_PORT": str(dev["port"]),
                "GAME_SERVER_HOST": str(env.get("host") or "localhost"),
                "GAME_SERVER_PORT": str(env.get("port") or 2567),
                "PLATFORM_UI_SMOKE_SELECTOR": dev.get("smoke_selector", ""),
                "PLATFORM_UI_SMOKE_RESULT": dev.get("smoke_result", ""),
                "PLATFORM_UI_WAIT_TIMEOUT": str(config["wait_timeout"]),
            }
        )
        log_sink("[airtest] UI 独占执行；连接设备 → Poco 准备 → 用例 → 证据归档\n")

        def redact(text):
            for raw in (
                str(output.resolve()),
                suite["root"],
                str(BACKEND),
                str(Path(executor["python_executable"]).parent),
            ):
                for variant in (raw, raw.replace("\\", "/"), raw.replace("\\", "\\\\")):
                    text = text.replace(variant, "[runtime]")
            return text

        try:
            proc = start_proc(
                [
                    executor["python_executable"],
                    "-m",
                    "app.executors.ui_worker",
                    str(input_path.resolve()),
                ],
                cwd=str(BACKEND),
                env=child,
                register_proc=register_proc,
            )
            outcome = drain_proc(
                proc, lambda line: log_sink(redact(line)), is_cancelled, "[cancel] 终止 UI 进程树\n"
            )
            reap_proc(proc)
        finally:
            input_path.unlink(missing_ok=True)
        result = pytest_result(proc.returncode, junit_path)
        path = output / "ui-result.json"
        if path.is_file():
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                meta["error"] = "UI 阶段结果不可读"
        else:
            meta["error"] = "UI 子进程未生成阶段结果；检查解释器与依赖"
        if meta.get("error"):
            result.status = "failed"
            result.error = meta["error"]
        if result.status == "success" and not any(
            c.get("when") == "call" and c.get("outcome") in ("passed", "failed")
            for c in meta.get("cases", [])
        ):
            result.status = "failed"
            result.error = "UI 未执行任何用例主体（零用例或全部跳过）"
            meta.update(phase="run", failure_kind="execution", error=result.error)
        if outcome == "cancelled":
            result.status, result.error = "cancelled", "cancelled by user"
        meta.update(
            suite=params["suite"],
            case=params["case"],
            device=params["device"],
            runtime_requirements=requirements,
            device_fingerprint=device_fingerprint(params["device"], device=dev),
            reset="none",
        )
        result.engine_data, result.engine_data_schema = meta, "airtest/1.0"
        for p in output.rglob("*"):
            if (
                p.is_file()
                and p.suffix in (".txt", ".log", ".json", ".xml")
                and not p.name.startswith(".")
            ):
                try:
                    p.write_text(redact(p.read_text(encoding="utf-8")), encoding="utf-8")
                except (UnicodeError, OSError):
                    pass
        result.artifacts = [
            {
                "name": p.relative_to(output).as_posix(),
                "path": p.relative_to(output).as_posix(),
                "type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                "size": p.stat().st_size,
            }
            for p in output.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        ]
        return result
