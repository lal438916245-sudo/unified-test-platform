from .base import BaseRunner, RunnerResult, RunnerCtx
from .pytest_runner import PytestRunner
from .locust_runner import LocustRunner
from .matcheval_runner import MatchEvalRunner

# 引擎标识 -> Runner 实例
RUNNERS: dict[str, BaseRunner] = {
    "pytest": PytestRunner(),
    "locust": LocustRunner(),
    "matcheval": MatchEvalRunner(),
}


def get_runner(engine: str) -> BaseRunner:
    runner = RUNNERS.get(engine)
    if runner is None:
        raise ValueError(f"未注册的引擎: {engine}")
    return runner


__all__ = ["RUNNERS", "get_runner", "BaseRunner", "RunnerResult", "RunnerCtx"]