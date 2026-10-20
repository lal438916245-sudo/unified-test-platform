"""pytest Runner：以独立子进程运行 pytest，实时回传日志，junit-xml 结构化结果。"""

from __future__ import annotations

import json
import os

from .base import BaseRunner, RunnerResult, child_env, drain_proc, reap_proc, shell_cmd, start_proc
from .junit import junit_artifacts, parse_junit, pytest_result


class PytestRunner(BaseRunner):
    engine = "pytest"

    def run(
        self, job, env, executor, junit_path, log_sink, is_cancelled, register_proc=None
    ) -> RunnerResult:
        python = executor["python_executable"]
        params = job["params"] or {}
        from ..config_center import resolve_pytest_workdir

        cwd = resolve_pytest_workdir(params, executor)
        if not cwd or not os.path.isdir(cwd):
            return RunnerResult(
                status="failed",
                exit_code=-1,
                error="测试项目目录未配置或已移动，请检查本机路径配置 / 项目登记",
            )
        args = params.get("args") or ["demo/test_platform_demo.py", "-o", "addopts=", "--tb=short"]
        from ..ui_config import BACKEND, pytest_ui_errors, ui_files

        errors = pytest_ui_errors({**params, "args": args}, cwd)
        if errors:
            return RunnerResult(status="failed", exit_code=-1, error="；".join(errors))

        cmd = [python, "-m", "pytest", *args, "-p", "platform_ui_guard", "--junitxml=" + junit_path]
        log_sink(f"$ {shell_cmd(cmd)}\n$ cwd={cwd}\n")

        proc = start_proc(
            cmd,
            cwd=cwd,
            env=child_env(
                {
                    "PYTHONPATH": str(BACKEND) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                    "PLATFORM_UI_BOUND_FILES": json.dumps([str(p) for p in ui_files()]),
                    "PLATFORM_ENV_KIND": str(env.get("kind") or "http"),
                    "PLATFORM_ENV_HOST": str(env.get("host") or ""),
                    "PLATFORM_ENV_PORT": str(env.get("port") or 0),
                    "PLATFORM_ENV_PROTOCOL": str(env.get("protocol") or "http"),
                    "GAME_SERVER_HOST": str(env.get("host") or "localhost"),
                    "GAME_SERVER_PORT": str(env.get("port") or 2567),
                    "PYTEST_ADDOPTS": "",
                }
            ),
            register_proc=register_proc,
        )

        outcome = drain_proc(
            proc, log_sink, is_cancelled, "\n[cancel] 收到取消请求，终止 pytest 进程树\n"
        )
        if outcome == "cancelled":
            return RunnerResult(status="cancelled", exit_code=130, error="cancelled by user")
        reap_proc(proc)
        return self._finalize(proc.returncode, junit_path)

    def _finalize(self, exit_code: int, junit_path: str) -> RunnerResult:
        return pytest_result(exit_code, junit_path, self._parse_junit, self._junit_artifact)

    def _parse_junit(self, junit_path: str) -> dict:
        return parse_junit(junit_path)

    def _junit_artifact(self, junit_path: str) -> list[dict]:
        return junit_artifacts(junit_path)
