"""报告中心服务：派生可比性元数据、构建查询索引、严格对比兼容性校验、引擎指标差值/趋势。

原则：
- report.json 是唯一事实源，本模块只做只读派生，绝不改写事实源/历史产物。
- 指纹必须脱敏：不含 Token、账号、明文路径或敏感配置；只保留受控键的稳定哈希。
- 仅同 engine、同 engine_data_schema 主版本、同场景/选择器、同环境、关键输入可比才有效。
- 不生成跨引擎总分；fixture 与真实资产识别报告绝不混为有效性能结论。
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional

from . import db
from .config_center import plan_snapshot_name, snapshot_env_view


def _h(*parts) -> str:
    """稳定脱敏指纹：对受控键规范化后做 sha256 前 12 位，不落任何敏感原文。"""
    norm = "|".join(str(p) for p in parts if p not in (None, ""))
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _safe_basename(v: Any) -> str:
    """去敏：路径/URL 只保留最后一段，避免写入明文机器路径或完整 host 细节。"""
    s = str(v or "")
    s = s.replace("\\", "/")
    return s.split("/")[-1]


def _first(d: dict, *keys, default=None):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return default


# ---------- 引擎参数快照（从 jobs.params 派生，脱敏） ----------
def engine_compare_key(job: dict, env: dict, engine_data_schema: str) -> dict:
    """把 Job 的可比输入归纳为稳定、脱敏的比较键。

    - pytest : 测试选择器 = 显式 args 里的 .py/.:: 项 + cwd 尾段
    - locust : 场景键 + host 环境  + users/spawn_rate/run_time
    - matcheval : dataset/ground_truth 指纹 + algorithm 排序 + threshold
    其余键不回落到 compare_key。
    """
    params = job.get("params") or {}
    engine = job.get("engine") or "pytest"
    env_name = (env or {}).get("name") or "-"
    env_host = f"{(env or {}).get('host','-')}:{(env or {}).get('port','-')}"

    key: dict[str, Any] = {"engine": engine, "env": env_name,
                           "schema_main": _main_version(engine_data_schema)}
    if engine == "pytest":
        args = params.get("args") or []
        selectors = [a for a in args if (str(a).endswith(".py") or "::" in str(a))]
        key["selector"] = selectors or []
        key["selector_fp"] = _h(sorted(selectors)) if selectors else None
        key["cwd_tail"] = _safe_basename(params.get("cwd") or "")
    elif engine == "locust":
        key["scenario"] = str(params.get("locustfile") or "http-fixture")
        key["host"] = str(env_host)
        key["users"] = int(params.get("users") or 0)
        key["spawn_rate"] = int(params.get("spawn_rate") or 0)
        key["run_time"] = str(params.get("run_time") or "")
        key["input_fp"] = _h(key["scenario"], params.get("users"),
                             params.get("spawn_rate"), params.get("run_time"))
    elif engine == "matcheval":
        alg = params.get("algorithm") or []
        if isinstance(alg, str):
            alg = [x.strip() for x in str(alg).split(",") if x.strip()]
        key["dataset"] = str(params.get("dataset") or "")
        key["algorithm"] = sorted(str(a) for a in alg)
        key["threshold"] = _num(params.get("threshold"))
        # ground_truth 指纹不落原始路径，仅存稳定哈希
        key["gt_fp"] = _h(key["dataset"], key["algorithm"], key["threshold"])
        key["input_fp"] = _h(*key["algorithm"])

    sorted_key = json.dumps(key, sort_keys=True, ensure_ascii=False)
    key["compare_fp"] = _h(sorted_key)
    return key


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _main_version(schema: Optional[str]) -> str:
    # "locust/1.0" -> "locust/1"；未知 schema 视为 generic 以避免误判不可比
    s = str(schema or "")
    if "/" in s:
        ms, vs = s.split("/", 1)
        major = vs.split(".")[0]
        return f"{ms}/{major}"
    return s or "generic"


# ---------- 供调度器落库的派生行 ----------
def build_index_row(report: dict, job: dict, plan: Optional[dict],
                    env: dict, created_at: str) -> dict:
    """从已归档 report.json + job + plan + env 派生一行 report_index，兼容缺失字段。"""
    status = report.get("status") or job.get("status") or "-"
    started = report.get("started_at")
    ended = report.get("ended_at")
    duration_ms = _num(metrics_of(report).get("duration_ms")) if metrics_of(report).get("duration_ms") is not None \
        else _num(report.get("summary", {}).get("duration_ms"))

    schema = report.get("engine_data_schema") or "generic/1.0"
    ck = engine_compare_key(job, env, schema)
    schema_main = ck.pop("schema_main")

    row = {
        "job_id": job.get("id"),
        "plan_id": job.get("plan_id"),
        "plan_run_id": job.get("plan_run_id"),
        "plan_name": (plan or {}).get("name"),
        "engine": report.get("engine") or job.get("engine") or "pytest",
        "engine_data_schema": schema,
        "schema_main_version": schema_main,
        "status": status,
        "env_id": (env or {}).get("id"),
        "env_name": ck.pop("env"),
        "env_host": ck.get("host") if "host" in ck else _safe_basename((env or {}).get("host")),
        "started_at": started,
        "ended_at": ended,
        "duration_ms": duration_ms,
        "comparator": _comparator(ck, schema_main),
        "compare_key": json.dumps(ck, ensure_ascii=False),
        "metrics": json.dumps(metrics_of(report), ensure_ascii=False),
        "summary": json.dumps(report.get("summary") or {}, ensure_ascii=False),
        "metadata_incomplete": 0,
        "extra": json.dumps({
            "job_name": job.get("name"),
            "report_schema_version": report.get("schema_version"),
        }, ensure_ascii=False),
        "created_at": created_at,
    }
    # 缺少关键可比性输入（如旧记录 args 为空）→ 标记信息不完整但不伪造
    row["metadata_incomplete"] = 1 if _is_incomplete(ck) else 0
    return row


def _is_incomplete(ck: dict) -> bool:
    eng = ck.get("engine")
    if eng == "pytest":
        return not ck.get("selector") and not ck.get("cwd_tail")
    if eng == "locust":
        return not ck.get("scenario")
    if eng == "matcheval":
        return not ck.get("dataset")
    return False


def _comparator(ck: dict, schema_main: Optional[str] = "") -> str:
    """可比性分组稳定串：反映"同一类输入"，并限定同 schema 主版本（趋势不跨协议版本）。"""
    eng = ck.get("engine")
    if eng == "pytest":
        return _h("pytest", schema_main, ck.get("selector"), ck.get("cwd_tail"))
    if eng == "locust":
        # 并发参数是趋势的关注点，保留在 comparator 内以便"同配置"分组
        return _h("locust", schema_main, ck.get("scenario"), ck.get("host"),
                  ck.get("users"), ck.get("spawn_rate"), ck.get("run_time"))
    if eng == "matcheval":
        return _h("matcheval", schema_main, ck.get("dataset"), ck.get("algorithm"),
                  ck.get("threshold"))
    return _h(eng or "?", "generic")


def metrics_of(report: dict) -> dict:
    return report.get("metrics") or {}


# ---------- 严格对比兼容性校验 ----------
def compare_compatibility(a: dict, b: dict) -> dict:
    """a=基线, b=候选。返回 {compatible, reasons[], summary}。仅提示差异，不做跨引擎总分。"""
    reasons: list[str] = []
    ack, bck = a.get("compare_key") or {}, b.get("compare_key") or {}

    if (a.get("engine") or "") != (b.get("engine") or ""):
        return {"compatible": False,
                "reasons": [f"引擎不同：{a.get('engine')} vs {b.get('engine')}"],
                "summary": "引擎不同，禁止比较"}
    if (ack.get("schema_main") or a.get("schema_main_version")) \
            != (bck.get("schema_main") or b.get("schema_main_version")):
        return {"compatible": False,
                "reasons": [f"engine_data_schema 主版本不同：{a.get('schema_main_version')} vs "
                            f"{b.get('schema_main_version')}"],
                "summary": "报告协议主版本不同，禁止比较"}
    if a.get("env_name") != b.get("env_name"):
        return {"compatible": False,
                "reasons": [f"环境不同：{a.get('env_name')} vs {b.get('env_name')}"],
                "summary": "被测环境不同，禁止比较"}
    if a.get("metadata_incomplete") or b.get("metadata_incomplete"):
        return {"compatible": False,
                "reasons": ["旧报告元数据不足（缺少可比性输入，历史记录信息不完整）"],
                "summary": "历史记录信息不完整"}

    eng = a.get("engine")
    if eng == "pytest":
        if _norm_sel(ack) != _norm_sel(bck):
            reasons.append(f"测试选择器不同：{_norm_sel(ack)} vs {_norm_sel(bck)}")
        elif ack.get("cwd_tail") != bck.get("cwd_tail"):
            reasons.append("工作目录不同")
    elif eng == "locust":
        if ack.get("scenario") != bck.get("scenario"):
            reasons.append(f"场景不同：{ack.get('scenario')} vs {bck.get('scenario')}")
        if ack.get("host") != bck.get("host"):
            reasons.append(f"目标 host 不同：{ack.get('host')} vs {bck.get('host')}")
        if ack.get("users") != bck.get("users"):
            reasons.append(f"并发(users)不同：{ack.get('users')} vs {bck.get('users')}")
        if ack.get("spawn_rate") != bck.get("spawn_rate"):
            reasons.append(f"递增率(spawn_rate)不同：{ack.get('spawn_rate')} vs {bck.get('spawn_rate')}")
        if ack.get("run_time") != bck.get("run_time"):
            reasons.append(f"压测时长(run_time)不同：{ack.get('run_time')} vs {bck.get('run_time')}")
    elif eng == "matcheval":
        if ack.get("dataset") != bck.get("dataset"):
            reasons.append(f"样本集(dataset)不同：{ack.get('dataset')} vs {bck.get('dataset')}")
        if ack.get("algorithm") != bck.get("algorithm"):
            reasons.append(f"算法集不同：{ack.get('algorithm')} vs {bck.get('algorithm')}")
        if ack.get("threshold") != bck.get("threshold"):
            reasons.append(f"阈值不同：{ack.get('threshold')} vs {bck.get('threshold')}")

    compatible = not reasons
    return {"compatible": compatible,
            "reasons": reasons if not compatible else [],
            "summary": "有效对比" if compatible else "输入不可比；原因如下"}


def _norm_sel(ck: dict):
    s = ck.get("selector")
    return sorted(s) if isinstance(s, list) else str(s or "")


# ---------- 引擎专项指标（统一方向与格式化） ----------
# metric -> (label, 单位, "higher_better" | "lower_better" | "neutral")
_ENGINE_METRICS: dict[str, list[tuple]] = {
    "pytest": [
        ("passed", "通过数", "", "higher_better"),
        ("failed", "失败数", "", "lower_better"),
        ("duration_ms", "耗时", "ms", "lower_better"),
    ],
    "locust": [
        ("rps", "RPS", "", "higher_better"),
        ("requests", "请求数", "", "higher_better"),
        ("failure_rate", "错误率", "%", "lower_better"),
        ("avg_ms", "平均时延", "ms", "lower_better"),
        ("p95_ms", "P95", "ms", "lower_better"),
        ("p99_ms", "P99", "ms", "lower_better"),
    ],
    "matcheval": [
        ("precision", "Precision", "%", "higher_better"),
        ("recall", "Recall", "%", "higher_better"),
        ("f1", "F1", "%", "higher_better"),
        ("tp", "TP", "", "higher_better"),
        ("fp", "FP", "", "lower_better"),
        ("fn", "FN", "", "lower_better"),
    ],
}


def engine_metrics(engine: str) -> list[dict]:
    out = []
    for key, label, unit, direction in _ENGINE_METRICS.get(engine, []):
        out.append({"key": key, "label": label, "unit": unit, "direction": direction})
    return out


def metric_deltas(a: dict, b: dict) -> dict:
    """a=基线, b=候选 -> 各指标 {base, candidate, delta, formatted, dir_label, color}。"""
    engine = b.get("engine")
    am, bm = a.get("metrics") or {}, b.get("metrics") or {}
    items = []
    for meta in engine_metrics(engine):
        key = meta["key"]
        bv = bm.get(key)
        av = amc = am.get(key)
        if bv is None and av is None:
            continue
        bv = _num(bv)
        av = _num(av)
        if bv is None or av is None:
            items.append({**meta, "base": av, "candidate": bv, "delta": None,
                          "label_fmt": _fmt_single(meta, av) + " → " + _fmt_single(meta, bv),
                          "missing": True})
            continue
        delta = bv - av
        unit = meta["unit"]
        if unit == "%":
            bvf, avf, deltav = bv * 100, av * 100, delta * 100
        else:
            bvf, avf, deltav = bv, av, delta
        prefix = "+" if deltav > 0 else ("-" if deltav < 0 else "")
        if meta["direction"] == "higher_better":
            color = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            dir_label = "更好" if delta > 0 else ("更差" if delta < 0 else "持平")
        elif meta["direction"] == "lower_better":
            color = "down" if delta > 0 else ("up" if delta < 0 else "flat")
            dir_label = "更差" if delta > 0 else ("更好" if delta < 0 else "持平")
        else:
            color = "flat"; dir_label = ""
        items.append({**meta,
                      "base": round(av, 4), "candidate": round(bv, 4),
                      "delta": round(deltav, 4),
                      "formatted": f"{prefix}{_trim(deltav)}{unit}",
                      "dir_label": dir_label, "color": color})
    return {"engine": engine, "items": items}


def _trim(x: float) -> str:
    return f"{x:g}" if abs(x) >= 1 or x == 0 else f"{x:.3f}".rstrip("0").rstrip(".")


def _fmt_single(meta: dict, v) -> str:
    if v is None:
        return "-"
    unit = meta["unit"]
    return f"{v * 100:.1f}{unit}" if unit == "%" else f"{_trim(v)}{unit}"


def build_trend(rows: list[dict]) -> dict:
    """同一 comparator 历史序列 -> 趋势点列。返回 {engine, points:[{job_id, started_at, metrics:{...}}]}。"""
    engine = rows[0].get("engine") if rows else None
    wanted = [m["key"] for m in engine_metrics(engine)]
    points = []
    for r in rows:
        m = r.get("metrics") or {}
        mm = {k: (m[k] if m.get(k) is not None else None) for k in wanted}
        points.append({"job_id": r.get("job_id"), "plan_run_id": r.get("plan_run_id"),
                       "started_at": r.get("started_at"), "metrics": mm})
    return {"engine": engine, "points": points}


# ---------- 迁移：从既有 reports/jobs 重建索引（兼容旧记录，不触碰事实源） ----------

def rebuild_report_index(db_path: str) -> dict:
    """全量重建 report_index。旧报告缺少可比性输入时标记 metadata_incomplete，
    绝不 mock/fill 缺失值。返回统计。"""
    conn = db.get_conn(db_path)
    rows = conn.execute(
        "SELECT r.job_id AS rid, r.report_json, r.created_at AS rcreated, "
        "pr.plan_snapshot AS run_plan_snapshot, j.* "
        "FROM reports r LEFT JOIN jobs j ON j.id=r.job_id "
        "LEFT JOIN plan_runs pr ON pr.id=j.plan_run_id").fetchall()
    conn.close()
    rebuilt = incomplete = skipped = 0
    for r in rows:
        job = dict(r)
        if not job.get("id"):
            skipped += 1
            continue
        # jobs.params 在库中是 JSON 字符串；build_index_row 需要已解析的 dict
        if isinstance(job.get("params"), str):
            try:
                job["params"] = json.loads(job["params"])
            except Exception:  # noqa: BLE001 —— 非法 params 视作无可比输入
                job["params"] = {}
        try:
            rep = json.loads(r["report_json"])
            # 环境身份优先取 Job 固化快照：配置实体后续编辑不得改变历史比较结论
            env = snapshot_env_view(job.get("config_snapshot")) \
                or db.get_environment(db_path, job.get("environment_id")) or {}
            # 计划名优先取执行时的 PlanRun 计划快照：后续改名不漂移历史
            snap_name = plan_snapshot_name(job.get("run_plan_snapshot"))
            plan = {"name": snap_name} if snap_name else (db.get_plan(db_path, job.get("plan_id")) or {})
            row = build_index_row(rep, job, plan, env, r["rcreated"] or "")
        except Exception:  # noqa: BLE001 —— 单条失败不阻断整体迁移
            skipped += 1
            continue
        rebuilt += 1
        incomplete += int(row.get("metadata_incomplete", 0))
        db.upsert_report_index(db_path, row)
    return {"rebuilt": rebuilt, "incomplete": incomplete, "skipped": skipped}