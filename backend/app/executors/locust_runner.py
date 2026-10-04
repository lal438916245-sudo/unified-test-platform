"""Locust Runner：headless 运行 Locust 压测，收集 HTML/CSV 产物，构建冻结 report.json。

安全约束：Job 参数仅允许白名单内的受控项，命令始终以参数列表交给 subprocess（不 shell=True），
任何人不得通过 Job 参数注入任意命令或任意本地路径。
"""
from __future__ import annotations

import csv
import os
import subprocess
import time
from typing import Callable, Optional

from .base import BaseRunner, RunnerResult, kill_proc_tree
from ..config_center import LOCUST_SCENARIOS, resolve_asset_ref

# 受白名单保护的 locustfile：Job 只能引用受控场景 key，不能填任意本地路径。
# **唯一来源是登记层 config_center.LOCUST_SCENARIOS**：本文件不再持有第二份路径事实（避免双源漂移）。
_SCENARIO_KEYS = tuple(LOCUST_SCENARIOS)   # 受控场景 key 列表；绝对路径由登记层解析


def _locustfile_path(key: str) -> str:
    """受控场景 key -> 绝对路径（仅后端使用，不返回前端、不落日志）。"""
    return resolve_asset_ref("locust_scenario", key)

# 受控参数：仅这些键会被读取；未知键一律忽略（不做 shell 拼接/任意路径）。
_INT_KEYS = ("users", "spawn_rate")
_STR_KEYS = ("locustfile", "run_time")


def _fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class LocustRunner(BaseRunner):
    engine = "locust"

    def run(self, job, env, executor, junit_path, log_sink, is_cancelled, register_proc=None) -> RunnerResult:
        params = job["params"] or {}

        # ---- 参数白名单解析 ----
        scen = str(params.get("locustfile", "http-fixture"))
        if scen not in _SCENARIO_KEYS:
            return RunnerResult(status="failed", exit_code=-1,
                                error=f"非白名单 locustfile 场景: {scen!r}")
        locustfile = _locustfile_path(scen)
        users = max(1, int(params.get("users", 10)))
        spawn_rate = max(1, int(params.get("spawn_rate", 2)))
        run_time = str(params.get("run_time", "20s"))
        csv_full_history = bool(params.get("csv_full_history", True))
        python = executor["python_executable"]
        host = f"http://{env['host']}:{env['port']}"

        # 产物目录：本 Job 的报告子目录（junit_path 位于 reports/run_X/job_Y/junit.xml）
        rdir = os.path.dirname(junit_path)
        os.makedirs(rdir, exist_ok=True)
        html_path = os.path.join(rdir, "locust_report.html")
        csv_prefix = os.path.join(rdir, "locust")

        cmd = [python, "-m", "locust", "-f", locustfile, "--headless",
               "-u", str(users), "-r", str(spawn_rate),
               "--run-time", run_time, "--host", host,
               "--html", html_path, "--csv", csv_prefix]
        if csv_full_history:
            cmd.append("--csv-full-history")
        log_sink(f"$ {_shell(cmd)}\n$ cwd={os.path.dirname(locustfile)}\n")

        proc = subprocess.Popen(
            cmd, cwd=os.path.dirname(locustfile),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
        if register_proc:
            register_proc(proc)

        line = proc.stdout.readline()
        while line:
            if is_cancelled():
                log_sink("\n[cancel] 收到取消请求，终止 locust 进程树\n")
                kill_proc_tree(proc)
                try:
                    proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=6)
                log_sink("[cancel] locust 进程树已回收\n")
                return RunnerResult(status="cancelled", exit_code=130,
                                    error="cancelled by user")
            log_sink(line)
            line = proc.stdout.readline()
        proc.wait()
        return self._finalize(proc.returncode, rdir, host, env)

    # ---------- 结果构建 ----------
    def _finalize(self, exit_code, rdir, host, env) -> RunnerResult:
        stats_path = os.path.join(rdir, "locust_stats.csv")
        history_path = os.path.join(rdir, "locust_stats_history.csv")
        failures_path = os.path.join(rdir, "locust_failures.csv")
        exceptions_path = os.path.join(rdir, "locust_exceptions.csv")
        html_path = os.path.join(rdir, "locust_report.html")

        per_request = self._parse_stats(stats_path)          # 每接口维度
        timeseries = self._parse_history(history_path)
        n_failures = sum(r["failures"] for r in per_request)
        requests = sum(r["requests"] for r in per_request)

        summary = {
            "requests": requests,
            "failures": n_failures,
            "failure_rate": round(n_failures / requests, 6) if requests else 1.0,
            "users": sum(r.get("users", 0) for r in timeseries[-1:]) if timeseries else 0,
            "duration_ms": 0,
        }
        # 加权全局时延
        total_w = sum(r["requests"] for r in per_request) or 1
        metrics = {
            "requests": requests,
            "failures": n_failures,
            "failure_rate": round(n_failures / requests, 6) if requests else 1.0,
            "rps": round(sum(r["rps"] for r in per_request), 2),
            "avg_ms": round(sum(r["requests"] * r["avg_ms"] for r in per_request) / total_w, 2),
            "p50_ms": round(sum(r["requests"] * r["p50_ms"] for r in per_request) / total_w, 2),
            "p95_ms": round(sum(r["requests"] * r["p95_ms"] for r in per_request) / total_w, 2),
            "p99_ms": round(sum(r["requests"] * r["p99_ms"] for r in per_request) / total_w, 2),
        }

        # 诚实归档：进程失败，或多于 0 个请求失败（含被测服务不可达）→ failed
        extra = ""
        if exit_code != 0:
            status = "failed"
            extra = f"(locust exit={exit_code})"
        elif requests > 0 and n_failures > 0:
            status = "failed"
            extra = f"({n_failures}/{requests} 请求失败，failure_rate={metrics['failure_rate']})"
        elif requests == 0:
            status = "failed"
            extra = "(无任何请求采样)"
        else:
            status = "success"
        error = f"locust run failed {extra}".strip() if status == "failed" else None

        artifacts = self._artifacts(rdir, html_path, stats_path, failures_path,
                                    exceptions_path, history_path)
        engine_data = {
            "host": host,
            "request_stats": per_request,
            "timeseries": timeseries,
        }
        return RunnerResult(
            status=status, exit_code=exit_code, summary=summary, metrics=metrics,
            artifacts=artifacts, engine_data=engine_data,
            engine_data_schema="locust/1.0", error=error,
        )

    # ---------- CSV 解析 ----------
    @staticmethod
    def _rows(path: str) -> list[dict]:
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
                return list(csv.DictReader(f))
        except Exception:  # noqa: BLE001
            return []

    def _parse_stats(self, path: str) -> list[dict]:
        """locust_stats.csv：每接口一行；跳过聚合 Total 行。返回受控字段列表。"""
        out = []
        for row in self._rows(path):
            name = row.get("Name", "").strip()
            if not name or "Aggregated" in name:
                continue
            reqs = int(float(row.get("Request Count") or 0))
            fails = int(float(row.get("Failure Count") or 0))
            out.append({
                "type": row.get("Type", "-"),
                "name": name,
                "requests": reqs,
                "failures": fails,
                "rps": round(_fnum(row.get("Requests/s")), 3),
                "avg_ms": round(_fnum(row.get("Average Response Time")), 2),
                "p50_ms": round(_fnum(row.get("50%")) if _fnum(row.get("50%")) else _fnum(row.get("Median")), 2),
                "p95_ms": round(_fnum(row.get("95%")), 2),
                "p99_ms": round(_fnum(row.get("99%")), 2),
            })
        return out

    def _parse_history(self, path: str) -> list[dict]:
        """locust_stats_history.csv：按时间聚合的时序。汇总为秒级点列（吞吐/错误率/P95/用户）。"""
        by_ts: dict[str, dict] = {}
        for row in self._rows(path):
            ts = row.get("Timestamp", "").strip()
            if "Aggregated" in (row.get("Name") or ""):
                pass
            cur = by_ts.setdefault(ts, {"ts": ts, "req": 0, "fail": 0,
                                        "rps": 0.0, "p95": 0.0, "users": 0})
            cur["req"] += int(float(row.get("Requests") or 0))
            cur["fail"] += int(float(row.get("Failures") or 0))
            cur["rps"] = max(cur["rps"], _fnum(row.get("Current RPS") or row.get("Requests/s")))
            cur["p95"] = max(cur["p95"], _fnum(row.get("95%")))
            cur["users"] = max(cur["users"], _fnum(row.get("User Count")))
        series = []
        for k in sorted(by_ts):
            c = by_ts[k]
            series.append({
                "ts": c["ts"],
                "requests": c["req"],
                "failure_rate": round(c["fail"] / c["req"], 6) if c["req"] else 0.0,
                "rps": round(c["rps"], 2),
                "p95_ms": round(c["p95"], 2),
                "users": int(c["users"]),
            })
        return series

    # ---------- 产物 ----------
    def _artifacts(self, rdir, html, stats, failures, exceptions, history) -> list[dict]:
        arts = []
        entries = [
            ("locust_report.html", "text/html", html),
            ("locust_stats.csv", "text/csv", stats),
            ("locust_failures.csv", "text/csv", failures),
            ("locust_exceptions.csv", "text/csv", exceptions),
            ("locust_stats_history.csv", "text/csv", history),
        ]
        for name, mime, path in entries:
            if path and os.path.exists(path):
                arts.append({"name": name, "type": mime, "path": os.path.basename(path),
                             "size": os.path.getsize(path)})
        return arts


def _shell(parts: list[str]) -> str:
    return " ".join(f'"{p}"' if " " in p else p for p in parts)