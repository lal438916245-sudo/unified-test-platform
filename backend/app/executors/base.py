"""执行层基类：统一 Runner 接口。执行必须走独立子进程，绝不在本线程内跑测试。"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
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


def shell_cmd(parts: list[str]) -> str:
    """把命令列表渲染成一行可读文本（**只用于日志**，绝不用于执行）。"""
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


def reap_proc(proc: subprocess.Popen, timeout: float = 6.0) -> None:
    """有界回收子进程：先 wait(timeout)，超时才 kill 再 wait。**绝不无限等待**。"""
    if proc is None:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            pass


def _close_stdout(proc: subprocess.Popen) -> None:
    """尽力关闭本端读句柄，让后台读线程摆脱阻塞。

    ⚠️ 必须**异步**执行：若读线程正阻塞在 readline（管道写端被孤儿孙进程持有），
    直接 ``proc.stdout.close()`` 会等读线程释放锁而**同样卡住**（实测可卡到孤儿退出为止，
    那就白改了）。因此把它丢进守护线程，主流程绝不为它等待。
    """
    def _do() -> None:
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_do, daemon=True).start()


def drain_proc(proc: subprocess.Popen, log_sink: Callable[[str], None],
               is_cancelled: Callable[[], bool], cancel_note: str,
               eof_grace: float = 5.0, poll: float = 0.3) -> str:
    """把子进程输出逐行喂给 log_sink，直到 EOF。返回 ``'eof' | 'cancelled' | 'stalled'``。

    为什么不能简单写 ``while line: readline()``：
      Windows 上 ``taskkill /T /F`` 之后，若有**脱离进程树的孙进程**仍继承着 stdout 管道的写端，
      ``readline()`` 就永远等不到 EOF —— 整个编排线程会**永久卡死在这个 Job**，
      后续 Job 全部不执行（README 里「Job#24 约 69s 退出延迟、根因未确证」高度吻合此模式）。
      所以把「读」放到后台线程，主循环只做三件事：转发日志、响应取消、识别"孤儿管道"。

    约定：
      · 返回 ``'eof'``       → 正常读完，调用方 reap 后按产物归档；
      · 返回 ``'cancelled'`` → 收到取消请求，进程树已强杀，调用方应直接归 cancelled；
      · 返回 ``'stalled'``   → **进程已退出**但管道迟迟没有 EOF（孤儿持有写端）；
                              此时不再等，调用方仍可按**已落盘的产物**归档（数据是完整的）。
    """
    q: "queue.Queue" = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:  # noqa: BLE001 —— 强杀进程时管道读取会抛，属预期
            pass
        finally:
            q.put(None)                      # EOF 哨兵

    threading.Thread(target=_reader, daemon=True).start()

    exited_at: Optional[float] = None
    while True:
        try:
            item = q.get(timeout=poll)
        except queue.Empty:
            item = ""                        # 超时：仅用于做取消/存活检查，不是数据
        if item is None:
            return "eof"
        if item:
            log_sink(item)
        if is_cancelled():
            log_sink(cancel_note)
            kill_proc_tree(proc)
            _close_stdout(proc)
            reap_proc(proc)
            return "cancelled"
        if proc.poll() is not None:
            if exited_at is None:
                exited_at = time.monotonic()
            elif time.monotonic() - exited_at > eof_grace:
                log_sink("\n[warn] 子进程已退出但输出管道未收到 EOF（疑似孤儿孙进程仍持有写端），"
                         "已跳过等待并按已落盘产物归档。\n")
                kill_proc_tree(proc)
                _close_stdout(proc)
                return "stalled"



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