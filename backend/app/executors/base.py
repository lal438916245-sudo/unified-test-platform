"""执行层基类：统一 Runner 接口。执行必须走独立子进程，绝不在本线程内跑测试。"""
from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


def kill_proc_tree(proc: subprocess.Popen) -> None:
    """彻底终止整个进程树。Windows 用 taskkill /T /F（含全部子进程），
    否则仅 terminate/kill 父进程，避免残留 pytest 子进程孤岛。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=6,
            )
        else:
            proc.terminate()
    except Exception:  # noqa: BLE001 —— 杀进程失败不致命，交给回收后再看
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

# 编排线程向 Runner 提供的两个回调：
#   is_cancelled()  -> 取消探测
#   register_proc(proc) -> 把 Runner 内部创建的 Popen 交给编排看门狗（用于超时强杀）
RunnerCtx = Callable[[], bool]
RegisterProc = Callable[[Any], None]


def child_env(*extra: dict) -> dict:
    """构造被测子进程的环境变量：继承本进程 + 统一强制 UTF-8 输出。

    为什么必须注入 ``PYTHONIOENCODING=utf-8``：
      Windows 上 Python 子进程的标准流出到**管道**时，默认用 locale 编码（简中即 cp936/GBK），
      而三个 Runner 都按 UTF-8 解码（``encoding="utf-8", errors="replace"``）。
      GBK 字节被 UTF-8 解出来就是 U+FFFD 替换字符，**一旦写进日志文件就不可逆**——
      日志与报告里的中文会永久变成 `����ƽ̨` 这种乱码（子进程输出 utf-8 才是对的）。

    只影响子进程的 stdio 编码，不改文件系统编码（故不用 PYTHONUTF8=1，避免影响被测项目的读文件行为）。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    for d in extra:
        if d:
            env.update(d)
    return env



@dataclass
class RunnerResult:
    status: str                       # success | failed | cancelled | timedout
    exit_code: int = 0
    summary: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)
    engine_data: Optional[dict] = None
    engine_data_schema: str = "generic/1.0"
    error: Optional[str] = None


class BaseRunner(ABC):
    engine: str = "generic"

    @abstractmethod
    def run(self,
            job: dict,
            env: dict,
            executor: dict,
            junit_path: str,
            log_sink: Callable[[str], None],
            is_cancelled: RunnerCtx,
            register_proc: Optional[RegisterProc] = None) -> RunnerResult:
        """启动独立子进程执行引擎并回收结果。log_sink 接收已含换行的文本行。"""
        raise NotImplementedError