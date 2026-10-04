"""GET /health —— 最小存活探针（零副作用）。

存在意义：CI（self-hosted Windows runner）启动 backend 后需要一个**不触碰数据库、
不依赖任何被测服务**的就绪判据。本机 `netstat` / `tasklist` 都会静默返回空，
无法用来判断端口是否就绪，只能靠 HTTP 探活。

因此本文件不仅要测"返回 200 ok"，还必须证明「探针本身不产生任何副作用」——
否则它就不配当 CI 的就绪信号。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import build_app  # noqa: E402

_PLATFORM = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # platform
_STATIC = os.path.join(_PLATFORM, "frontend")


def test_health_returns_ok():
    with TestClient(build_app(_STATIC)) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_is_served_at_root_not_under_api_prefix():
    """探针挂在根路径，不经业务 router（/api/*），因此不继承其依赖链。"""
    with TestClient(build_app(_STATIC)) as c:
        assert c.get("/health").status_code == 200
        # 明确记录：它**不是** /api/health
        assert c.get("/api/health").status_code == 404


def test_health_has_no_side_effect_on_database(tmp_path):
    """即使显式提供了 DB 路径，/health 也不得创建或读写它。"""
    db_file = tmp_path / "must_not_be_created.sqlite"
    app = build_app(_STATIC)
    app.state.db_path = str(db_file)
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
    assert not db_file.exists(), "/health 触碰了数据库（应为零副作用）"


def test_health_works_without_db_path_configured():
    """未配置 db_path 也应可用 —— 证明它不依赖 DB / 调度器 / 被测资产 / 外部服务。"""
    app = build_app(_STATIC)          # 刻意不设 app.state.db_path
    with TestClient(app) as c:
        r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
