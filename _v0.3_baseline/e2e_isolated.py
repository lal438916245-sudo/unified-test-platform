"""v0.2 收尾 · 隔离实例端到端验证。

隔离铁律（因 2026-10-01 的数据安全偏差而立）：
  1) 先 socket.bind(0) 取一个**空闲端口**，并确认它当前不可达；
  2) 数据目录建在 %TEMP% 下的独立新目录，用 --data-dir 显式指定；
  3) 启动后**断言自己那个进程仍存活**（若端口被遗留实例占用，run.py 会启动失败退出）
     —— 这一步正是上次事故缺失的闸门：只看"端口 UP"会把遗留实例误认成自己的；
  4) 断言隔离库与真实库的计划数一致（证明读到的是我复制的副本）；
  5) 结束后关闭进程、删临时目录，并逐字节核对**真实库 md5/mtime 未变**。

只跑平台自包含 demo（backend/demo/test_platform_demo.py），不连任何外部服务。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.environ.get("PLATFORM_ROOT") or os.path.dirname(_HERE)   # 仓库根
REAL_DB = os.environ.get("PLATFORM_REAL_DB") or os.path.join(PLATFORM, "data", "db.sqlite")
PY = os.environ.get("PLATFORM_PY") or sys.executable                     # 用当前解释器

PASS = 0
FAIL = 0
FAILED = []


def ck(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ✗ {name}" + (f"\n      {detail}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def reachable(port: int, timeout: float = 0.6) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def db_md5(path: str) -> str:
    return hashlib.md5(open(path, "rb").read()).hexdigest()


def count(path: str, table: str) -> int:
    con = sqlite3.connect(f"file:{path.replace(chr(92), '/')}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def steps_raw(path: str, pid: int) -> str:
    con = sqlite3.connect(f"file:{path.replace(chr(92), '/')}?mode=ro", uri=True)
    try:
        return con.execute("SELECT steps FROM plans WHERE id=?", (pid,)).fetchone()[0]
    finally:
        con.close()


def main() -> int:
    base_md5 = db_md5(REAL_DB)
    base_mtime = os.path.getmtime(REAL_DB)
    print("=" * 72)
    print(" 0. 隔离仪式")
    print("=" * 72)
    print(f"  真实库基线 md5 = {base_md5}")

    PORT = free_port()
    ck(f"取得的端口 {PORT} 当前空闲（不可达）", not reachable(PORT))
    TMP = tempfile.mkdtemp(prefix="pe_e2e_suite_")
    temp_root = os.path.abspath(tempfile.gettempdir()).lower()
    ck("数据目录位于系统临时目录内", os.path.abspath(TMP).lower().startswith(temp_root), TMP)
    shutil.copy2(REAL_DB, os.path.join(TMP, "db.sqlite"))
    ck("已把真实库副本放入隔离数据目录（真实库只读复制）", os.path.isfile(os.path.join(TMP, "db.sqlite")))

    log = open(os.path.join(TMP, "run.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [PY, os.path.join("backend", "run.py"), "--port", str(PORT),
         "--data-dir", TMP, "--no-browser"],
        cwd=PLATFORM, stdout=log, stderr=subprocess.STDOUT, env=dict(os.environ))

    B = f"http://127.0.0.1:{PORT}/api"

    def call(method, path, body=None, timeout=120):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(B + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            # 带上响应体，避免只看到一个 400/409 而不知原因
            raise RuntimeError(f"{method} {path} -> HTTP {e.code}: "
                               f"{e.read()[:300].decode('utf-8', 'replace')}") from None

    def raw_call(method, path, body=None, timeout=60):
        """不抛异常的调用：返回 (status_code, body)。用于断言 4xx 行为。"""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(B + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:300].decode("utf-8", "replace")

    try:
        up = False
        for _ in range(80):
            if reachable(PORT):
                up = True
                break
            time.sleep(0.5)
        ck("隔离实例已监听", up)
        if not up:
            raise SystemExit("实例未就绪")
        # ★ 关键：确认在服务的就是**我们自己启的进程**
        ck("服务进程是我们自己启动的那个（未因端口被占而退出）", proc.poll() is None,
           f"proc.poll()={proc.poll()}（非 None 说明端口被遗留实例占用）")
        ck("隔离库是本次复制的那份（计划数一致）",
           count(os.path.join(TMP, "db.sqlite"), "plans") == 15,
           f"plans={count(os.path.join(TMP, 'db.sqlite'), 'plans')}")

        print()
        print("=" * 72)
        print(" A. Suite CRUD（隔离库）")
        print("=" * 72)
        tc = call("POST", "/test-cases", {
            "name": "e2e-demo-case", "description": "", "engine": "pytest",
            "params": {"cwd_root": "backend-demo",
                       "args": ["demo/test_platform_demo.py", "-o", "addopts=", "--tb=short"],
                       "timeout_sec": 120},
            "requires_exclusive": False, "asset_kind": ""})
        ck("创建 TestCase", tc.get("id") is not None and tc["revision"] == 1, json.dumps(tc)[:200])
        ref = {"id": tc["id"], "revision": tc["revision"], "fingerprint": tc["fingerprint"]}

        su = call("POST", "/suites", {"name": "e2e-suite", "description": "收尾验证",
                                      "cases": [ref], "owner": "local", "tags": ["e2e"]})
        ck("创建 Suite（revision=1）", su["revision"] == 1, json.dumps(su)[:200])
        ck("Suite cases 原样规范化往返", su["cases"] == [ref], json.dumps(su["cases"]))
        ck("Suite 不接受执行作用域字段",
           raw_call("POST", "/suites", {"name": "x", "cases": [], "environment_id": 1})[0] == 400)

        su2 = call("PUT", f"/suites/{su['id']}", {
            "name": "e2e-suite-renamed", "description": "改定义", "cases": [ref],
            "owner": "local", "tags": ["e2e"]})
        ck("改定义字段 → revision 2", su2["revision"] == 2, f"rev={su2['revision']}")
        su3 = call("PUT", f"/suites/{su['id']}", {
            "name": "e2e-suite-renamed", "description": "只改描述", "cases": [ref],
            "owner": "local", "tags": ["e2e", "x"]})
        ck("仅改描述/标签 → revision 不变（仍 2）", su3["revision"] == 2, f"rev={su3['revision']}")
        ck("指纹因定义未变而保持", su3["fingerprint"] == su2["fingerprint"],
           f"{su2['fingerprint']} vs {su3['fingerprint']}")

        print()
        print("=" * 72)
        print(" B. TestCase 删除保护（Suite 引用）")
        print("=" * 72)
        r = raw_call("DELETE", f"/test-cases/{tc['id']}")
        ck("被 Suite 引用时删除 TestCase → 409", r[0] == 409, str(r))

        print()
        print("=" * 72)
        print(" C. case_ref 计划：混合步骤 → 展开 → 预检 → 执行 → 报告")
        print("=" * 72)
        mixed = [
            {"case_ref": dict(ref), "override_params": {}},
            {"engine": "pytest", "name": "inline-demo", "requires_exclusive": False,
             "params": {"cwd_root": "backend-demo",
                        "args": ["demo/test_platform_demo.py", "-o", "addopts=", "--tb=short"],
                        "timeout_sec": 120}},
        ]
        # env 用 offline-eval（id=2）：它是"不适用"型环境，预检返回 ok=null，不做连通性探测。
        # 真实库里的 id=1 是 Colyseus 环境（localhost:2567），未启动时预检会如实报"不可达"。
        plan = call("POST", "/plans", {
            "name": "e2e-mixed", "description": "", "environment_id": 2,
            "executor_id": 1, "asset_source_id": 0, "fail_fast": True, "owner": "local",
            "steps": mixed})
        pid = plan["id"]
        raw = steps_raw(os.path.join(TMP, "db.sqlite"), pid)
        ck("存储的 steps 含 case_ref", '"case_ref"' in raw, raw[:200])
        ck("存储的 steps 不含 suite_ref", '"suite_ref"' not in raw, raw[:200])
        case_stored = json.loads(raw)[0]
        ck("case_ref 步骤不泄漏 engine/params",
           "engine" not in case_stored and "params" not in case_stored, json.dumps(case_stored))

        pc = call("POST", f"/plans/{pid}/precheck")
        ck("预检 ok 且零副作用", pc["ok"] and pc["no_side_effects"],
           json.dumps(pc, ensure_ascii=False)[:900])
        ck("两步均 ready", [s["ready"] for s in pc["steps"]] == [True, True])

        out = call("POST", f"/plans/{pid}/run")
        rid = out["run_id"]
        TERM = {"success", "failed", "cancelled", "timedout", "skipped"}
        t0 = time.time()
        run = None
        while time.time() - t0 < 180:
            run = call("GET", f"/runs/{rid}")["run"]
            if run["status"] in TERM:
                break
            time.sleep(1.0)
        ck(f"执行到达终态（{run['status']}）", run["status"] in TERM, json.dumps(run)[:200])
        ck("PlanRun success（平台链路成功）", run["status"] == "success", run["status"])
        jobs = call("GET", f"/runs/{rid}")["jobs"]
        ck("产生 2 个 Job（1 个来自用例展开 + 1 个内联）", len(jobs) == 2, json.dumps(jobs)[:300])
        ck("两个 Job 均 success", all(j["status"] == "success" for j in jobs),
           json.dumps([(j["engine"], j["status"]) for j in jobs]))
        # report_path 是**相对隔离数据目录**的路径（数据目录即 TMP）
        rp = jobs[0].get("report_path") or ""
        rp_abs = rp if os.path.isabs(rp) else os.path.join(TMP, rp)
        ck("report.json 已生成且可读", os.path.isfile(rp_abs), f"{rp} → {rp_abs}")
        if os.path.isfile(rp_abs):
            rep = json.load(open(rp_abs, encoding="utf-8"))
            ck("report.json 有 schema/engine/status",
               all(k in rep for k in ("schema_version", "engine", "status")), str(list(rep))[:160])
        rc = call("GET", "/report-center?page=1&page_size=5")
        ck("Report Center 已建立索引", rc.get("total", 0) >= 2, f"total={rc.get('total')}")

        print()
        print("=" * 72)
        print(" D. 15 个 legacy 计划：PUT 原样回存 → steps 逐字节一致")
        print("=" * 72)
        same = 0
        fp_changed = []
        for i in range(1, 16):
            before = call("GET", f"/plans/{i}")
            raw_b = steps_raw(os.path.join(TMP, "db.sqlite"), i)
            call("PUT", f"/plans/{i}", {
                "name": before["name"], "description": before["description"] or "",
                "environment_id": before["environment_id"], "executor_id": before["executor_id"],
                "asset_source_id": before["asset_source_id"] or 0,
                "fail_fast": bool(before["fail_fast"]), "owner": before["owner"] or "local",
                "steps": before["steps"]})
            raw_a = steps_raw(os.path.join(TMP, "db.sqlite"), i)
            if raw_b == raw_a:
                same += 1
            else:
                ck(f"plan#{i} steps 逐字节一致", False, f"\n      前={raw_b[:160]}\n      后={raw_a[:160]}")
            fp_changed.append((i, call("GET", f"/plans/{i}")["revision"]))
        ck(f"15/15 legacy 计划 steps 逐字节一致（实测 {same}/15）", same == 15)
        ck("PUT 后 revision 均 +1（既有版本化设计，非本批引入）",
           all(rev == 2 for _, rev in fp_changed), str(fp_changed))

        print()
        print("=" * 72)
        print(" E. 真实库安全（过程中持续核对）")
        print("=" * 72)
        ck("此刻真实库 md5 未变", db_md5(REAL_DB) == base_md5)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        log.close()
        time.sleep(1.0)
        ck("隔离实例已关闭（端口释放）", not reachable(PORT, 0.8))
        for _ in range(12):
            shutil.rmtree(TMP, ignore_errors=True)
            if not os.path.exists(TMP):
                break
            time.sleep(0.5)
        ck("临时数据目录已删除", not os.path.exists(TMP), TMP)

    print()
    print("=" * 72)
    print(" F. 收尾：真实库逐字节核对")
    print("=" * 72)
    ck("真实库 md5 未变", db_md5(REAL_DB) == base_md5, db_md5(REAL_DB))
    ck("真实库 mtime 未变", os.path.getmtime(REAL_DB) == base_mtime)
    for t, want in (("plans", 15), ("plan_runs", 34), ("jobs", 41), ("reports", 39),
                    ("report_index", 39)):
        got = count(REAL_DB, t)
        ck(f"真实库 {t} 计数未变（{want}）", got == want, f"实测 {got}")
    con = sqlite3.connect(f"file:{REAL_DB.replace(chr(92), '/')}?mode=ro", uri=True)
    tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    con.close()
    ck("真实库未出现 test_suites 表（未启动过本批代码）", "test_suites" not in tabs, str(tabs))

    print()
    print("=" * 72)
    print(f" 结果: {PASS} passed, {FAIL} failed")
    if FAILED:
        print(" 失败项: " + json.dumps(FAILED, ensure_ascii=False, indent=1))
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
