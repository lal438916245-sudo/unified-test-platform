"""CI 第一阶段 L1：平台「真实启动 + Demo Plan 执行」端到端冒烟。

与 L0（纯代码级单测）的分工：
    L0 证明"代码是对的"；L1 证明"把平台真的跑起来、真的执行一个计划、真的产出报告"。

============================ 隔离与自证契约 ============================
本脚本每次执行都必须能自己证明下面三件事（这正是 2026-10-01 那次数据安全
偏差缺失的闸门 —— 当时只看"端口 UP"，把遗留实例误认成自己启动的实例）：

    1. Platform PID      == 本脚本 Popen 出来的那个 PID
    2. Platform Port     == 本脚本 socket.bind(0) 动态取到的端口（绝不是 8000）
    3. Platform Data Dir == 本脚本创建的临时目录（绝不是 platform/data）

自证手段（不靠猜）：
    * 启动前断言端口不可达 → 起完后能连上，说明这个端口是本次新起的；
    * 读取**子进程自己 stdout** 打印的 `http://127.0.0.1:<port>` / `数据库:` / `数据目录:`，
      这是被启动进程亲口声明的绑定信息，而非我们的假设；
    * 执行结束后断言：临时库里 plan_runs 有记录，而**真实库计数逐字节不变**。

============================ 外部依赖 ============================
只跑 `backend/demo/test_platform_demo.py`（自包含，无网络/无服务端/无 MongoDB/
无 Colyseus / 无 Locust / 无 MatchEval / 无真实游戏素材）。

退出码：0 = 全部通过；1 = 任一断言失败（CI 据此判红，绝不用 continue-on-error 掩盖）。
用法：
    python _ci/l1_smoke.py [--keep]
"""
from __future__ import annotations

import argparse
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

# 本脚本正文含中文以及 `⇒`(U+21D2)、`↔`(U+2194) 等符号。CI 里 stdout 是管道，
# Python 会退回系统 locale 编码（中文 Windows = cp936/GBK），而 **GBK 编不出这两个符号**
# → print 抛 UnicodeEncodeError、脚本崩在收尾那一步（CI 上真实发生过，本机用
# PYTHONIOENCODING=gbk 可一比一复现）。这里显式把自身两个流设成 UTF-8：
# 无论控制台/父进程是什么代码页都不会再崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001  reconfigure 需 3.7+，极老解释器忽略即可
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(_HERE)                                   # platform
RUN_PY = os.path.join(PLATFORM, "backend", "run.py")
REAL_DB = os.path.join(PLATFORM, "data", "db.sqlite")
REAL_DATA = os.path.join(PLATFORM, "data")
CI_OUT = os.path.join(PLATFORM, "_ci_out")                          # 已 gitignore

PYTHON = os.environ.get("PLATFORM_CI_PYTHON") or sys.executable

PASS = 0
FAIL = 0
FAILED: list[str] = []

# 命令行开关。定义在模块级（而非只在 __main__ 里赋值），这样 import 本模块后
# 直接调用 main() 也能工作 —— 便于把「全新签出」等分支写成自动化测试。
KEEP = False

# 平台自身可能处于 http_proxy 之后：显式禁用代理，否则 127.0.0.1 会被送去代理（502）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ck(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}")
        if detail:
            print(f"         {detail}")
    return ok


def call(method: str, path: str, body=None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api{path}", data=data,
                                 method=method, headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        raw = r.read().decode()
        try:
            return json.loads(raw)
        except Exception:
            return raw


def get_health(timeout: float = 3.0):
    try:
        with _OPENER.open(f"http://127.0.0.1:{PORT}/health", timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def reachable(port: int, timeout: float = 0.8) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def free_port() -> int:
    """向内核要一个当前空闲的端口，并立刻复验它确实不可达。"""
    for _ in range(20):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if not reachable(p):
            return p
    raise RuntimeError("无法取到空闲端口")


def snapshot_data_dir() -> dict | None:
    """真实 platform/data 的指纹（md5 + mtime + 文件数 + plan_runs 计数）。

    CI 前后必须完全一致。若真实库不存在（例如全新 runner），返回 None，
    调用方跳过比对 —— 不把"文件不存在"误报成"被改动"。
    """
    if not os.path.isfile(REAL_DB):
        return None
    b = open(REAL_DB, "rb").read()
    n = sum(len(fs) for _, _, fs in os.walk(REAL_DATA))
    con = sqlite3.connect(f"file:{REAL_DB.replace(chr(92), '/')}?mode=ro", uri=True)
    runs = con.execute("SELECT COUNT(*) FROM plan_runs").fetchone()[0]
    con.close()
    return {"md5": hashlib.md5(b).hexdigest(), "size": len(b),
            "mtime": os.path.getmtime(REAL_DB), "files": n, "runs": runs}


def tail(path: str, n: int = 25) -> str:
    try:
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:  # noqa: BLE001
        return f"(日志不可读: {e})"


def pid_alive(pid: int) -> bool:
    """OS 级判断进程是否仍存活。

    刻意不用 `tasklist` —— 本机 `tasklist` 会**静默返回空**，把"在跑"误判成"没跑"。
    这里直接走 Windows API：OpenProcess + GetExitCodeProcess == STILL_ACTIVE。
    """
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        code = wintypes.DWORD()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return bool(ok) and code.value == STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return False


def _powershell() -> str | None:
    for c in (shutil.which("powershell"), shutil.which("powershell.exe"),
              r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"):
        if c and os.path.exists(c):
            return c
    return None


def _ps_to_file(script: str) -> str:
    """PowerShell 在本机 stdout 不回显 → 一律落文件再读（既有踩坑结论）。"""
    ps = _powershell()
    if not ps:
        return ""
    out = os.path.join(TMP, "_ps_out.txt")
    try:
        subprocess.run([ps, "-NoProfile", "-Command",
                        f'{script} | Out-File -FilePath "{out}" -Encoding utf8'],
                       capture_output=True, text=True, timeout=60)
        return open(out, encoding="utf-8-sig").read().strip()
    except Exception:  # noqa: BLE001
        return ""


def leftover_pids(marker: str) -> list[str] | None:
    """按**本次运行唯一标记**（临时目录名）精确匹配残留进程。

    返回 None 表示本机无 PowerShell、无法扫描（由调用方按 skip 处理，不当作失败）——
    这样既不误伤开发机自身的平台进程，也不会因工具缺失而假绿/假红。
    """
    if not _powershell():
        return None
    txt = _ps_to_file(
        # 只匹配 python.exe 且排除 PowerShell 自身 —— 否则查询命令里的 marker 会让
        # powershell.exe 自己命中自己（本脚本首版就踩了这个自匹配）。
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like \"*%s*\" } | "
        "ForEach-Object { $_.ProcessId }" % marker)
    return [x for x in txt.split() if x.strip().isdigit()]


def dump_artifacts(proc, log_path: str, report_dir: str | None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(CI_OUT, stamp)
    os.makedirs(dest, exist_ok=True)
    shutil.copy2(log_path, os.path.join(dest, "platform-stdout.log")) if os.path.isfile(log_path) else None
    if report_dir and os.path.isdir(report_dir):
        shutil.copytree(report_dir, os.path.join(dest, "job_report"), dirs_exist_ok=True)
    meta = {"platform": PLATFORM, "python": PYTHON, "port": PORT, "data_dir": TMP,
            "db": DB_PATH, "spawned_pid": getattr(proc, "pid", None),
            "pass": PASS, "fail": FAIL, "failed_checks": FAILED,
            "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(os.path.join(dest, "ci-meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return dest


def main() -> int:
    global PORT, TMP, DB_PATH

    print("=" * 74)
    print(" CI 第一阶段 L1：平台真实启动 + Demo Plan 端到端冒烟")
    print("=" * 74)
    print(f"  PLATFORM = {PLATFORM}")
    print(f"  PYTHON   = {PYTHON}")

    before = snapshot_data_dir()
    if before:
        print(f"  [基线] 真实库 md5={before['md5'][:12]}… mtime="
              f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(before['mtime']))} "
              f"files={before['files']} plan_runs={before['runs']}")
        ck("真实库存在（用于 CI 前后比对）", True)
    else:
        print("  [基线] 真实库不存在 → 跳过前后比对（全新 runner 场景）")

    PORT = free_port()
    TMP = tempfile.mkdtemp(prefix="platform_ci_l1_")
    DB_PATH = os.path.join(TMP, "db.sqlite")
    MARKET = os.path.basename(TMP)
    log_path = os.path.join(TMP, "platform-stdout.log")

    print(f"  [隔离] 动态端口={PORT}（≠8000）· 数据目录={TMP} · 唯一标记={MARKET}")
    assert PORT != 8000, "动态端口不得为 8000"
    print()

    # ---------------- 1) 启动前：端口必须不可达 ----------------
    print("--- 1. 启动前环境检查 ---")
    ck(f"端口 {PORT} 启动前不可达（证明是新起的，不是遗留实例）", not reachable(PORT))
    if before is not None:
        ck("真实库存在（用于前后比对）", os.path.isfile(REAL_DB), REAL_DB)
    else:
        # 全新 runner / 新签出：仓库里没有 data/（已 gitignore），本就不该有真实库。
        # 这里必须[跳过]而不是[FAIL] —— 否则 CI 上这条恒红，且掩盖真正的断言。
        print("  [跳过] 全新签出：workspace 内无 data/db.sqlite，本轮不做真实库前后比对"
              "（与启动前快照 = None 一致）")

    child_env = os.environ.copy()
    # 让子进程自己就把"执行机解释器"预置成当前解释器 —— CI 不依赖任何本机绝对路径
    child_env["PLATFORM_PYTHON"] = PYTHON
    child_env["PLATFORM_DEFAULT_PYTHON"] = PYTHON
    child_env["PLATFORM_DATA_DIR"] = TMP
    child_env["PLATFORM_DB"] = DB_PATH
    child_env["NO_PROXY"] = child_env["no_proxy"] = "127.0.0.1,localhost"
    # 关键：子进程 stdout 重定向到文件时 Python 默认**块缓冲**，run.py 启动横幅
    # （绑定端口/数据库/数据目录）会滞留在缓冲区里读不到 → 强制无缓冲，
    # 这样"子进程亲口声明它绑到哪"才能在 /health 就绪时就被读到。
    child_env["PYTHONUNBUFFERED"] = "1"

    argv = [PYTHON, RUN_PY, "--host", "127.0.0.1", "--port", str(PORT),
            "--data-dir", TMP, "--db", DB_PATH, "--no-browser"]
    print(f"\n  启动: {' '.join(argv)}")

    proc = None
    report_dir = None
    try:
        with open(log_path, "wb") as logf:
            proc = subprocess.Popen(argv, cwd=PLATFORM, env=child_env,
                                    stdout=logf, stderr=subprocess.STDOUT)
        print(f"  子进程 PID = {proc.pid}")

        # ---------------- 2) 等待 /health（同时监视进程是否已死）----------------
        print("\n--- 2. 等待 /health 就绪 ---")
        t0 = time.time()
        status = None
        while time.time() - t0 < 120:
            if proc.poll() is not None:
                ck("backend 进程存活", False,
                   f"进程提前退出，exit={proc.returncode}\n{tail(log_path)}")
                return 1
            code, body = get_health(2.0)
            if code == 200 and isinstance(body, dict) and body.get("status") == "ok":
                status = (code, body)
                break
            time.sleep(0.5)

        if not ck("GET /health 在 120s 内返回 200", status is not None,
                  f"未就绪；日志尾部：\n{tail(log_path)}"):
            return 1
        ck('GET /health 载荷 == {"status": "ok"}', status[1] == {"status": "ok"}, repr(status[1]))
        print(f"  就绪耗时 {time.time() - t0:.1f}s")

        # ---------------- 3) 自证：PID ↔ 端口 ↔ 数据目录 ----------------
        print("\n--- 3. 身份自证（PID / 端口 / 数据目录）---")
        ck("本次 PID 仍存活（不是遗留实例在应答）", pid_alive(proc.pid),
           f"poll={proc.poll()}")
        ck("端口由内核动态分配且刻意避开 8000（不撞开发机默认端口）",
           1024 < PORT < 65536 and PORT != 8000, str(PORT))
        own_dump = _ps_to_file(
            "Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" | "
            "ForEach-Object { $_.CommandLine }" % proc.pid)
        if own_dump:
            ck("OS 报告的该 PID 命令行正是本次 run.py + 本次端口",
               "run.py" in own_dump and str(PORT) in own_dump, own_dump[:300])
            ck("该 PID 命令行含本次临时数据目录", MARKET in own_dump, own_dump[:300])
        else:
            print("  [跳过] 本机无 PowerShell：改用\"端口归属\"闭合证明（见收尾的端口释放断言）")

        logtext = open(log_path, encoding="utf-8", errors="replace").read()
        ck("子进程自报绑定了本次端口", f"http://127.0.0.1:{PORT}" in logtext,
           f"日志中未找到 http://127.0.0.1:{PORT}")
        ck("子进程自报数据库为本次临时库", DB_PATH in logtext)
        ck("子进程自报数据目录为本次临时目录", TMP in logtext)
        ck("隔离库已建在临时目录", os.path.isfile(DB_PATH))
        ck("端口 ≠ 8000", PORT != 8000, str(PORT))

        # ---------------- 4) 创建 CI Demo Plan ----------------
        print("\n--- 4. 创建 CI Demo Plan（offline-eval / 自包含 pytest）---")
        envs = call("GET", "/environments")
        execs = call("GET", "/executors")
        env = next((e for e in envs if e["name"] == "offline-eval"), None)
        ex = next((x for x in execs if "pytest" in (x.get("allowed_runners") or [])), None)
        if not ck("找到 offline-eval 环境与支持 pytest 的执行机", env and ex,
                  f"envs={[e['name'] for e in envs]} execs={[x['name'] for x in execs]}"):
            return 1
        print(f"  Environment={env['name']}(id={env['id']})  Executor={ex['name']}(id={ex['id']})")

        plan_body = {
            "name": "CI L1 Demo（自包含 pytest 冒烟）",
            "description": "CI 第一阶段 L1 专用：只跑 backend/demo 的自包含用例，零外部依赖。",
            "environment_id": env["id"], "executor_id": ex["id"], "asset_source_id": 0,
            "fail_fast": True, "owner": "ci",
            "steps": [{
                "engine": "pytest", "name": "ci-demo-smoke", "requires_exclusive": False,
                "params": {"timeout_sec": 120, "cwd_root": "backend-demo",
                           "args": ["demo/test_platform_demo.py", "-o", "addopts=", "--tb=short"]},
            }],
        }
        plan = call("POST", "/plans", plan_body)
        pid = plan["id"]
        print(f"  Plan id={pid}")

        pc = call("POST", f"/plans/{pid}/precheck")
        ck("预检 ok=True（配置与资产均就绪）", pc.get("ok") is True, json.dumps(pc)[:400])
        ck("预检零副作用 no_side_effects=True", pc.get("no_side_effects") is True)
        ck("预检 missing 为空", not pc.get("missing"), str(pc.get("missing")))
        ck("步骤 ready=True", [s.get("ready") for s in pc.get("steps", [])] == [True],
           json.dumps(pc.get("steps"))[:300])

        # ---------------- 5) 执行并等待终态 ----------------
        print("\n--- 5. 执行 PlanRun 并跟踪到终态 ---")
        run_id = call("POST", f"/plans/{pid}/run")["run_id"]
        print(f"  PlanRun id={run_id}")

        TERM = {"success", "failed", "cancelled", "timedout", "skipped"}
        run, jobs = None, []
        t0 = time.time()
        while time.time() - t0 < 300:
            d = call("GET", f"/runs/{run_id}")
            run, jobs = d["run"], d.get("jobs") or []
            if run["status"] in TERM and jobs and all(j["status"] in TERM for j in jobs):
                break
            if proc.poll() is not None:
                ck("执行期间 backend 存活", False, f"进程退出 exit={proc.returncode}")
                return 1
            time.sleep(1.0)

        ck(f"PlanRun 在 300s 内到达终态（{run['status']}）", run["status"] in TERM)
        ck("PlanRun status == success（平台链路成功）", run["status"] == "success", run["status"])
        ck("产生 1 个 Job", len(jobs) == 1, f"{len(jobs)} 个")
        if jobs:
            j = jobs[0]
            ck("Job status == success", j["status"] == "success", json.dumps(j)[:300])
            ck("Job exit_code == 0", j.get("exit_code") == 0, str(j.get("exit_code")))
            ck("Job engine == pytest", j.get("engine") == "pytest", str(j.get("engine")))

            rp = j.get("report_path") or ""
            rp_abs = rp if os.path.isabs(rp) else os.path.join(TMP, rp)
            report_dir = os.path.dirname(rp_abs) if rp_abs else None
            ck("report.json 已生成且位于本次临时数据目录内", os.path.isfile(rp_abs), rp_abs)
            ck("report.json 落在临时目录内（未写入真实 data）",
               os.path.abspath(rp_abs).startswith(os.path.abspath(TMP)), rp_abs)
            if os.path.isfile(rp_abs):
                rep = json.load(open(rp_abs, encoding="utf-8"))
                ck("report.json 结构完整（schema/engine/status/summary）",
                   all(k in rep for k in ("schema_version", "engine", "status", "summary")),
                   str(sorted(rep))[:200])
                ck("report.json.status == success", rep.get("status") == "success", str(rep.get("status")))

            # ---------------- 6) Report Center 索引 ----------------
            rc = call("GET", "/report-center?page=1&page_size=5")
            rows = rc.get("rows") or []
            ck("Report Center 已建立索引", rc.get("total", 0) >= 1, str(rc.get("total")))
            ck("索引中包含本次 Job", any(r.get("job_id") == j["id"] for r in rows),
               json.dumps(rows)[:300])

        # ---------------- 7) 写入去向自证：临时库有、真实库无 ----------------
        print("\n--- 6. 写入去向自证（关键：真实库绝不能被写）---")
        con = sqlite3.connect(f"file:{DB_PATH.replace(chr(92), '/')}?mode=ro", uri=True)
        tmp_runs = con.execute("SELECT COUNT(*) FROM plan_runs").fetchone()[0]
        tmp_jobs = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        con.close()
        ck("临时库记录了本次 PlanRun", tmp_runs == 1, f"tmp plan_runs={tmp_runs}")
        ck("临时库记录了本次 Job", tmp_jobs == 1, f"tmp jobs={tmp_jobs}")

        after = snapshot_data_dir()
        if before and after:
            ck("真实库 md5 未变", after["md5"] == before["md5"],
               f"{before['md5']} → {after['md5']}")
            ck("真实库 mtime 未变", after["mtime"] == before["mtime"],
               f"{time.strftime('%H:%M:%S', time.localtime(before['mtime']))} → "
               f"{time.strftime('%H:%M:%S', time.localtime(after['mtime']))}")
            ck("真实 data 目录文件数未变", after["files"] == before["files"],
               f"{before['files']} → {after['files']}")
            ck(f"真实库 PlanRun 未新增（仍为 {before['runs']}）", after["runs"] == before["runs"],
               f"{before['runs']} → {after['runs']}")
        else:
            print("  [跳过] 真实库不存在，未做前后比对")

        return 0 if FAIL == 0 else 1

    finally:
        # ---------------- 8) 收尾：无论成败都要清干净 ----------------
        print("\n--- 7. 收尾（关闭进程 + 清理临时目录）---")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except Exception:  # noqa: BLE001
                    pass
        ck("backend 进程已退出", proc is None or proc.poll() is not None,
           f"poll={None if proc is None else proc.poll()}")
        if proc is not None:
            ck("OS 级确认该 PID 已消亡（非 tasklist）", not pid_alive(proc.pid))

        left = leftover_pids(MARKET)
        if left is None:
            print("  [跳过] 本机无 PowerShell，未做残留进程扫描")
        else:
            ck("无残留 backend 进程（按本次唯一标记精确匹配）", not left, f"残留 PID={left}")

        released = False
        for _ in range(20):
            if not reachable(PORT):
                released = True
                break
            time.sleep(0.5)
        # 这条同时是"端口监听者就是本进程"的闭合证明：启动前不可达 → 运行期可达 → 进程退出后再次不可达
        ck(f"端口 {PORT} 已释放（⇒ 监听者确为本次进程）", released)

        dest = dump_artifacts(proc if proc else None, log_path, report_dir)
        print(f"  CI 日志与产物已归档 → {dest}")

        if not KEEP:
            for _ in range(10):
                shutil.rmtree(TMP, ignore_errors=True)
                if not os.path.exists(TMP):
                    break
                time.sleep(0.3)
        ck("临时数据目录已删除", KEEP or not os.path.exists(TMP), TMP)

        print("\n" + "=" * 74)
        print(f" L1 结果：{PASS} passed, {FAIL} failed")
        if FAILED:
            for n in FAILED:
                print(f"   - {n}")
        print("=" * 74)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录（排障用）")
    a = ap.parse_args()
    KEEP = a.keep
    sys.exit(main())
