"""Read-only Windows inventory and deterministic selection. No focus or input."""

from __future__ import annotations

import json
import os
import sys


def enumerate_windows():
    import win32api
    import win32con
    import win32gui
    import win32process

    rows = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return
        process = ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
            )
            try:
                process = os.path.basename(win32process.GetModuleFileNameEx(handle, 0))
            finally:
                handle.Close()
        except Exception:
            pass  # Unknown process cannot satisfy a process-specific rule.
        rows.append(
            {
                "hwnd": int(hwnd),
                "title": title,
                "window_class": win32gui.GetClassName(hwnd),
                "process_name": process,
            }
        )

    win32gui.EnumWindows(collect, None)
    return sorted(rows, key=lambda row: row["hwnd"])


def matching_windows(config, windows):
    title = config.get("title_match", "").strip()
    if not title:
        raise ValueError("窗口标题匹配规则不能为空")
    return [
        w
        for w in windows
        if (w["title"] == title if config.get("match_mode") == "exact" else title in w["title"])
        and (
            not config.get("process_name")
            or w["process_name"].casefold() == config["process_name"].casefold()
        )
        and (not config.get("window_class") or w["window_class"] == config["window_class"])
    ]


def resolve_window(config, windows=None):
    matches = matching_windows(config, enumerate_windows() if windows is None else windows)
    if len(matches) != 1:
        raise RuntimeError(
            f"UI 窗口匹配到 {len(matches)} 个目标，必须恰好 1 个；请检查客户端是否启动或收窄匹配规则"
        )
    return matches[0]


def main():
    config = json.load(sys.stdin)
    windows = enumerate_windows()
    matches = matching_windows(config, windows) if config.get("title_match") else windows
    print(json.dumps({"windows": matches}, ensure_ascii=False))


if __name__ == "__main__":
    main()
