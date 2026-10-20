"""单元测试：测试计划编辑器 + 不可变执行快照（版本化 / 快照 / 预检 / 参数白名单）。

运行：python -m pytest backend/tests/test_plan_editor.py -q
只测纯函数与隔离 SQLite，不依赖运行中的服务，也不发起任何执行/压测。
"""
import atexit
import gc
import os
import shutil
import socket
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import db
from app.config_center import (build_plan_snapshot, plan_snapshot_name, precheck_plan,
                               validate_step_params)
from app.domain import Plan, PlanStep


_TMP_DIRS: list[str] = []


def _cleanup_tmp_dirs() -> None:
    """进程退出时清理临时目录。

    说明：`db.get_conn` 用的是 sqlite3 的**事务**上下文管理器（并不 close），
    Windows 上文件句柄释放滞后，直接 rmtree 常常失败并静默留下一个库文件，
    所以这里 gc + 重试。（连接生命周期本身是已知 P1 技术债，不在测试里改。）
    """
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
    tmp = tempfile.mkdtemp(prefix="pe_test_")
    _TMP_DIRS.append(tmp)
    path = os.path.join(tmp, "db.sqlite")
    db.init_db(path)
    return path


def _plan(steps, *, name="p", revision=1, fail_fast=True,
          environment_id=1, executor_id=2):
    return {
        "id": 7, "name": name, "description": "d", "revision": revision,
        "fail_fast": fail_fast, "owner": "local",
        "environment_id": environment_id, "executor_id": executor_id,
        "asset_source_id": None,
        "steps": steps,
    }


# ---------- 步骤参数白名单 ----------
def test_relative_selector_accepted_absolute_rejected():
    ok = ["demo/test_platform_demo.py", "-o", "addopts=", "--tb=short", "-k", "smoke"]
    assert validate_step_params("pytest", {"args": ok}) == []
    for bad in ([r"C:\evil.py"], ["/etc/passwd"], ["../../x.py"], ["a.py; rm -rf /"],
                ["tool.exe"], ["a && b"], ["$(whoami)"], [""]):
        assert validate_step_params("pytest", {"args": bad}), bad


def test_threshold_range_and_numeric():
    assert validate_step_params("matcheval", {"threshold": 0.0}) == []
    assert validate_step_params("matcheval", {"threshold": 1.0}) == []
    assert any("0..1" in e for e in validate_step_params("matcheval", {"threshold": 1.5}))
    assert any("0..1" in e for e in validate_step_params("matcheval", {"threshold": -0.1}))
    assert any("数字" in e for e in validate_step_params("matcheval", {"threshold": "abc"}))


def test_unknown_engine_and_arbitrary_keys_rejected():
    assert validate_step_params("shell", {"args": []}) == ["未知引擎：shell"]
    assert any("不接受的参数键" in e for e in validate_step_params(
        "pytest", {"command": "rm -rf /"}))
    assert any("不接受的参数键" in e for e in validate_step_params(
        "locust", {"host": "http://evil"}))


# ---------- 计划快照 ----------
def test_build_plan_snapshot_structure_and_fingerprint_stable():
    steps = [
        {"engine": "pytest", "name": "smoke", "requires_exclusive": False,
         "params": {"args": ["demo/test_platform_demo.py"], "cwd_root": "backend-demo"}},
        {"engine": "locust", "name": "l", "params": {"locustfile": "http-fixture", "users": 4}},
    ]
    snap = build_plan_snapshot(_plan(steps, name="三步骤", revision=3))
    assert snap["name"] == "三步骤" and snap["revision"] == 3
    assert snap["fail_fast"] is True and snap["owner"] == "local"
    assert [s["engine"] for s in snap["steps"]] == ["pytest", "locust"]
    assert [s["index"] for s in snap["steps"]] == [0, 1]
    assert snap["snapshot_fingerprint"]
    # 同定义 → 同指纹；换 revision → 指纹变化
    assert build_plan_snapshot(_plan(steps, name="三步骤", revision=3))["snapshot_fingerprint"] \
        == snap["snapshot_fingerprint"]
    assert build_plan_snapshot(_plan(steps, name="三步骤", revision=4))["snapshot_fingerprint"] \
        != snap["snapshot_fingerprint"]


def test_plan_snapshot_name_tolerates_json_and_empty():
    assert plan_snapshot_name('{"name": "快照名"}') == "快照名"
    assert plan_snapshot_name({"name": "x"}) == "x"
    assert plan_snapshot_name(None) == "" and plan_snapshot_name("not-json") == ""


# ---------- 隔离 SQLite：版本化 / 快照固化 ----------
def test_create_plan_starts_at_revision_1():
    path = _db()
    pid = db.create_plan(path, Plan(name="p1", description="", environment_id=1,
                                    executor_id=2, steps=[PlanStep(engine="pytest", name="s")]))
    p = db.get_plan(path, pid)
    assert p["revision"] == 1 and p["created_at"] and p["updated_at"]


def test_update_plan_bumps_revision_and_can_be_suppressed():
    path = _db()
    pid = db.create_plan(path, Plan(name="p1", description="", environment_id=1,
                                    executor_id=2,
                                    steps=[PlanStep(engine="pytest", name="s")]))
    db.update_plan(path, pid, {"name": "p1-r2"})
    assert db.get_plan(path, pid)["revision"] == 2
    db.update_plan(path, pid, {"name": "p1-r3"})
    assert db.get_plan(path, pid)["revision"] == 3
    # 迁移回填等非用户编辑不递增版本
    db.update_plan(path, pid, {"asset_source_id": 5}, bump_revision=False)
    p = db.get_plan(path, pid)
    assert p["revision"] == 3 and p["asset_source_id"] == 5


def test_planrun_freezes_revision_and_snapshot():
    path = _db()
    pid = db.create_plan(path, Plan(name="snap-A", description="", environment_id=1,
                                    executor_id=2, fail_fast=True,
                                    steps=[PlanStep(engine="pytest", name="a"),
                                           PlanStep(engine="locust", name="b")]))
    plan = db.get_plan(path, pid)
    snap = build_plan_snapshot(plan)
    rid = db.create_run(path, pid, plan_revision=plan["revision"], plan_snapshot=snap)
    run = db.get_run(path, rid)
    assert run["plan_revision"] == 1
    assert run["plan_snapshot"]["name"] == "snap-A"
    assert [s["engine"] for s in run["plan_snapshot"]["steps"]] == ["pytest", "locust"]

    # 编辑原计划：PlanRun 版本与快照必须不变
    db.update_plan(path, pid, {"name": "snap-A-renamed", "fail_fast": 0,
                               "steps": [{"engine": "matcheval", "name": "c", "params": {}}]})
    after = db.get_plan(path, pid)
    assert after["revision"] == 2 and after["name"] == "snap-A-renamed"
    frozen = db.get_run(path, rid)
    assert frozen["plan_revision"] == 1
    assert frozen["plan_snapshot"]["name"] == "snap-A"
    assert frozen["plan_snapshot"]["fail_fast"] is True
    assert frozen["plan_snapshot"]["snapshot_fingerprint"] == snap["snapshot_fingerprint"]


def test_legacy_db_migration_adds_columns_without_rewriting():
    tmp = tempfile.mkdtemp(prefix="pe_legacy_")
    _TMP_DIRS.append(tmp)
    path = os.path.join(tmp, "db.sqlite")
    import sqlite3
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE plans(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                  "steps TEXT DEFAULT '[]', fail_fast INTEGER DEFAULT 1)")
        c.execute("INSERT INTO plans(name,steps,fail_fast) VALUES('legacy','[]',1)")
        c.execute("CREATE TABLE plan_runs(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                  "plan_id INTEGER, status TEXT)")
        c.execute("INSERT INTO plan_runs(plan_id,status) VALUES(1,'success')")
        c.commit()
    db.init_db(path)
    p = db.get_plan(path, 1)
    assert p["revision"] == 1 and p["name"] == "legacy"
    r = db.get_run(path, 1)
    assert r["plan_revision"] is None and r["plan_snapshot"] is None


# ---------- 预检 / 执行预览：零副作用 ----------
def _count(path, table):
    import sqlite3
    with sqlite3.connect(path) as c:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_precheck_plan_no_side_effects_and_reports_readiness():
    path = _db()
    before = (_count(path, "plan_runs"), _count(path, "jobs"))
    plan = _plan([{"engine": "pytest", "name": "s",
                   "params": {"args": ["demo/test_platform_demo.py"]}}],
                 name="draft", revision=0)
    r = precheck_plan(db, path, plan)
    assert r["no_side_effects"] is True
    assert r["entity"] == "plan" and r["name"] == "draft"
    assert [s["engine"] for s in r["steps"]] == ["pytest"]
    assert {"engine_registered", "params_ok", "executor_allows", "deps_ready",
            "ready"} <= set(r["steps"][0])
    assert "enabled" in r["fail_fast"] and "behavior" in r["fail_fast"]
    assert "exclusive_lock_held" in r["concurrency"]
    assert r["fingerprint"]
    assert (_count(path, "plan_runs"), _count(path, "jobs")) == before

    # ---------- Environment 连通性的「必需 vs 仅供参考」（2026-10-13 修误报） ----------
    # 这里必须用**真实存在**的配置实体：上面 _plan 默认引用的 environment_id=1 / executor_id=2
    # 在 init_db 刚建的库里并不存在（env_pc 恒为 None），所以这条分支此前从未被覆盖过。
    def _closed_port() -> int:
        """拿一个刚被释放的端口 —— 无人监听 ⇒ 连通性探测必然失败。"""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    ex_id = db.create_executor(path, {
        "name": "ex-pytest-only", "python_executable": sys.executable, "cwd": "",
        "python_ref": "", "cwd_root": "backend-demo", "allowed_runners": ["pytest"],
        "max_concurrency": 1, "enabled": 1})
    env_down = db.create_environment(path, {
        "name": "e-down", "host": "127.0.0.1", "port": _closed_port(),
        "kind": "http", "protocol": "http", "enabled": 1})
    pytest_step = {"engine": "pytest", "name": "s",
                   "params": {"args": ["demo/test_platform_demo.py"]}}
    base = (_count(path, "plan_runs"), _count(path, "jobs"))

    # (1) 纯 pytest + Environment 不可达 → env **仅供参考**，不阻断整体预检
    r1 = precheck_plan(db, path, _plan([pytest_step], environment_id=env_down,
                                       executor_id=ex_id))
    assert r1["config"]["environment"]["ok"] is False            # 探测确实失败了
    assert r1["config"]["environment"]["requirement"] == "informational"
    assert r1["environment_required"] is False
    assert "被测环境不可达" not in r1["missing"]                  # 不再计入缺失项
    assert r1["notices"] and any("没有显式的环境依赖声明机制" in n for n in r1["notices"])
    assert r1["ok"] is True, (r1["missing"], r1["notices"])       # 关键：不被无关 env 拖垮

    # (2) 含 Locust 步骤 + Environment 不可达 → env 仍是**必需项**，明确提示（既有规则不变）
    locust_step = {"engine": "locust", "name": "l",
                   "params": {"locustfile": "http-fixture", "users": 2,
                              "spawn_rate": 1, "run_time": "5s"}}
    r2 = precheck_plan(db, path, _plan([locust_step], environment_id=env_down,
                                       executor_id=ex_id))
    assert r2["config"]["environment"]["requirement"] == "required"
    assert r2["environment_required"] is True
    assert "被测环境不可达" in r2["missing"]
    assert r2["ok"] is False
    assert any("目标服务已启动" in s for s in r2["next_steps"])

    # (3) Environment 可达 → 原有行为保持：就绪、无提示、无缺失
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(1)
    try:
        env_up = db.create_environment(path, {
            "name": "e-up", "host": "127.0.0.1", "port": ls.getsockname()[1],
            "kind": "http", "protocol": "http", "enabled": 1})
        r3 = precheck_plan(db, path, _plan([pytest_step], environment_id=env_up,
                                           executor_id=ex_id))
        assert r3["config"]["environment"]["ok"] is True
        assert r3["notices"] == [] and "被测环境不可达" not in r3["missing"]
        assert r3["ok"] is True
    finally:
        ls.close()

    # 三种场景走完，预检依然**零副作用**（未创建 PlanRun/Job）
    assert (_count(path, "plan_runs"), _count(path, "jobs")) == base


def test_precheck_plan_flags_unregistered_engine():
    path = _db()
    r = precheck_plan(db, path, _plan([{"engine": "shell", "name": "x", "params": {}}]))
    s = r["steps"][0]
    assert s["engine_registered"] is False and s["ready"] is False
    assert r["ok"] is False and r["missing"] and r["next_steps"]


def test_precheck_plan_flags_invalid_params_and_empty_steps():
    path = _db()
    r = precheck_plan(db, path, _plan([{"engine": "matcheval", "name": "m",
                                        "params": {"threshold": 2.0}}]))
    assert r["steps"][0]["params_ok"] is False and r["ok"] is False
    r2 = precheck_plan(db, path, _plan([]))
    assert any("没有执行单元" in m for m in r2["missing"])