"""Bounded read-only probes, executed with the selected executor's Python.

No pytest/conftest import, UI input, scene reset or database write is performed.
Only a summary is returned; credentials, database records and UI trees stay local.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import runpy
import sys
from datetime import datetime, timezone


def check(key, name, status, detail, next_step=""):
    return {
        "key": key,
        "name": name,
        "status": status,
        "ok": True if status == "passed" else False if status == "failed" else None,
        "detail": detail,
        "next_step": next_step,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def check_project(config, realtime=True):
    from .runtime_requirements import MODULES

    req = config.get("runtime_requirements") or {}
    rows = []
    for module in req.get("modules", []):
        try:
            if module not in MODULES:
                raise ValueError("Uncontrolled dependency")
            importlib.import_module(module)
        except Exception as exc:
            rows.append(
                check(
                    "module:" + module,
                    "项目依赖 · " + module,
                    "failed",
                    "无法导入（" + type(exc).__name__ + "）",
                    "在所选执行机的 Python 环境安装或修复该依赖",
                )
            )
        else:
            rows.append(check("module:" + module, "项目依赖 · " + module, "passed", "可导入"))
    if req.get("mongo_ref"):
        if not realtime:
            rows.append(
                check("mongodb", "MongoDB 连接与读取", "not_checked", "配置预检不连接数据库")
            )
        elif any(r["key"] == "module:pymongo" and r["ok"] is False for r in rows):
            rows.append(
                check("mongodb", "MongoDB 连接与读取", "not_checked", "先修复 pymongo 依赖")
            )
        else:
            client = None
            try:
                if req["mongo_ref"] != "gameautotest":
                    raise ValueError("Uncontrolled MongoDB source")
                # Server-owned config file, same source/defaults/env as the real fixture.
                from pathlib import Path

                cfg = runpy.run_path(str(Path(config["root"]) / "config.py"))
                from pymongo import MongoClient

                client = MongoClient(
                    cfg["MONGO_URI"],
                    serverSelectionTimeoutMS=3000,
                    connectTimeoutMS=3000,
                    socketTimeoutMS=3000,
                    timeoutMS=4000,
                )
                client.admin.command("ping")
                client[cfg["MONGO_DB_NAME"]][cfg["MONGO_USER_COLLECTION"]].find_one(
                    {}, {"_id": 1}, max_time_ms=2000
                )
                rows.append(
                    check(
                        "mongodb",
                        "MongoDB 连接与读取",
                        "passed",
                        "项目配置的数据库可连接、目标集合可读；未写入数据，未验证删除权限",
                    )
                )
            except Exception as exc:
                rows.append(
                    check(
                        "mongodb",
                        "MongoDB 连接与读取",
                        "failed",
                        "连接或读取失败（" + type(exc).__name__ + "）；详细连接信息不对外显示",
                        "检查项目 MongoDB 配置、服务、认证与目标集合读取权限",
                    )
                )
            finally:
                if client is not None:
                    client.close()
    return rows


def check_unity(config):
    """Read window/GameView and issue two protocol RPCs without Airtest input setup."""
    rows = []
    d = config["device_config"]
    try:
        if "title_match" in d:
            from .ui_windows import resolve_window

            target = resolve_window(d)
            hwnd = target["hwnd"]
        else:
            hwnd = int(d["uri"].removeprefix("Windows:///"))
            import win32gui

            if not win32gui.IsWindow(hwnd):
                raise RuntimeError("Target window unavailable")
            target = {"title": win32gui.GetWindowText(hwnd)}
        rows.append(
            check("window", "Unity 目标窗口", "passed", "唯一目标窗口可用：" + target["title"])
        )
    except Exception:
        return [
            check(
                "window",
                "Unity 目标窗口",
                "failed",
                "没有唯一可用目标；客户端可能未启动或窗口规则匹配多个目标",
                "启动目标客户端，并在 UI 设备中确认窗口匹配规则",
            ),
            check("gameview", "Unity GameView", "not_checked", "先修复目标窗口"),
            check("poco", "Poco 实时层级", "not_checked", "先修复目标窗口"),
        ]
    if d.get("unity_editor"):
        try:
            from pywinauto import Application

            # pywinauto 0.6.3 accepts timeout only for path-based attachment.
            # Handle attachment is immediate; keep the bounded wait on exists.
            app = Application(backend="win32").connect(handle=hwnd)
            exists = (
                app.window(handle=hwnd).child_window(title="UnityEditor.GameView").exists(timeout=1)
            )
            rows.append(
                check(
                    "gameview",
                    "Unity GameView",
                    "passed" if exists else "failed",
                    (
                        "GameView 存在；未聚焦、未改变 Play 或暂停状态"
                        if exists
                        else "GameView 不可用"
                    ),
                    "" if exists else "打开目标 Editor 的 Game 视图",
                )
            )
        except Exception as exc:
            rows.append(
                check(
                    "gameview",
                    "Unity GameView",
                    "failed",
                    "GameView 探测未完成（" + type(exc).__name__ + "）",
                    "检查执行机的 pywinauto 依赖与兼容性；此结果不代表 GameView 一定不存在",
                )
            )
    else:
        rows.append(
            check(
                "gameview", "Unity GameView", "not_applicable", "独立客户端不要求 Editor GameView"
            )
        )
    rpc = None
    transport = None
    connected = False
    try:
        from poco.utils.simplerpc.rpcclient import RpcClient
        from poco.utils.simplerpc.transport.tcp.main import TcpClient

        transport = TcpClient((d["host"], int(d["port"])))
        rpc = RpcClient(transport)
        rpc.connect()
        connected = True
        version, error = rpc.call("GetSDKVersion").wait(timeout=3)
        if error or version is None:
            raise RuntimeError("SDK handshake failed")
        tree, error = rpc.call("Dump").wait(timeout=5)
        if error or not isinstance(tree, dict) or not tree:
            raise RuntimeError("Hierarchy unavailable")
        rows.append(
            check("poco", "Poco 实时层级", "passed", "SDK 握手成功且 UI 层级可读；未点击或填写控件")
        )
    except Exception as exc:
        rows.append(
            check(
                "poco",
                "Poco 实时层级",
                "failed",
                "连接、握手或层级读取失败（" + type(exc).__name__ + "）",
                "确认 Unity 处于 Play 模式、未暂停，且 Poco SDK 地址和端口正确",
            )
        )
    finally:
        if rpc is not None:
            try:
                rpc.close()
                rows.append(
                    check(
                        "rpc_cleanup",
                        "Poco 探测连接回收",
                        "passed" if connected else "not_applicable",
                        "RPC 已关闭" if connected else "未建立 RPC 连接；本地资源已释放",
                    )
                )
            except Exception:
                # SDK's close calls shutdown before close, which raises on a
                # refused/unconnected socket. Ensure the local socket is closed.
                released = False
                if not connected:
                    try:
                        sock = getattr(getattr(transport, "c", None), "sock", None)
                        if sock is not None:
                            sock.close()
                        released = True
                    except Exception:
                        pass
                rows.append(
                    check(
                        "rpc_cleanup",
                        "Poco 探测连接回收",
                        "not_applicable" if released else "failed",
                        (
                            "未建立 RPC 连接；本地资源已释放"
                            if released
                            else "RPC 关闭未确认；请检查客户端后再执行"
                        ),
                    )
                )
    return rows


def main():
    config = json.load(sys.stdin)
    # Third party RPC libraries print connection logs; never return them or raw errors.
    with contextlib.redirect_stdout(sys.stderr):
        rows = check_project(config, config.get("realtime", True))
        if config.get("probe_ui"):
            rows += check_unity(config)
    print(json.dumps({"checks": rows}, ensure_ascii=False))


if __name__ == "__main__":
    main()
