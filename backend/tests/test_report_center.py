"""单元测试：报告中心读模型 + 严格对比兼容性 + 引擎指标方向。
运行：python -m pytest backend/tests/test_report_center.py -q（配合既有单测目录）
本文件只测纯函数，不依赖服务。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.report_center as rc
from app.report_center import (engine_compare_key, _main_version,
                               compare_compatibility, metric_deltas, build_trend,
                               engine_metrics, build_index_row)


def _env(name="local"):
    return {"id": 1, "name": name, "host": "127.0.0.1", "port": 8000}


def _job(engine, params, env_id=1):
    return {"id": 1, "plan_id": 1, "plan_run_id": 1, "engine": engine,
            "name": "j", "environment_id": env_id, "params": params}


# ---------- 可比性元数据 ----------
def test_pytest_selector_fingerprint():
    k1 = engine_compare_key(_job("pytest", {"args": ["a/test_x.py", "-q"]}), _env(), "pytest/1.0")
    k2 = engine_compare_key(_job("pytest", {"args": ["a/test_x.py", "-q"]}), _env(), "pytest/1.0")
    k3 = engine_compare_key(_job("pytest", {"args": ["b/test_y.py", "-q"]}), _env(), "pytest/1.0")
    assert k1["selector"] == ["a/test_x.py"]
    assert k1["selector_fp"] == k2["selector_fp"]
    assert k1["selector_fp"] != k3["selector_fp"]


def test_locust_compare_key_no_sensitive_path():
    k = engine_compare_key(
        _job("locust", {"locustfile": "http-fixture", "users": 4, "spawn_rate": 2, "run_time": "12s"}),
        _env(), "locust/1.0")
    assert k["scenario"] == "http-fixture"   # 只存受控键，不落绝对路径
    assert k["users"] == 4 and k["spawn_rate"] == 2 and k["run_time"] == "12s"
    raw = str(k)
    assert "C:" not in raw and "\\" not in raw and "token" not in raw.lower()


def test_matcheval_compare_key():
    k = engine_compare_key(_job("matcheval", {"dataset": "fixture-small",
                                              "algorithm": ["tpl", "mstpl"], "threshold": 0.8}),
                           _env(), "matcheval/v1")
    assert k["dataset"] == "fixture-small"
    assert sorted(k["algorithm"]) == ["mstpl", "tpl"]
    assert k["threshold"] == 0.8
    assert k["gt_fp"]  # 脱敏指纹存在，且不含数据集明文路径


def test_schema_main_version():
    assert _main_version("locust/1.0") == "locust/1"
    assert _main_version("matcheval/v1") == "matcheval/v1"
    assert _main_version("") == "generic"


# ---------- 兼容性校验 ----------
def _idx(engine, sch, env="local", incomplete=0, extra=None):
    """构造 report_index 行的简化形态（用于 compare_compatibility）。

    与真实链路一致：engine_compare_key 接收带 engine+params 的完整 job 字典，
    而非裸参数 dict（否则 job.get("engine") 为 None 会回退成 pytest）。
    """
    job = {"id": 1, "plan_id": 1, "engine": engine, "params": engine_params(engine, extra)}
    ck = engine_compare_key(job, _env(env), sch)
    ck = {k: v for k, v in ck.items() if k in ("engine", "schema_main", "scenario",
                                               "host", "users", "spawn_rate", "run_time",
                                               "dataset", "algorithm", "threshold",
                                               "selector", "cwd_tail", "gt_fp", "input_fp")}
    return {"engine": engine, "env_name": env, "schema_main_version": _main_version(sch),
            "metadata_incomplete": incomplete, "compare_key": ck}


def engine_params(engine, extra=None):
    p = (extra or {}).copy()
    if engine == "pytest":
        p.setdefault("args", ["demo/test_platform_demo.py"])
    elif engine == "locust":
        p.setdefault("locustfile", "http-fixture"); p.setdefault("users", 4)
        p.setdefault("spawn_rate", 2); p.setdefault("run_time", "12s")
    elif engine == "matcheval":
        p.setdefault("dataset", "fixture-small"); p.setdefault("algorithm", ["tpl"]); p.setdefault("threshold", 0.8)
    return {**p, "base_cwd": ""}


def test_compatible_same():
    a, b = _idx("locust", "locust/1.0"), _idx("locust", "locust/1.0")
    r = compare_compatibility(a, b)
    assert r["compatible"] is True


def test_env_diff_rejected():
    a, b = _idx("locust", "locust/1.0", "local"), _idx("locust", "locust/1.0", "staging")
    r = compare_compatibility(a, b)
    assert r["compatible"] is False
    assert any("环境不同" in x for x in r["reasons"])


def test_locust_users_diff_rejected():
    a = _idx("locust", "locust/1.0")
    b = _idx("locust", "locust/1.0", extra={"users": 8})
    r = compare_compatibility(a, b)
    assert r["compatible"] is False
    assert any("users" in x for x in r["reasons"])


def test_matcheval_dataset_not_mixed():
    a = _idx("matcheval", "matcheval/v1")
    b = _idx("matcheval", "matcheval/v1", extra={"dataset": "real-assets"})
    r = compare_compatibility(a, b)
    assert r["compatible"] is False
    assert any("dataset" in x for x in r["reasons"])


def test_engine_diff_no_cross_total():
    r = compare_compatibility(_idx("pytest", "pytest/1.0"), _idx("locust", "locust/1.0"))
    assert r["compatible"] is False


def test_old_metadata_incomplete():
    a = _idx("pytest", "pytest/1.0", incomplete=1)
    b = _idx("pytest", "pytest/1.0")
    r = compare_compatibility(a, b)
    assert r["compatible"] is False
    assert any("不完整" in x for x in r["reasons"])


# ---------- 引擎指标差值方向 ----------
def test_metric_deltas_direction():
    a = {"engine": "locust",
         "metrics": {"rps": 10.0, "failure_rate": 0.02, "p95_ms": 100.0}}
    b = {"engine": "locust",
         "metrics": {"rps": 12.0, "failure_rate": 0.01, "p95_ms": 90.0}}
    d = {it["key"]: it for it in metric_deltas(a, b)["items"]}
    # RPS 上升 → 更好(绿 up)
    assert d["rps"]["delta"] == 2.0 and d["rps"]["color"] == "up"
    assert d["rps"]["dir_label"] == "更好"
    # 错误率下降 → 更好；单位 %，差值按展示单位(百分点)计 = -0.01*100 = -1.0
    assert d["failure_rate"]["delta"] == -1.0 and d["failure_rate"]["color"] == "up"
    # P95 下降 → 更好
    assert d["p95_ms"]["delta"] == -10.0 and d["p95_ms"]["color"] == "up"


def test_metric_deltas_direction_down():
    a = {"engine": "matcheval", "metrics": {"precision": 0.8, "fp": 2}}
    b = {"engine": "matcheval", "metrics": {"precision": 0.6, "fp": 5}}
    d = {it["key"]: it for it in metric_deltas(a, b)["items"]}
    # precision 下降 → 更差(红 down)
    assert d["precision"]["color"] == "down" and d["precision"]["dir_label"] == "更差"
    # FP 上升 → 更差(红 down)
    assert d["fp"]["color"] == "down"


def test_build_index_row_missing_marked():
    row = build_index_row(
        {"engine": "pytest", "status": "success", "metrics": {"passed": 1},
         "summary": {"duration_ms": 10}, "schema_version": "1.0"},
        _job("pytest", {"args": []}), None, _env(), "t")
    assert row["metadata_incomplete"] == 1


def test_build_trend_collects_keys():
    rows = [
        {"engine": "locust", "job_id": 1, "started_at": "a",
         "metrics": {"rps": 10, "failure_rate": 0.1, "p95_ms": 50}},
        {"engine": "locust", "job_id": 2, "started_at": "b",
         "metrics": {"rps": 11, "failure_rate": 0.05, "p95_ms": 45}},
    ]
    t = build_trend(rows)
    assert t["engine"] == "locust"
    assert len(t["points"]) == 2
    assert set(t["points"][0]["metrics"].keys()) == {"rps", "requests", "failure_rate", "avg_ms", "p95_ms", "p99_ms"}