"""编排调度器：单编排线程串行拉起独立子进程并流式收日志，单看门狗线程负责超时。
测试执行永远发生在子进程里，本线程只做编排/读取/轮询。"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Optional

from .. import db
from ..config_center import (build_config_snapshot, resolve_runtime_env,
                             resolve_runtime_executor, snapshot_env_view)
from ..domain import (TERMINAL, STATUS_CANCELLING, STATUS_CANCELLED, STATUS_TIMEDOUT,
                      aggregate_run_status, latest_attempt_statuses)
from ..executors import get_runner
from ..executors.base import RunnerResult, kill_proc_tree
from ..schemas import build_report, validate_report, write_report

# 平台数据根目录：platform/data（db.sqlite、logs/、reports/）
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))      # app
PLATFORM = os.path.dirname(BACKEND)                                        # backend
BASE = os.path.dirname(PLATFORM)                                           # platform
DATA_DIR = os.environ.get("PLATFORM_DATA_DIR") or os.path.join(BASE, "data")

SLEEP = 0.4
POLL = 0.4
MAX_LOG_LINES = 4000


class Orchestrator(threading.Thread):
    def __init__(self, db_path: str):
        super().__init__(daemon=True, name="orchestrator")
        self.db_path = db_path
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cancel: set[int] = set()
        self._cancel_pending: set[int] = set()
        self._logs: dict[int, list[str]] = {}
        # 当前运行 Job 的共享状态（供看门狗读取）
        self._proc: Optional[subprocess.Popen] = None
        self._job_id: Optional[int] = None
        self._job_started: float = 0.0
        self._job_timeout: float = 0.0
        self._timedout = False

    # ---------- 生命周期 ----------
    def shutdown(self) -> None:
        self._stop.set()

    def run(self) -> None:
        watchdog = threading.Thread(target=self._watchdog_loop, daemon=True,
                                    name="sched-watchdog")
        watchdog.start()
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001 —— 编排错误不打死服务
                self._logs.setdefault(-1, []).append(f"[orchestrator] {type(e).__name__}: {e}\n")
            time.sleep(SLEEP)

    # ---------- 主循环（串行，全局同一时刻至多一个 Job） ----------
    def _tick(self) -> None:
        if db.has_running_job(self.db_path):
            return
        job = db.find_queued_job(self.db_path)
        if job is None:
            return
        self._process(job)

    # ---------- 取消 / 重试 ----------
    def request_cancel(self, job_id: int) -> None:
        """幂等取消。终态/重复请求直接忽略；绝不在子进程退出前标记终态 cancelled。"""
        job = db.get_job(self.db_path, job_id)
        if not job:
            return
        status = job["status"]
        if status == "queued":
            # 尚未启动：直接置终态 cancelled，无需触碰子进程
            db.update_job(self.db_path, job_id,
                          {"status": "cancelled", "ended_at": now_ts()})
            self._finalize_run(job["plan_run_id"])
            return
        if status == "running":
            # 中间态：置 cancelling（非终态），登记取消并终止进程树，等待 Runner 回收后落 cancelled
            with self._lock:
                self._cancel.add(job_id)
            db.update_job(self.db_path, job_id,
                          {"status": "cancelling", "cancel_requested": 1})
            self._kill_current(job_id)
            return
        # status ∈ {running 已被置 cancelling, 终态}：多播取消请求视为幂等，忽略
        return

    def _kill_current(self, job_id: int) -> None:
        """登记取消并终止当前运行 Job 的进程树。若进程尚未注册，由看门狗兜底强杀。"""
        with self._lock:
            self._cancel_pending.add(job_id)
            proc = self._proc
        if proc is not None:
            kill_proc_tree(proc)

    def retry(self, job: dict) -> int:
        """重试：创建新的 Job 实例（新 id），严格复用原 Job 的 PlanRun 配置快照。
        保留原 Job 与报告；新 Job 记录 retry_of（原始 job id）与 attempt（原+1）。"""
        from ..domain import Job
        if job["status"] not in TERMINAL:
            raise ValueError("只有终态 Job 可重试")
        nj = Job(
            plan_run_id=job["plan_run_id"], plan_id=job["plan_id"], engine=job["engine"],
            name=job.get("name", "job"),
            environment_id=job["environment_id"], executor_id=job["executor_id"],
            timeout_sec=job.get("timeout_sec", 300),
            requires_exclusive=bool(job.get("requires_exclusive", 0)),
            params=job.get("params") or {}, status="queued",
            config_snapshot=job.get("config_snapshot"),
            parent_job_id=job["id"],
            retry_of=job.get("retry_of") or job["id"],
            attempt=(job.get("attempt") or 1) + 1,
        )
        return db.insert_job(self.db_path, nj)

    def _is_cancelled(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self._cancel

    # ---------- 执行单个 Job ----------
    def _process(self, job: dict) -> None:
        job_id = job["id"]
        # Runner 只使用后端解析后的受控配置，绝不信任前端/DB 中的任意路径或命令。
        env = resolve_runtime_env(db.get_environment(self.db_path, job["environment_id"]) or {})
        executor = resolve_runtime_executor(db.get_executor(self.db_path, job["executor_id"]) or {})
        runner = get_runner(job["engine"] or "pytest")

        # UI/Unity 类任务先抢执行机独占锁；抢不到则保持 queued，绝不并发启动
        lock_id = None
        if job.get("requires_exclusive", 0):
            lock_id = db.acquire_executor_lock(self.db_path, executor.get("id", 0), job_id)
            if not lock_id:
                return  # 锁被占用 → 保持 queued，留待下一轮调度

        try:
            self._run_job(job, env, executor, runner)
        finally:
            # 任意路径（成功/失败/取消/超时/异常）都必须释放独占锁，绝不泄漏
            if lock_id:
                db.release_executor_lock(self.db_path, lock_id)

    def _run_job(self, job: dict, env: dict, executor: dict, runner) -> None:
        job_id = job["id"]
        # 竞态守卫：拉起前若已被取消/跳过，直接放弃（request_cancel 已把 queued 置终态）
        if db.get_job(self.db_path, job_id).get("status") != "queued":
            return

        log_dir = os.path.join(DATA_DIR, "logs")
        report_dir = os.path.join(DATA_DIR, "reports")
        os.makedirs(log_dir, exist_ok=True)
        os.makedirs(report_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"job_{job_id}.log")
        rdir = os.path.join(report_dir, f"run_{job['plan_run_id']}", f"job_{job_id}")
        os.makedirs(rdir, exist_ok=True)
        junit_path = os.path.join(rdir, "junit.xml")

        start_wall = now_ts()
        db.set_job_running(self.db_path, job_id, log_path, junit_path)
        self._logs[job_id] = []
        fh = open(log_path, "a", encoding="utf-8")

        def log_sink(line: str) -> None:
            buf = self._logs.setdefault(job_id, [])
            if len(buf) >= MAX_LOG_LINES:
                buf.pop(0)
            buf.append(line)
            fh.write(line)
            fh.flush()

        def register_proc(proc: subprocess.Popen) -> None:
            with self._lock:
                self._proc = proc

        started = time.monotonic()
        self._job_id = job_id
        self._job_started = started
        self._job_timeout = float(job.get("timeout_sec", 300))
        self._timedout = False

        try:
            pre_errors = self._config_errors(job, executor)
            if pre_errors:
                # 受控配置不满足：诚实失败并给出可行动提示，绝不用模拟成功掩盖配置问题
                log_sink("\n[config] " + "；".join(pre_errors) + "\n")
                result = RunnerResult(status="failed", exit_code=-1,
                                      error="配置不可用: " + "；".join(pre_errors))
            else:
                result = runner.run(job, env, executor, junit_path, log_sink,
                                    lambda: self._is_cancelled(job_id), register_proc)
        except Exception as e:  # noqa: BLE001
            log_sink(f"\n[runner-error] {type(e).__name__}: {e}\n")
            result = RunnerResult(status="failed", exit_code=-1, error=str(e))
        finally:
            fh.close()
            with self._lock:
                self._proc = None
                self._job_id = None
                self._cancel_pending.discard(job_id)

        # 终态裁决：超时 / 取消 / Runner异常 三者互斥区分。
        #   看门狗超时置 _timedout => timedout；请求取消置 cancelling => cancelled；
        #   其余按 Runner 结果（failed / success）；Runner 异常由上面归为 failed。
        if self._timedout:
            status = STATUS_TIMEDOUT
            result.error = result.error or "run hit timeout (watchdog)"
        elif db.get_job(self.db_path, job_id).get("status") == STATUS_CANCELLING:
            status = STATUS_CANCELLED
            result.error = result.error or "cancelled by user"
        else:
            status = result.status if result.status in TERMINAL else "failed"

        db.set_job_terminal(self.db_path, job_id, status, result.exit_code, result.error)

        report = build_report(
            plan_run_id=job["plan_run_id"], job_id=job_id, engine=job["engine"] or "pytest",
            status=status, started_at=start_wall, ended_at=now_ts(),
            environment_snapshot=self._env_snapshot(job, env),
            summary=result.summary, metrics=result.metrics,
            artifacts=result.artifacts, engine_data=result.engine_data,
            engine_data_schema=result.engine_data_schema,
        )
        errors = validate_report(report)
        if errors:
            db.update_job(self.db_path, job_id,
                          {"error": "report 校验失败: " + "; ".join(errors)})
        rdir, report_path = write_report(report_dir, job["plan_run_id"], job_id, report)
        rel = os.path.relpath(report_path, DATA_DIR)
        db.save_report(self.db_path, job_id, job["plan_run_id"], rel, report)
        db.update_job(self.db_path, job_id, {"report_path": rel, "junit_path": junit_path})
        # 报告中心读模型：派生可比性元数据入索引（不触碰事实源；失败不阻断归档）
        try:
            self._index_report(job, env, report)
        except Exception as e:  # noqa: BLE001
            self._logs.setdefault(job_id, []).append(
                f"[report-center] 索引派生失败(报告仍已归档): {e}\n")

        # 失败即停：本 Job 失败且计划 fail_fast => 后继 queued Job 置 skipped（不覆盖历史）
        if status == "failed":
            plan = db.get_plan(self.db_path, job["plan_id"]) or {}
            if plan.get("fail_fast", 1):
                db.cancel_remaining_in_run(self.db_path, job["plan_run_id"])

        with self._lock:
            self._cancel.discard(job_id)
        self._finalize_run(job["plan_run_id"])

    def _index_report(self, job: dict, env: dict, report: dict) -> None:
        """把已归档的 report 派生为报告中心索引行（读模型）。兼容旧/缺字段。
        环境身份优先取 Job 固化快照、计划名优先取 PlanRun 计划快照，
        保证后续编辑配置实体/计划定义不会改变历史比较结论。"""
        from ..report_center import build_index_row
        from ..config_center import plan_snapshot_name
        run = db.get_run(self.db_path, job.get("plan_run_id")) or {}
        snap_name = plan_snapshot_name(run.get("plan_snapshot"))
        plan = {"name": snap_name} if snap_name else (db.get_plan(self.db_path, job.get("plan_id")) or {})
        env_for_index = snapshot_env_view(job.get("config_snapshot")) or env
        row = build_index_row(report, job, plan, env_for_index, now_ts())
        db.upsert_report_index(self.db_path, row)

    def _config_errors(self, job: dict, executor: dict) -> list[str]:
        """Runner 启动前的受控配置校验：执行机是否允许该 Runner、解释器是否可用。"""
        errors: list[str] = []
        engine = job.get("engine") or "pytest"
        allowed = executor.get("allowed_runners") or []
        if allowed and engine not in allowed:
            errors.append(f"执行机 {executor.get('name') or '-'} 不允许运行 {engine}"
                          f"（允许：{', '.join(allowed)}）；请在 Executor 中调整允许的 Runner")
        if not executor.get("python_executable"):
            errors.append("执行机解释器不可用（受控引用不可解析且无 legacy 路径）；"
                          "请在 Executor 选择可用的解释器引用并做预检")
        return errors

    def _env_snapshot(self, job: dict, env: dict) -> dict:
        """report.json 的环境快照：固化 Job 创建时的脱敏配置快照（不可变）。
        旧记录无快照时，回退为既有最小环境标识（保持历史报告兼容）。"""
        snap = job.get("config_snapshot")
        if not snap:
            ex = db.get_executor(self.db_path, job.get("executor_id")) or {}
            snap = build_config_snapshot(env, ex, None)
        return {
            "env_id": (snap.get("environment") or {}).get("name") or env.get("name", "-"),
            "host": (snap.get("environment") or {}).get("target")
                    or f"{env.get('host', '-')}:{env.get('port', '-')}",
            "config_snapshot": snap,
        }

    def _finalize_run(self, run_id: int) -> None:
        if db.run_has_live_jobs(self.db_path, run_id):
            return
        jobs = db.list_jobs_by_run(self.db_path, run_id)
        # 折叠重试链，仅用最新一次尝试参与聚合（原失败+重试成功 => success）
        run_status = aggregate_run_status(latest_attempt_statuses(jobs))
        db.update_run_status(self.db_path, run_id, run_status, ended=True)

    # ---------- 看门狗：超时强杀当前子进程 ----------
    def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                proc = self._proc
                job_id = self._job_id
                started = self._job_started
                timeout = self._job_timeout
            # 兜底：进程注册前收到取消请求，待 _proc 就绪后立即强杀
            if proc is not None and job_id in self._cancel_pending:
                try:
                    kill_proc_tree(proc)
                except Exception:  # noqa: BLE001
                    pass
            if proc is not None and timeout and (time.monotonic() - started) > timeout:
                try:
                    kill_proc_tree(proc)
                except Exception:  # noqa: BLE001
                    pass
                self._timedout = True
            time.sleep(POLL)

    # ---------- 日志 ----------
    def get_logs(self, job_id: int, tail: int = 400) -> list[str]:
        lines = self._logs.get(job_id) or []
        if not lines:
            job = db.get_job(self.db_path, job_id)
            if job and job.get("log_path") and os.path.exists(job["log_path"]):
                with open(job["log_path"], "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
        return lines[-tail:]


def now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")