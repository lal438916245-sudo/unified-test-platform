"""UI subprocess entry point and pytest evidence plugin.

Prepare before importing the external project's conftest. Metadata is flushed
at each phase so cancellation/timeout retains the last known phase.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path


class UIPlugin:
    def __init__(self, output, config):
        self.output = Path(output)
        self.config = config
        self.meta = {
            "phase": "prepare",
            "failure_kind": None,
            "cases": [],
            "evidence_errors": [],
            "error": None,
        }
        self.poco = None
        self.flush()

    def close(self):
        """Release the owned RPC once; cleanup errors never replace test errors."""
        if "connection_cleanup" in self.meta:
            return
        cleanup = {"attempted": self.poco is not None, "closed": False}
        self.meta["connection_cleanup"] = cleanup
        if self.poco is None:
            cleanup["state"] = "not_connected"
            return
        try:
            self.poco.agent.rpc.close()
        except Exception as exc:
            cleanup["state"] = "failed"
            warning = f"Poco RPC 关闭失败（{type(exc).__name__}）；原测试结果保留"
            self.meta.setdefault("cleanup_errors", []).append(warning)
            try:
                (self.output / "cleanup-trace.txt").write_text(
                    traceback.format_exc(), encoding="utf-8"
                )
            except OSError:
                pass
            message = "[warn] " + warning
        else:
            cleanup.update(state="closed", closed=True)
            message = "[airtest] Poco RPC 已关闭"
        try:
            print(message, flush=True)
        except OSError:
            # A closed parent pipe must not replace the recorded test outcome.
            pass

    def flush(self):
        temp = self.output / "ui-result.tmp"
        temp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.output / "ui-result.json")

    def prepare(self):
        from airtest.core.api import auto_setup
        from poco.drivers.unity3d import UnityPoco

        d = self.config["device_config"]
        uri = d.get("uri", "")
        if "title_match" in d:
            from app.ui_windows import resolve_window

            try:
                target = resolve_window(d)
            except RuntimeError as exc:
                self.meta["prepare_detail"] = str(exc)
                raise
            self.meta["target_window"] = target
            self.flush()
            uri = f"Windows:///{target['hwnd']}"
            print(
                f"[airtest] 窗口唯一匹配：{target['title']}（{target['process_name']}）", flush=True
            )
        os.environ["POCO_DEVICE"] = uri
        auto_setup(self.config["root"], devices=[uri], logdir=str(self.output / "airtest-log"))
        from airtest.core.api import device

        dev = device()
        if d.get("unity_editor"):
            # Use the GameView inside the already selected window. Unlike the
            # SDK's convenience helper, do not rediscover a different editor.
            game_view = dev.app.top_window().child_window(title="UnityEditor.GameView")
            if not game_view.exists():
                raise RuntimeError("指定 Unity Editor 窗口缺少 GameView")
            dev._top_window = game_view.wrapper_object()
            dev.focus_rect = (0, 40, 0, 0)
            dev.set_focus()
        # Existing Windows Unity fixture also needs this for virtual-NIC hosts.
        dev.get_ip_address = lambda: d["host"]
        self.poco = UnityPoco(
            addr=(d["host"], int(d["port"])), device=dev, use_airtest_input=True, auto_setup=True
        )
        deadline = time.monotonic() + self.config["wait_timeout"]
        last_error = None
        while time.monotonic() < deadline:
            try:
                if self.poco.agent.hierarchy.dump():
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(0.25)
        raise RuntimeError("Poco UI 层级不可读；确认客户端已启动并集成 Poco SDK") from last_error

    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.failed or report.skipped:
            self.meta["cases"].append(
                {
                    "id": report.nodeid,
                    "when": report.when,
                    "outcome": report.outcome,
                    "duration": report.duration,
                }
            )
        if report.failed:
            phase = "teardown" if report.when == "teardown" else "run"
            # Preserve the primary failure when teardown fails as well.
            if self.meta["error"] is None:
                self.meta.update(
                    phase=phase, failure_kind="test", error="UI 用例断言或 fixture 失败"
                )
            self.capture(report)
        self.flush()

    def capture(self, report):
        import hashlib

        prefix = hashlib.sha256((report.nodeid + report.when).encode()).hexdigest()[:12]
        (self.output / (prefix + "-trace.txt")).write_text(str(report.longrepr), encoding="utf-8")
        try:
            from airtest.core.api import snapshot

            snapshot(filename=str(self.output / (prefix + "-screen.png")))
        except Exception:
            self.meta["evidence_errors"].append("失败截图采集失败")
        try:
            tree = self.poco.agent.hierarchy.dump()
            (self.output / (prefix + "-hierarchy.json")).write_text(
                json.dumps(tree, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            self.meta["evidence_errors"].append("失败 UI 层级采集失败")

    def final_evidence(self):
        try:
            from airtest.core.api import snapshot

            snapshot(filename=str(self.output / "final-screen.png"))
        except Exception:
            self.meta["evidence_errors"].append("最终界面截图采集失败")
        try:
            tree = self.poco.agent.hierarchy.dump()
            (self.output / "final-hierarchy.json").write_text(
                json.dumps(tree, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            self.meta["evidence_errors"].append("最终 UI 层级采集失败")


def _execute(plugin, config):
    import pytest

    from app.runtime_probe import check, check_project

    output = plugin.output
    try:
        plugin.meta["runtime_checks"] = check_project(config)
        for row in plugin.meta["runtime_checks"]:
            print(f"[airtest] {row['name']}：{row['status']} · {row['detail']}", flush=True)
        failed = [r for r in plugin.meta["runtime_checks"] if r["status"] == "failed"]
        if failed:
            plugin.meta["prepare_detail"] = "；".join(
                r["name"] + "：" + r["detail"] for r in failed
            )
            raise RuntimeError("Project requirements unavailable")
        plugin.prepare()
        plugin.meta["runtime_checks"].append(
            check("poco", "执行前客户端检查", "passed", "目标设备已连接，Poco UI 层级可读")
        )
    except Exception:
        plugin.meta.update(
            phase="prepare",
            failure_kind="infrastructure",
            error="UI 准备失败："
            + (
                plugin.meta.get("prepare_detail")
                or "设备连接或 Poco 层级不可用；检查目标窗口、SDK、端口和依赖"
            ),
        )
        (output / "prepare-trace.txt").write_text(traceback.format_exc(), encoding="utf-8")
        plugin.flush()
        return 2
    import importlib.metadata

    plugin.meta["dependency_versions"] = {}
    for package in ("airtest", "pocoui", "pytest"):
        try:
            plugin.meta["dependency_versions"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            plugin.meta["dependency_versions"][package] = "unknown"

    class Fixtures:
        @pytest.fixture
        def platform_poco(self):
            return plugin.poco

    plugin.meta["phase"] = "run"
    plugin.flush()
    os.chdir(config["root"])
    sys.path.insert(0, config["root"])
    rc = int(
        pytest.main(
            [
                config["case"],
                "-o",
                "addopts=",
                "--tb=short",
                "-p",
                "no:cacheprovider",
                "--junitxml=" + config["junit"],
            ],
            plugins=[plugin, Fixtures()],
        )
    )
    if rc and not plugin.meta["error"]:
        plugin.meta.update(error="UI 执行未完成或未收集到用例", failure_kind="execution")
    if not rc:
        plugin.meta["phase"] = "teardown"
    plugin.final_evidence()
    plugin.flush()
    return rc


def main():
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    plugin = UIPlugin(Path(config["output"]), config)
    try:
        return _execute(plugin, config)
    except Exception:
        # Unexpected execution errors still leave the connection through finally.
        if not plugin.meta["error"]:
            plugin.meta.update(
                error="UI 子进程执行异常；参见 worker-trace.txt", failure_kind="execution"
            )
        (plugin.output / "worker-trace.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return 2
    finally:
        # Evidence collection must finish while RPC is alive. Covers prepare
        # failure, pytest failure, exceptions and normal completion. Force-kill
        # cannot execute Python finally; the watchdog semantics stay unchanged.
        plugin.close()
        try:
            plugin.flush()
        except OSError:
            print("[warn] UI 最终阶段结果写入失败", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
