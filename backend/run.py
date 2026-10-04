"""启动入口：初始化 SQLite → 预置数据 → 启动编排线程 → 起 Web 服务。

用法：
    python backend/run.py                 # 默认 127.0.0.1:8000
    python backend/run.py --port 8001
    python backend/run.py --no-browser
    python backend/run.py --data-dir <dir> [--db <file>]   # 隔离实例（独立数据目录/库）
环境变量：PLATFORM_PYTHON 覆盖 pytest 子进程用到的 Python；
          PLATFORM_LOCUST_PYTHON 覆盖 locust venv python。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser

# 在导入 scheduler（其模块级 DATA_DIR 在 import 时解析）之前，先把 CLI 的隔离参数落到环境变量
for _i, _a in enumerate(sys.argv):
    if _a == "--data-dir" and _i + 1 < len(sys.argv):
        os.environ["PLATFORM_DATA_DIR"] = os.path.abspath(sys.argv[_i + 1])
    if _a == "--db" and _i + 1 < len(sys.argv):
        os.environ["PLATFORM_DB"] = os.path.abspath(sys.argv[_i + 1])

import uvicorn

from app import db
from app.api import build_app
from app.orchestration.scheduler import DATA_DIR, Orchestrator
from preset import seed_if_empty

PLATFORM = os.path.dirname(os.path.abspath(__file__))          # backend
ROOT = os.path.dirname(PLATFORM)                                # platform
STATIC_DIR = os.path.join(ROOT, "frontend")
DB_PATH = os.environ.get("PLATFORM_DB") or os.path.join(DATA_DIR, "db.sqlite")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir", default=None, help="覆盖平台数据目录（日志/报告/DB）")
    parser.add_argument("--db", default=None, help="覆盖 SQLite 库文件路径")
    args = parser.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    db.init_db(DB_PATH)
    seed_if_empty(DB_PATH, self_url=f"http://{args.host}:{args.port}")

    # 报告中心读模型：启动时对既有报告一次性重建索引（兼容旧记录；不触碰事实源）
    try:
        from app.report_center import rebuild_report_index
        mig = rebuild_report_index(DB_PATH)
        print(f"[platform] 报告索引迁移：重建 {mig['rebuilt']} 条，不完整(旧记录) {mig['incomplete']} 条，跳过 {mig['skipped']} 条")
    except Exception as e:  # noqa: BLE001 —— 迁移失败不阻断启动，后续新报告仍会写索引
        print(f"[platform] 报告索引迁移失败(继续启动): {e}")

    orch = Orchestrator(DB_PATH)
    orch.start()

    app = build_app(STATIC_DIR)
    app.state.db_path = DB_PATH
    app.state.orchestrator = orch

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{args.host}:{args.port}")).start()

    print(f"\n[platform] 统一游戏测试平台 MVP 已启动: http://{args.host}:{args.port}")
    print(f"[platform] 数据库: {DB_PATH}")
    print(f"[platform] 数据目录: {DATA_DIR}\n")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        orch.shutdown()


if __name__ == "__main__":
    main()