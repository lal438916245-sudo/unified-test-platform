"""SQLite 存储层：每个操作独立连接 + 全局锁，供 API 线程与编排线程共用。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from .domain import Plan, RUN_TERMINAL, STATUS_QUEUED

_LOCK = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class _ClosingConnection(sqlite3.Connection):
    """让 `with get_conn(...)` **既管事务、也管关闭连接**。

    sqlite3 的 Connection 上下文管理器只提交/回滚事务，**不会 close()**；
    于是连接只能靠引用计数回收，在 Windows 上句柄释放滞后会锁住库文件
    （测试里不得不写 `_rmtree_retry` 规避）。这里在 `__exit__` 里补一次 close，
    语义与 `contextlib.closing()` 等价，但**无需改动 50 余处既有调用点**。
    """

    def __exit__(self, exc_type, exc, tb):  # type: ignore[override]
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with _LOCK, get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS environments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                port INTEGER NOT NULL,
                notes TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS executors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                python_executable TEXT NOT NULL,
                cwd TEXT DEFAULT '',
                max_concurrency INTEGER DEFAULT 1,
                notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS asset_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                dataset_ref TEXT DEFAULT '',
                tpl_ref TEXT DEFAULT '',
                scenes_ref TEXT DEFAULT '',
                ground_truth_ref TEXT DEFAULT '',
                airtest_ref TEXT DEFAULT '',
                scenario_ref TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                enabled INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                environment_id INTEGER,
                executor_id INTEGER,
                asset_source_id INTEGER,
                steps TEXT DEFAULT '[]',
                fail_fast INTEGER DEFAULT 1,
                owner TEXT DEFAULT 'local',
                revision INTEGER DEFAULT 1,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS plan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER,
                plan_revision INTEGER,
                plan_snapshot TEXT,
                status TEXT,
                started_at TEXT,
                ended_at TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_run_id INTEGER,
                plan_id INTEGER,
                engine TEXT,
                name TEXT,
                environment_id INTEGER,
                executor_id INTEGER,
                timeout_sec INTEGER DEFAULT 300,
                requires_exclusive INTEGER DEFAULT 0,
                params TEXT DEFAULT '{}',
                status TEXT,
                exit_code INTEGER,
                log_path TEXT,
                report_path TEXT,
                junit_path TEXT,
                cancel_requested INTEGER DEFAULT 0,
                parent_job_id INTEGER,
                started_at TEXT,
                ended_at TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(plan_run_id);
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER UNIQUE,
                plan_run_id INTEGER,
                report_path TEXT,
                report_json TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS executor_locks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                executor_id INTEGER NOT NULL,
                holder_job_id INTEGER NOT NULL,
                acquired_at TEXT,
                released_at TEXT
            );
            CREATE TABLE IF NOT EXISTS report_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER UNIQUE,
                plan_id INTEGER,
                plan_run_id INTEGER,
                plan_name TEXT,
                engine TEXT,
                engine_data_schema TEXT,
                schema_main_version TEXT,
                status TEXT,
                env_id INTEGER,
                env_name TEXT,
                env_host TEXT,
                started_at TEXT,
                ended_at TEXT,
                duration_ms REAL,
                comparator TEXT,
                compare_key TEXT,
                metrics TEXT,
                summary TEXT,
                metadata_incomplete INTEGER DEFAULT 0,
                extra TEXT,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ri_engine ON report_index(engine);
            CREATE INDEX IF NOT EXISTS idx_ri_status ON report_index(status);
            CREATE INDEX IF NOT EXISTS idx_ri_started ON report_index(started_at);
            CREATE INDEX IF NOT EXISTS idx_ri_comparator ON report_index(comparator);
            -- v0.2 TestCase 定义层：只保存"测试定义"，不含任何执行态字段、
            -- 不绑定 Environment/Executor/AssetSource（那三项属 Plan）。
            -- 与全库一致：不建 FOREIGN KEY，关联由应用层维护。
            CREATE TABLE IF NOT EXISTS test_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                engine TEXT NOT NULL,
                params TEXT NOT NULL DEFAULT '{}',
                requires_exclusive INTEGER NOT NULL DEFAULT 0,
                asset_kind TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                owner TEXT DEFAULT 'local',
                tags TEXT DEFAULT '[]',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_tc_engine ON test_cases(engine);
            CREATE INDEX IF NOT EXISTS idx_tc_enabled ON test_cases(enabled);
            -- v0.2 TestSuite 定义层：**有序的用例引用集合**。
            -- cases 存 JSON（[{id, revision, fingerprint}, ...]），与 plans.steps 同风格 ——
            -- 数组下标即执行顺序，故**不建 test_suite_cases 关联表**（全库无 FOREIGN KEY，
            -- 关联表拿不到完整性收益，却会引入第二个顺序源）。
            -- 不绑定 Environment / Executor / AssetSource（那三项属 Plan）；
            -- 不复制用例定义（engine/params/requires_exclusive/asset_kind 一律不在本表）。
            CREATE TABLE IF NOT EXISTS test_suites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                cases TEXT NOT NULL DEFAULT '[]',
                fingerprint TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1,
                owner TEXT DEFAULT 'local',
                tags TEXT DEFAULT '[]',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ts_enabled ON test_suites(enabled);
            """
        )
        # 增量迁移：给已存在的旧库补列（retry_of / attempt，任务控制加固后新增）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        if "retry_of" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN retry_of INTEGER")
        if "attempt" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN attempt INTEGER DEFAULT 1")
        # 配置中心迁移：环境/执行机补列（受控引用 + 类型 + 凭据引用 + 启用态 + 标签）
        ecols = {r[1] for r in conn.execute("PRAGMA table_info(environments)")}
        for name, ddl in (
            ("kind", "TEXT DEFAULT 'http'"),
            ("protocol", "TEXT DEFAULT 'http'"),
            ("secret_ref", "TEXT DEFAULT ''"),
            ("secret_kind", "TEXT DEFAULT ''"),
            ("labels", "TEXT DEFAULT '[]'"),
            ("created_at", "TEXT"),
            ("updated_at", "TEXT"),
        ):
            if name not in ecols:
                conn.execute(f"ALTER TABLE environments ADD COLUMN {name} {ddl}")
        xcols = {r[1] for r in conn.execute("PRAGMA table_info(executors)")}
        for name, ddl in (
            ("python_ref", "TEXT DEFAULT ''"),
            ("cwd_root", "TEXT DEFAULT ''"),
            ("allowed_runners", "TEXT DEFAULT '[]'"),
            ("enabled", "INTEGER DEFAULT 1"),
            ("created_at", "TEXT"),
            ("updated_at", "TEXT"),
        ):
            if name not in xcols:
                conn.execute(f"ALTER TABLE executors ADD COLUMN {name} {ddl}")
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(plans)")}
        if "asset_source_id" not in pcols:
            conn.execute("ALTER TABLE plans ADD COLUMN asset_source_id INTEGER")
        if "revision" not in pcols:
            conn.execute("ALTER TABLE plans ADD COLUMN revision INTEGER DEFAULT 1")
        # 计划编辑器迁移：PlanRun 固化"计划版本 + 不可变计划快照"
        rcols = {r[1] for r in conn.execute("PRAGMA table_info(plan_runs)")}
        if "plan_revision" not in rcols:
            conn.execute("ALTER TABLE plan_runs ADD COLUMN plan_revision INTEGER")
        if "plan_snapshot" not in rcols:
            conn.execute("ALTER TABLE plan_runs ADD COLUMN plan_snapshot TEXT")
        if "config_snapshot" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN config_snapshot TEXT")
        conn.commit()


# ---------- Environment / Executor ----------
_ENV_COLS = ("name", "host", "port", "kind", "protocol", "secret_ref", "secret_kind",
             "labels", "notes", "enabled")
_EXEC_COLS = ("name", "python_executable", "cwd", "python_ref", "cwd_root",
              "allowed_runners", "max_concurrency", "notes", "enabled")
_ASSET_COLS = ("name", "kind", "dataset_ref", "tpl_ref", "scenes_ref",
               "ground_truth_ref", "airtest_ref", "scenario_ref", "notes", "enabled")


def list_environments(db_path: str) -> list[dict]:
    with _LOCK, get_conn(db_path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM environments ORDER BY id")]


def get_environment(db_path: str, eid: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM environments WHERE id=?", (eid,)).fetchone()
        return dict(r) if r else None


def get_executor_by_name(db_path: str, name: str) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM executors WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None


def _entity_create(db_path: str, table: str, cols: tuple, data: dict) -> int:
    keys = [k for k in cols if k in data]
    keys += ["created_at", "updated_at"]
    vals = [_json_col(k, data.get(k)) for k in keys[:-2]] + [_now(), _now()]
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(
            f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", vals)
        c.commit()
        return cur.lastrowid


def _entity_update(db_path: str, table: str, eid: int, cols: tuple, data: dict) -> bool:
    fields = {k: _json_col(k, data[k]) for k in cols if k in data}
    if not fields:
        return False
    fields["updated_at"] = _now()
    set_sql = ", ".join(f"{k}=?" for k in fields)
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(f"UPDATE {table} SET {set_sql} WHERE id=?", (*fields.values(), eid))
        c.commit()
        return cur.rowcount > 0


def _json_col(key: str, v):
    if key in ("labels", "allowed_runners") and isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    return v


def create_environment(db_path: str, data: dict) -> int:
    return _entity_create(db_path, "environments", _ENV_COLS, data)


def update_environment(db_path: str, eid: int, data: dict) -> bool:
    return _entity_update(db_path, "environments", eid, _ENV_COLS, data)


def seed_environment(db_path: str, name: str, host: str, port: int,
                     kind: str = "http", protocol: str = "http",
                     labels: Optional[list] = None) -> int:
    return create_environment(db_path, {
        "name": name, "host": host, "port": int(port), "kind": kind,
        "protocol": protocol, "labels": labels or [], "enabled": 1,
    })


def list_executors(db_path: str) -> list[dict]:
    with _LOCK, get_conn(db_path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM executors ORDER BY id")]


def get_executor(db_path: str, xid: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM executors WHERE id=?", (xid,)).fetchone()
        return dict(r) if r else None


def create_executor(db_path: str, data: dict) -> int:
    return _entity_create(db_path, "executors", _EXEC_COLS, data)


def update_executor(db_path: str, xid: int, data: dict) -> bool:
    return _entity_update(db_path, "executors", xid, _EXEC_COLS, data)


def seed_executor(db_path: str, name: str, python: str, cwd: str,
                  python_ref: str = "", cwd_root: str = "",
                  allowed_runners: Optional[list] = None) -> int:
    return create_executor(db_path, {
        "name": name, "python_executable": python, "cwd": cwd,
        "python_ref": python_ref, "cwd_root": cwd_root,
        "allowed_runners": allowed_runners or [], "max_concurrency": 1, "enabled": 1,
    })


# ---------- AssetSource ----------
def list_asset_sources(db_path: str) -> list[dict]:
    with _LOCK, get_conn(db_path) as c:
        return [dict(r) for r in c.execute("SELECT * FROM asset_sources ORDER BY id")]


def get_asset_source(db_path: str, aid: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM asset_sources WHERE id=?", (aid,)).fetchone()
        return dict(r) if r else None


def get_asset_source_by_name(db_path: str, name: str) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM asset_sources WHERE name=?", (name,)).fetchone()
        return dict(r) if r else None


def create_asset_source(db_path: str, data: dict) -> int:
    return _entity_create(db_path, "asset_sources", _ASSET_COLS, data)


def update_asset_source(db_path: str, aid: int, data: dict) -> bool:
    return _entity_update(db_path, "asset_sources", aid, _ASSET_COLS, data)


# ---------- TestCase（v0.2 定义层） ----------
# 定义层实体：只描述"要跑什么"，不描述"在哪儿跑/用谁跑/用哪份数据跑"（属 Plan）。
# revision 语义与 plans.revision 完全一致：用户编辑定义字段才 +1；预置/回填必须 bump_revision=False。
_TC_COLS = ("name", "description", "engine", "params", "requires_exclusive",
            "asset_kind", "fingerprint", "tags", "owner", "enabled")


def _test_case_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.get("params") or "{}")
    d["tags"] = _json_or_list(d.get("tags"))
    d["revision"] = int(d.get("revision") or 1)
    d["enabled"] = int(d.get("enabled") if d.get("enabled") is not None else 1)
    d["requires_exclusive"] = bool(d.get("requires_exclusive"))
    return d


def _json_or_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        try:
            got = json.loads(v)
            return got if isinstance(got, list) else []
        except Exception:  # noqa: BLE001
            return []
    return []


def list_test_cases(db_path: str, engine: str = "", enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM test_cases"
    where, args = [], []
    if engine:
        where.append("engine=?"); args.append(str(engine))
    if enabled_only:
        where.append("enabled=1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id"
    with _LOCK, get_conn(db_path) as c:
        return [_test_case_row(r) for r in c.execute(sql, args).fetchall()]


def get_test_case(db_path: str, cid: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM test_cases WHERE id=?", (cid,)).fetchone()
        return _test_case_row(r) if r else None


def get_test_case_by_name(db_path: str, name: str) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM test_cases WHERE name=?", (name,)).fetchone()
        return _test_case_row(r) if r else None


def create_test_case(db_path: str, data: dict) -> int:
    cols = _TC_COLS + ("revision", "created_at", "updated_at")
    vals = [data.get(k) for k in _TC_COLS] + [1, _now(), _now()]
    vals[_TC_COLS.index("params")] = json.dumps(data.get("params") or {}, ensure_ascii=False)
    vals[_TC_COLS.index("tags")] = json.dumps(_json_or_list(data.get("tags")), ensure_ascii=False)
    vals[_TC_COLS.index("requires_exclusive")] = int(bool(data.get("requires_exclusive")))
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(
            f"INSERT INTO test_cases({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        c.commit()
        return cur.lastrowid


def update_test_case(db_path: str, cid: int, data: dict, bump_revision: bool = True) -> bool:
    """编辑 TestCase：只接受受控字段。
    定义字段变化时由调用方决定 bump_revision；默认递增（版本化），
    预置/回填等非用户编辑必须显式 bump_revision=False。"""
    allowed = ("name", "description", "engine", "params", "requires_exclusive",
               "asset_kind", "fingerprint", "tags", "owner", "enabled")
    fields: dict = {}
    for k in allowed:
        if k in data:
            v = data[k]
            if k == "params":
                v = json.dumps(v or {}, ensure_ascii=False)
            elif k == "tags":
                v = json.dumps(_json_or_list(v), ensure_ascii=False)
            elif k == "requires_exclusive":
                v = int(bool(v))
            elif k == "enabled":
                v = int(bool(v))
            fields[k] = v
    if not fields:
        return False
    fields["updated_at"] = _now()
    sets = [f"{k}=?" for k in fields]
    vals = list(fields.values())
    if bump_revision:
        sets.append("revision=COALESCE(revision,1)+1")
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(f"UPDATE test_cases SET {', '.join(sets)} WHERE id=?", (*vals, cid))
        c.commit()
        return cur.rowcount > 0


def set_test_case_enabled(db_path: str, cid: int, enabled: int) -> bool:
    """启停：可用性开关，不属"定义"，因此不递增 revision。"""
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute("UPDATE test_cases SET enabled=?, updated_at=? WHERE id=?",
                        (int(bool(enabled)), _now(), cid))
        c.commit()
        return cur.rowcount > 0


def delete_test_case(db_path: str, cid: int) -> bool:
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute("DELETE FROM test_cases WHERE id=?", (cid,))
        c.commit()
        return cur.rowcount > 0


# ---------- TestSuite（v0.2 定义层：有序用例集合） ----------
# Suite 只是「引用哪些用例、按什么顺序」；不描述"在哪儿跑/用谁跑/用哪份数据跑"（属 Plan）。
# revision 语义与 test_cases / plans 完全一致：定义字段变化才 +1；预置/回填必须 bump_revision=False。
# ⚠️ 本表刻意**没有** environment_id / executor_id / asset_source_id / fail_fast / engine / params：
#    前三项属 Plan（否则同一个 Suite 无法跨环境复用，且与 Plan 形成双源）；
#    engine 由 cases 派生且 Suite 允许跨引擎；params 属用例。
_SUITE_COLS = ("name", "description", "cases", "fingerprint", "tags", "owner", "enabled")


def normalize_suite_cases(v) -> list:
    """cases 列的**规范形态**定义（本函数是唯一实现，供路由层与指纹层复用）。

    只保留 {id, revision, fingerprint} 三键，id/revision 强制为 int。
    与 _json_or_list 同样的防御式风格：非 dict 或缺 id 的条目丢弃（阻断脏数据流入展开逻辑）。
    正常写入路径已由路由层校验，这里只做兜底。

    ⚠️ 之所以放在存储层：`cases` 是**列的形状**，规范形态应由持有该列的层定义；
    路由层（校验/组 payload）与指纹层都调用它，避免出现第二份等价实现而悄悄漂移。
    """
    out: list[dict] = []
    for c in _json_or_list(v):
        if not isinstance(c, dict):
            continue
        try:
            cid = int(c.get("id"))
        except (TypeError, ValueError):
            continue
        try:
            rev = int(c.get("revision") or 1)
        except (TypeError, ValueError):
            rev = 1
        out.append({"id": cid, "revision": rev,
                    "fingerprint": str(c.get("fingerprint") or "")})
    return out


def _suite_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["cases"] = normalize_suite_cases(d.get("cases"))
    d["tags"] = _json_or_list(d.get("tags"))
    d["revision"] = int(d.get("revision") or 1)
    d["enabled"] = int(d.get("enabled") if d.get("enabled") is not None else 1)
    return d


def list_suites(db_path: str, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM test_suites"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    with _LOCK, get_conn(db_path) as c:
        return [_suite_row(r) for r in c.execute(sql).fetchall()]


def get_suite(db_path: str, sid: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM test_suites WHERE id=?", (sid,)).fetchone()
        return _suite_row(r) if r else None


def get_suite_by_name(db_path: str, name: str) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM test_suites WHERE name=?", (name,)).fetchone()
        return _suite_row(r) if r else None


def create_suite(db_path: str, data: dict) -> int:
    cols = _SUITE_COLS + ("revision", "created_at", "updated_at")
    vals = [data.get(k) for k in _SUITE_COLS] + [1, _now(), _now()]
    vals[_SUITE_COLS.index("cases")] = json.dumps(normalize_suite_cases(data.get("cases")),
                                                  ensure_ascii=False)
    vals[_SUITE_COLS.index("tags")] = json.dumps(_json_or_list(data.get("tags")),
                                                 ensure_ascii=False)
    vals[_SUITE_COLS.index("enabled")] = int(bool(data.get("enabled", 1)))
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(
            f"INSERT INTO test_suites({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        c.commit()
        return cur.lastrowid


def update_suite(db_path: str, sid: int, data: dict, bump_revision: bool = True) -> bool:
    """编辑 Suite：只接受受控字段（**cases 整体替换**，不做增量 add/remove/move）。

    定义字段变化时由调用方决定 bump_revision；默认递增（版本化），
    预置/回填等非用户编辑必须显式 bump_revision=False。
    """
    fields: dict = {}
    for k in _SUITE_COLS:
        if k not in data:
            continue
        v = data[k]
        if k == "cases":
            v = json.dumps(normalize_suite_cases(v), ensure_ascii=False)
        elif k == "tags":
            v = json.dumps(_json_or_list(v), ensure_ascii=False)
        elif k == "enabled":
            v = int(bool(v))
        fields[k] = v
    if not fields:
        return False
    fields["updated_at"] = _now()
    sets = [f"{k}=?" for k in fields]
    vals = list(fields.values())
    if bump_revision:
        sets.append("revision=COALESCE(revision,1)+1")
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(f"UPDATE test_suites SET {', '.join(sets)} WHERE id=?", (*vals, sid))
        c.commit()
        return cur.rowcount > 0


def set_suite_enabled(db_path: str, sid: int, enabled: int) -> bool:
    """启停：可用性开关，不属"定义"，因此不递增 revision（与 TestCase 一致）。"""
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute("UPDATE test_suites SET enabled=?, updated_at=? WHERE id=?",
                        (int(bool(enabled)), _now(), sid))
        c.commit()
        return cur.rowcount > 0


def delete_suite(db_path: str, sid: int) -> bool:
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute("DELETE FROM test_suites WHERE id=?", (sid,))
        c.commit()
        return cur.rowcount > 0


def suites_referencing_case(db_path: str, cid: int) -> list[int]:
    """只读：返回 cases 中引用了该 TestCase 的 Suite id 列表。

    用途 = 删除用例前的**引用保护**。此前 _plans_referencing_case 只扫 plans，
    Suite 一旦引用用例，删掉该用例就会留下指向不存在用例的悬空 case_ref
    （该 Suite 将永久无法展开）。本函数补齐另一半。
    """
    out: list[int] = []
    for s in list_suites(db_path):
        if any(int(c.get("id") or 0) == int(cid) for c in (s.get("cases") or [])):
            out.append(s["id"])
    return out


# ---------- Plan ----------
def list_plans(db_path: str) -> list[dict]:
    with _LOCK, get_conn(db_path) as c:
        rows = c.execute("SELECT * FROM plans ORDER BY id").fetchall()
        return [_plan_row(r) for r in rows]


def get_plan(db_path: str, pid: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
        return _plan_row(r) if r else None


def _plan_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["steps"] = json.loads(d.get("steps") or "[]")
    d["revision"] = int(d.get("revision") or 1)
    return d


def create_plan(db_path: str, p: Plan) -> int:
    # ⚠️ 必须走 PlanStep.to_dict()：v0.1 这里硬编码只序列化 4 个键，
    # 会让 case_ref / override_params 在落库时被静默丢弃（v0.2 透传点 6/9）。
    steps = json.dumps([s.to_dict() for s in p.steps], ensure_ascii=False)
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO plans(name,description,environment_id,executor_id,asset_source_id,steps,fail_fast,owner,revision,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (p.name, p.description, p.environment_id, p.executor_id,
             getattr(p, "asset_source_id", 0) or None, steps,
             int(p.fail_fast), p.owner, 1, _now(), _now()),
        )
        c.commit()
        return cur.lastrowid


def update_plan(db_path: str, pid: int, data: dict, bump_revision: bool = True) -> bool:
    """编辑计划：只允许受控字段（名称/描述/引用 ID/步骤/失败即停）。
    用户编辑默认递增 revision（版本化）；迁移回填等非用户编辑可 bump_revision=False。"""
    allowed = ("name", "description", "environment_id", "executor_id",
               "asset_source_id", "steps", "fail_fast", "owner")
    fields = {}
    for k in allowed:
        if k in data:
            v = data[k]
            fields[k] = json.dumps(v, ensure_ascii=False) if k == "steps" else v
    if not fields:
        return False
    fields["updated_at"] = _now()
    sets = [f"{k}=?" for k in fields]
    vals = list(fields.values())
    if bump_revision:
        sets.append("revision=COALESCE(revision,1)+1")
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(f"UPDATE plans SET {', '.join(sets)} WHERE id=?", (*vals, pid))
        c.commit()
        return cur.rowcount > 0


# ---------- PlanRun ----------
def create_run(db_path: str, plan_id: int, plan_revision: Optional[int] = None,
               plan_snapshot: Optional[dict] = None) -> int:
    """创建 PlanRun 并固化"执行时的计划版本 + 不可变计划快照"。
    后续编辑 Plan 不会改变已创建 PlanRun 的结构/参数/引用。"""
    snap = json.dumps(plan_snapshot, ensure_ascii=False) if plan_snapshot else None
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO plan_runs(plan_id,plan_revision,plan_snapshot,status,started_at)"
            " VALUES(?,?,?,?,?)",
            (plan_id, plan_revision, snap, STATUS_QUEUED, _now()),
        )
        c.commit()
        return cur.lastrowid


def _run_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    raw = d.get("plan_snapshot")
    if isinstance(raw, str) and raw.strip():
        try:
            d["plan_snapshot"] = json.loads(raw)
        except Exception:  # noqa: BLE001 —— 非法快照视作无快照，不阻断历史读取
            d["plan_snapshot"] = None
    else:
        d["plan_snapshot"] = None
    if d.get("plan_revision") is not None:
        d["plan_revision"] = int(d["plan_revision"])
    return d


def get_run(db_path: str, run_id: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM plan_runs WHERE id=?", (run_id,)).fetchone()
        return _run_row(r) if r else None


def list_runs_meta(db_path: str, limit: int = 10, offset: int = 0) -> list[dict]:
    """运行记录元信息（倒序）。offset 用于「加载更多」式的历史浏览。"""
    with _LOCK, get_conn(db_path) as c:
        return [dict(r) for r in c.execute(
            "SELECT id,plan_id,plan_revision,plan_snapshot,status,started_at,ended_at FROM plan_runs "
            "ORDER BY id DESC LIMIT ? OFFSET ?", (int(limit), max(0, int(offset or 0))))]


def update_run_status(db_path: str, run_id: int, status: str, ended: bool = False) -> None:
    with _LOCK, get_conn(db_path) as c:
        if ended:
            c.execute(
                "UPDATE plan_runs SET status=?, ended_at=? WHERE id=?",
                (status, _now(), run_id),
            )
        else:
            c.execute("UPDATE plan_runs SET status=? WHERE id=?", (status, run_id))
        c.commit()


def _run_aggregate(db_path, run_id):
    with _LOCK, get_conn(db_path) as c:
        run = c.execute("SELECT * FROM plan_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return None, []
        jobs = c.execute("SELECT * FROM jobs WHERE plan_run_id=? ORDER BY id", (run_id,)).fetchall()
        return dict(run), [dict(j) for j in jobs]


# ---------- Job ----------
def insert_job(db_path: str, job: Job) -> int:
    snap = getattr(job, "config_snapshot", None)
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO jobs(plan_run_id,plan_id,engine,name,environment_id,executor_id,timeout_sec,"
            "requires_exclusive,params,status,cancel_requested,parent_job_id,retry_of,attempt,config_snapshot)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.plan_run_id, job.plan_id, job.engine, job.name, job.environment_id,
             job.executor_id, job.timeout_sec, int(job.requires_exclusive),
             json.dumps(job.params, ensure_ascii=False), job.status,
             job.cancel_requested, job.parent_job_id, job.retry_of, job.attempt,
             json.dumps(snap, ensure_ascii=False) if snap else None),
        )
        c.commit()
        return cur.lastrowid


def _job_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["params"] = json.loads(d.get("params") or "{}")
    raw_snap = d.get("config_snapshot")
    if isinstance(raw_snap, str) and raw_snap.strip():
        try:
            d["config_snapshot"] = json.loads(raw_snap)
        except Exception:  # noqa: BLE001 —— 非法快照视作无快照，不阻断
            d["config_snapshot"] = None
    else:
        d["config_snapshot"] = None
    return d


def get_job(db_path: str, job_id: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _job_row(r) if r else None


def list_jobs_by_run(db_path: str, run_id: int) -> list[dict]:
    with _LOCK, get_conn(db_path) as c:
        rows = c.execute("SELECT * FROM jobs WHERE plan_run_id=? ORDER BY id", (run_id,)).fetchall()
        return [_job_row(r) for r in rows]


def run_has_live_jobs(db_path: str, run_id: int) -> bool:
    """run 内是否仍有未终态（queued/running/cancelling）的 Job。"""
    with _LOCK, get_conn(db_path) as c:
        r = c.execute(
            "SELECT id FROM jobs WHERE plan_run_id=? AND status NOT IN ('success','failed',"
            "'cancelled','timedout','skipped') LIMIT 1",
            (run_id,),
        ).fetchone()
        return r is not None


def update_job(db_path: str, job_id: int, fields: dict) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _LOCK, get_conn(db_path) as c:
        c.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
        c.commit()


def mark_cancel_requested(db_path: str, job_id: int) -> None:
    update_job(db_path, job_id, {"cancel_requested": 1})


def set_job_running(db_path: str, job_id: int, log_path: str, junit_path: str) -> None:
    update_job(db_path, job_id, {
        "status": "running", "started_at": _now(), "log_path": log_path, "junit_path": junit_path
    })


def set_job_terminal(db_path: str, job_id: int, status: str, exit_code: int,
                     error: Optional[str], ended: bool = True) -> None:
    update_job(db_path, job_id, {
        "status": status, "exit_code": exit_code, "error": error, "ended_at": _now() if ended else None
    })


def cancel_remaining_in_run(db_path: str, run_id: int) -> None:
    """失败即停：把同一 PlanRun 里尚未执行的 queued Job 置为终态 skipped（可追溯）。"""
    with _LOCK, get_conn(db_path) as c:
        c.execute(
            "UPDATE jobs SET status='skipped', ended_at=? WHERE plan_run_id=? AND status='queued'",
            (_now(), run_id),
        )
        c.commit()


def find_queued_job(db_path: str) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute(
            "SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        return _job_row(r) if r else None


def has_running_job(db_path: str) -> bool:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT id FROM jobs WHERE status='running' LIMIT 1").fetchone()
        return r is not None


def any_non_terminal_run(db_path: str) -> bool:
    """是否存在未到终态的 PlanRun。

    ⚠️ 终态集合以 `domain.RUN_TERMINAL` 为**唯一来源**，不要手写枚举：
    此前这里写的是 ('success','failed','cancelled')，**漏了 timedout / partial** →
    历史上一旦出现过超时或部分成功的运行，预检里的 `concurrency.run_in_progress`
    就恒为 true（README §5 记录的"预检因历史 timedout 误报"的根因即此）。
    """
    terminal = sorted(RUN_TERMINAL)
    placeholders = ",".join("?" * len(terminal))
    with _LOCK, get_conn(db_path) as c:
        r = c.execute(
            f"SELECT id FROM plan_runs WHERE status NOT IN ({placeholders}) LIMIT 1",
            tuple(terminal),
        ).fetchone()
        return r is not None


# ---------- Report ----------
def get_report(db_path: str, job_id: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
        return dict(r) if r else None


def save_report(db_path: str, job_id: int, plan_run_id: int, report_path: str,
                report_json: dict) -> int:
    with _LOCK, get_conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO reports(job_id,plan_run_id,report_path,report_json,created_at)"
            " VALUES(?,?,?,?,?)",
            (job_id, plan_run_id, report_path, json.dumps(report_json, ensure_ascii=False), _now()),
        )
        c.commit()
        return cur.lastrowid


# ---------- 报告中心读模型（report_index） ----------
# report.json 仍为唯一事实源；本表仅是派生查询索引，兼容旧记录：缺字段置空 + 标记 incomplete。
_REPORT_INDEX_COLS = [
    "plan_id", "plan_run_id", "plan_name", "engine", "engine_data_schema",
    "schema_main_version", "status", "env_id", "env_name", "env_host", "started_at",
    "ended_at", "duration_ms", "comparator", "compare_key", "metrics", "summary",
    "metadata_incomplete", "extra", "created_at",
]


def upsert_report_index(db_path: str, row: dict) -> None:
    """写入一行报告索引（job_id 维度 upsert）。不触碰事实源 reports/report.json。"""
    with _LOCK, get_conn(db_path) as c:
        keys = ["job_id"] + [k for k in _REPORT_INDEX_COLS if k in row]
        vals = [row[k] for k in keys]
        c.execute(
            f"INSERT OR REPLACE INTO report_index({','.join(keys)}) VALUES({','.join('?' for _ in keys)})",
            vals,
        )
        c.commit()


def list_report_index(db_path: str, filters: Optional[dict] = None,
                      page: int = 1, page_size: int = 20) -> dict:
    """分页查询报告索引。filters: plan_id/engine/status/env_name, time_from/time_to。
    返回 {total, page, page_size, rows}。"""
    filters = filters or {}
    where, args = [], []
    if filters.get("plan_id"):
        where.append("plan_id=?"); args.append(int(filters["plan_id"]))
    if filters.get("engine"):
        where.append("engine=?"); args.append(str(filters["engine"]))
    if filters.get("status"):
        where.append("status=?"); args.append(str(filters["status"]))
    if filters.get("env_name"):
        where.append("env_name=?"); args.append(str(filters["env_name"]))
    if filters.get("time_from"):
        where.append("started_at>=?"); args.append(str(filters["time_from"]))
    if filters.get("time_to"):
        where.append("started_at<=?"); args.append(str(filters["time_to"]))
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    page = max(1, int(page)); page_size = min(200, max(1, int(page_size)))
    with _LOCK, get_conn(db_path) as c:
        total = c.execute(f"SELECT COUNT(*) FROM report_index {where_sql}", args).fetchone()[0]
        rows = [dict(r) for r in c.execute(
            f"SELECT * FROM report_index {where_sql} ORDER BY started_at DESC, id DESC "
            f"LIMIT ? OFFSET ?", args + [page_size, (page - 1) * page_size])]
    for r in rows:
        r["metrics"] = json.loads(r["metrics"]) if r["metrics"] else {}
        r["summary"] = json.loads(r["summary"]) if r["summary"] else {}
        r["compare_key"] = json.loads(r["compare_key"]) if r["compare_key"] else {}
    return {"total": total, "page": page, "page_size": page_size, "rows": rows}


def get_report_index(db_path: str, job_id: int) -> Optional[dict]:
    with _LOCK, get_conn(db_path) as c:
        r = c.execute("SELECT * FROM report_index WHERE job_id=?", (job_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else {}
        d["summary"] = json.loads(d["summary"]) if d["summary"] else {}
        d["compare_key"] = json.loads(d["compare_key"]) if d["compare_key"] else {}
        return d


def report_index_options(db_path: str) -> dict:
    """报告中心筛选下拉：可用计划/引擎/环境/状态。仅统计已成功聚合进索引的报告。"""
    with _LOCK, get_conn(db_path) as c:
        plans = [dict(r) for r in c.execute(
            "SELECT plan_id AS id, plan_name AS name, COUNT(*) AS n FROM report_index "
            "WHERE plan_name IS NOT NULL GROUP BY plan_id ORDER BY plan_name")]
        engines = [r[0] for r in c.execute(
            "SELECT DISTINCT engine FROM report_index ORDER BY engine")]
        envs = [dict(r) for r in c.execute(
            "SELECT env_name AS name, COUNT(*) AS n FROM report_index "
            "WHERE env_name IS NOT NULL GROUP BY env_name ORDER BY env_name")]
        statuses = [r[0] for r in c.execute(
            "SELECT DISTINCT status FROM report_index ORDER BY status")]
    return {"plans": plans, "engines": engines, "envs": envs, "statuses": statuses}


def report_index_history(db_path: str, comparator: str,
                         limit: int = 30) -> list[dict]:
    """同一可比性分组下的历史成功报告（按开始时间升序），用于趋势。"""
    rows = []
    with _LOCK, get_conn(db_path) as c:
        if comparator:
            rs = c.execute(
                "SELECT * FROM report_index WHERE comparator=? AND status='success' "
                "ORDER BY started_at ASC, id ASC LIMIT ?", (comparator, limit))
        else:
            rs = c.execute(
                "SELECT * FROM report_index WHERE status='success' "
                "ORDER BY started_at ASC, id ASC LIMIT ?", (limit,))
        for r in rs:
            d = dict(r)
            d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else {}
            rows.append(d)
    return rows


# ---------- 执行机独占锁（UI/Unity 类任务预留） ----------
def get_active_executor_lock(db_path: str, executor_id: int) -> Optional[dict]:
    """返回该执行机当前未释放的独占锁（含持有 Job），无则 None。供计划预检做并发提示。"""
    with _LOCK, get_conn(db_path) as c:
        r = c.execute(
            "SELECT id,executor_id,holder_job_id,acquired_at FROM executor_locks "
            "WHERE executor_id=? AND released_at IS NULL ORDER BY id DESC LIMIT 1",
            (executor_id,),
        ).fetchone()
        return dict(r) if r else None


def acquire_executor_lock(db_path: str, executor_id: int, job_id: int) -> Optional[str]:
    """尝试为 exec→job 抢占执行机锁；成功返回锁 id，否则返回 None。全局串行下通常成功。"""
    with _LOCK, get_conn(db_path) as c:
        held = c.execute(
            "SELECT id FROM executor_locks WHERE executor_id=? AND released_at IS NULL LIMIT 1",
            (executor_id,),
        ).fetchone()
        if held:
            return None
        now = _now()
        cur = c.execute(
            "INSERT INTO executor_locks(executor_id,holder_job_id,acquired_at) VALUES(?,?,?)",
            (executor_id, job_id, now),
        )
        c.commit()
        return str(cur.lastrowid)


def release_executor_lock(db_path: str, lock_id: Optional[str]) -> None:
    if not lock_id:
        return
    with _LOCK, get_conn(db_path) as c:
        c.execute("UPDATE executor_locks SET released_at=? WHERE id=?", (_now(), int(lock_id)))
        c.commit()