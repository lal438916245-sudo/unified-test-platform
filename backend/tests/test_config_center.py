"""单元测试：配置中心（受控引用 / 脱敏 / 指纹 / 输入校验 / 快照不可变）。

运行：python -m pytest backend/tests/test_config_center.py -q
只测纯函数与隔离 SQLite，不依赖运行中的服务，也不触碰任何真实环境/资产。
"""
import atexit
import gc
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db
from app.config_center import (build_config_snapshot, environment_fingerprint,
                               mask_asset_source, mask_environment, mask_executor,
                               mask_path, secret_status, snapshot_asset_key,
                               snapshot_env_view, validate_asset_input,
                               validate_environment_input, validate_executor_input,
                               validate_plan_refs, validate_step_params)
from app.domain import Job, Plan, PlanStep
from app.report_center import rebuild_report_index


# ---------- 脱敏 ----------
def test_mask_path_never_absolute():
    assert mask_path(r"C:\Anaconda3\python.exe") == "…/python.exe"
    assert mask_path("/usr/local/bin/python3") == "…/python3"
    assert mask_path("") == ""
    assert ":" not in mask_path(r"C:\x\y.exe")
    assert "\\" not in mask_path(r"C:\x\y.exe")


def test_mask_executor_clears_plaintext_paths():
    x = {"id": 1, "name": "e", "python_ref": "anaconda", "cwd_root": "backend-demo",
         "allowed_runners": '["pytest"]', "python_executable": r"C:\secret\py.exe",
         "cwd": r"C:\secret\work"}
    m = mask_executor(x)
    assert m["python_executable"] == "" and m["cwd"] == ""      # 明文路径绝不外泄
    assert m["python_masked"].startswith("…/")
    assert "C:" not in str(m)


def test_mask_environment_never_returns_secret_value():
    m = mask_environment({"id": 1, "name": "e", "secret_ref": "MY_TOKEN",
                          "secret_kind": "env_var"})
    assert m["secret_ref"] == "MY_TOKEN"            # 引用名可展示
    assert "value" not in m and "secret_value" not in m


def test_mask_asset_source_no_absolute_path():
    a = {"id": 1, "name": "s", "kind": "locust", "scenario_ref": "http-fixture"}
    m = mask_asset_source(a)
    assert ":" not in str(m.get("scenario_ref_masked", ""))
    assert m.get("scenario_ref_masked", "").startswith("…/")


# ---------- 密钥：只校验可用性，绝不返回值 ----------
def test_secret_status_env_var(monkeypatch):
    st = secret_status("NOPE_NOT_SET_XYZ", "env_var")
    assert st["configured"] is True and st["available"] is False
    assert "NOPE_NOT_SET_XYZ" in st["detail"] and "未设置" in st["detail"]
    monkeypatch.setenv("CC_TEST_SECRET", "super-secret-value")
    st2 = secret_status("CC_TEST_SECRET", "env_var")
    assert st2["available"] is True
    assert "super-secret-value" not in str(st2)     # 绝不回显实际值


def test_secret_status_external_ref_is_unknown_not_value():
    st = secret_status("vault://prod/game", "external_ref")
    assert st["configured"] is True and st["available"] is None
    assert "prod" in st["detail"]


def test_secret_status_unconfigured():
    st = secret_status("", "")
    assert st["configured"] is False


# ---------- 指纹（非敏感、稳定） ----------
def test_environment_fingerprint_stable_and_non_sensitive():
    e = {"name": "local", "kind": "colyseus", "protocol": "http",
         "host": "localhost", "port": 2567}
    fp = environment_fingerprint(e)
    assert fp == environment_fingerprint(dict(e))
    assert len(fp) == 12
    assert environment_fingerprint({**e, "host": "other"}) != fp


# ---------- Job 配置快照（脱敏 + 不可变） ----------
def test_build_config_snapshot_masked_and_has_fingerprint():
    env = {"id": 1, "name": "local", "kind": "colyseus", "protocol": "http",
           "host": "localhost", "port": 2567, "secret_ref": "GAME_TOKEN",
           "secret_kind": "env_var"}
    ex = {"id": 2, "name": "x", "python_ref": "anaconda", "cwd_root": "backend-demo",
          "allowed_runners": ["pytest"], "max_concurrency": 1}
    asset = {"id": 3, "name": "a", "kind": "locust", "scenario_ref": "http-fixture"}
    snap = build_config_snapshot(env, ex, asset)
    raw = str(snap)
    assert "F:" not in raw and "C:" not in raw            # 无明文绝对路径
    assert snap["environment"]["fingerprint"]
    assert snap["executor"]["python_masked"].startswith("…/")
    assert snap["snapshot_fingerprint"]
    assert snap["asset_source"]["fingerprint"]
    # 快照只存受控 key，不落解释器明文
    assert snap["executor"]["python_ref"] == "anaconda"
    assert snap["asset_source"]["refs"]["scenario_ref"] == "http-fixture"


def test_snapshot_env_view_and_asset_key_roundtrip():
    snap = build_config_snapshot(
        {"id": 1, "name": "local", "host": "localhost", "port": 2567},
        {"id": 2, "name": "x", "python_ref": "anaconda"},
        {"id": 3, "name": "a", "kind": "matcheval", "dataset_ref": "fixture-small"})
    v = snapshot_env_view(snap)
    assert v["name"] == "local" and v["host"] == "localhost"
    assert snapshot_asset_key(snap, "matcheval") == "fixture-small"
    assert snapshot_asset_key(snap, "locust") == ""
    assert snapshot_env_view(None) is None
    assert snapshot_env_view("not-json") is None


# ---------- 输入校验：只接受受控 ID/key ----------
def test_validate_environment_rejects_pathlike_and_bad_values():
    errs = validate_environment_input({"name": "e", "kind": "http",
                                       "host": r"C:\game\server", "port": 2567})
    assert any("host" in e for e in errs)
    assert validate_environment_input({"name": "e", "kind": "nope", "host": "h", "port": 1})
    assert validate_environment_input({"name": "e", "kind": "http", "host": "h", "port": 99999})
    assert validate_environment_input({"name": "e", "kind": "http", "host": "h", "port": 1,
                                       "secret_kind": "env_var", "secret_ref": "bad-name!"})
    assert validate_environment_input({"name": "e", "kind": "offline"}) == []


def test_validate_executor_rejects_non_controlled_refs():
    assert any("python_ref" in e for e in validate_executor_input(
        {"name": "x", "python_ref": r"C:\Anaconda3\python.exe"}))
    assert any("cwd_root" in e for e in validate_executor_input(
        {"name": "x", "cwd_root": r"C:\somewhere"}))
    assert any("allowed_runners" in e for e in validate_executor_input(
        {"name": "x", "allowed_runners": ["nope"]}))
    # 允许留空（未指定），一旦填写必须是受控 key
    assert validate_executor_input({"name": "x", "python_ref": ""}) == []
    assert validate_executor_input({"name": "x", "python_ref": "anaconda",
                                    "cwd_root": "backend-demo",
                                    "allowed_runners": ["pytest", "locust"]}) == []


def test_validate_asset_rejects_non_controlled_refs():
    assert any("dataset_ref" in e for e in validate_asset_input(
        {"name": "a", "kind": "matcheval", "dataset_ref": "real-assets"}))
    assert any("scenario_ref" in e for e in validate_asset_input(
        {"name": "a", "kind": "locust", "scenario_ref": "colyseus-bot"})) is False
    assert any("scenario_ref" in e for e in validate_asset_input(
        {"name": "a", "kind": "locust", "scenario_ref": "http-fixture"})) is False
    assert validate_asset_input({"name": "a", "kind": "locust",
                                 "scenario_ref": "http-fixture"}) == []


def test_validate_step_params_rejects_arbitrary_command_or_path():
    assert any("不接受的参数键" in e for e in validate_step_params(
        "pytest", {"command": "rm -rf /"}))
    assert any("args" in e for e in validate_step_params(
        "pytest", {"args": [r"C:\evil.py"]}))
    assert any("args" in e for e in validate_step_params(
        "pytest", {"args": ["../../etc/passwd"]}))
    assert any("locustfile" in e for e in validate_step_params(
        "locust", {"locustfile": r"C:\x\locustfile.py"}))
    assert any("dataset" in e for e in validate_step_params(
        "matcheval", {"dataset": "real-assets"}))
    assert any("algorithm" in e for e in validate_step_params(
        "matcheval", {"algorithm": ["evil"]}))
    assert any("output_level" in e for e in validate_step_params(
        "matcheval", {"output_level": "everything"}))
    # 合法受控参数
    assert validate_step_params("locust", {"locustfile": "http-fixture", "users": 5,
                                           "spawn_rate": 2, "run_time": "15s"}) == []
    assert validate_step_params("matcheval", {"dataset": "fixture-small",
                                              "algorithm": ["tpl"], "threshold": 0.8}) == []


# ---------- 隔离 SQLite：CRUD / 启停 / 引用约束 / 快照不可变 ----------
_TMP_DIRS: list[str] = []


def _cleanup_tmp_dirs() -> None:
    """进程退出时清理临时目录（gc + 重试：sqlite 连接句柄释放滞后的规避，见 P1 技术债）。"""
    import time
    for d in _TMP_DIRS:
        for _ in range(8):
            gc.collect()
            shutil.rmtree(d, ignore_errors=True)
            if not os.path.exists(d):
                break
            time.sleep(0.2)


atexit.register(_cleanup_tmp_dirs)


def _db():
    tmp = tempfile.mkdtemp(prefix="cc_test_")
    _TMP_DIRS.append(tmp)
    path = os.path.join(tmp, "db.sqlite")
    db.init_db(path)
    return path


def test_db_crud_toggle_and_plan_ref_constraints():
    path = _db()
    eid = db.create_environment(path, {"name": "e1", "host": "localhost", "port": 2567,
                                       "kind": "colyseus", "labels": ["a"]})
    assert db.get_environment(path, eid)["labels"] == '["a"]'
    db.update_environment(path, eid, {"enabled": 0})
    assert db.get_environment(path, eid)["enabled"] == 0

    xid = db.create_executor(path, {"name": "x1", "python_ref": "anaconda",
                                    "cwd_root": "backend-demo",
                                    "allowed_runners": ["pytest"], "max_concurrency": 1,
                                    "python_executable": "", "cwd": ""})
    assert db.get_executor(path, xid)["python_ref"] == "anaconda"

    aid = db.create_asset_source(path, {"name": "a1", "kind": "locust",
                                        "scenario_ref": "http-fixture"})
    assert db.get_asset_source(path, aid)["scenario_ref"] == "http-fixture"

    # 停用后不可被引用
    errs = validate_plan_refs(db, path, eid, xid, aid)
    assert any("已停用" in e for e in errs)
    db.update_environment(path, eid, {"enabled": 1})
    assert validate_plan_refs(db, path, eid, xid, aid) == []
    # 非法 ID 直接拒绝
    assert validate_plan_refs(db, path, 99999, xid, None)


def test_snapshot_immutable_after_config_edit():
    """配置实体被编辑后，历史 Job 的比较指纹/环境身份必须保持不变。"""
    path = _db()
    eid = db.create_environment(path, {"name": "local", "host": "localhost", "port": 2567,
                                       "kind": "colyseus"})
    xid = db.create_executor(path, {"name": "x", "python_ref": "anaconda",
                                    "cwd_root": "backend-demo",
                                    "allowed_runners": ["locust"], "python_executable": "",
                                    "cwd": ""})
    env = db.get_environment(path, eid)
    ex = db.get_executor(path, xid)
    snapshot = build_config_snapshot(env, ex, None)

    pid = db.create_plan(path, Plan(name="p", description="", environment_id=eid,
                                    executor_id=xid, steps=[PlanStep("locust", "l")]))
    run_id = db.create_run(path, pid)
    job = Job(plan_run_id=run_id, plan_id=pid, engine="locust", name="l",
              environment_id=eid, executor_id=xid,
              params={"locustfile": "http-fixture", "users": 4, "spawn_rate": 2,
                      "run_time": "12s"},
              status="success", config_snapshot=snapshot)
    jid = db.insert_job(path, job)
    db.save_report(path, jid, run_id, "reports/x.json", {
        "engine": "locust", "status": "success", "schema_version": "1.0",
        "engine_data_schema": "locust/1.0", "started_at": "2026-01-01T00:00:00",
        "ended_at": "2026-01-01T00:01:00",
        "summary": {"duration_ms": 60000}, "metrics": {"rps": 10, "p95_ms": 50},
        "artifacts": [], "engine_data": {}})
    rebuild_report_index(path)
    before = db.get_report_index(path, jid)
    assert before["env_name"] == "local"

    # 编辑配置实体：改名 + 换 host
    db.update_environment(path, eid, {"name": "renamed", "host": "10.0.0.9"})
    rebuild_report_index(path)
    after = db.get_report_index(path, jid)
    assert after["env_name"] == "local"                        # 快照优先，不被编辑影响
    assert after["comparator"] == before["comparator"]         # 比较指纹不漂移
    assert after["compare_key"] == before["compare_key"]