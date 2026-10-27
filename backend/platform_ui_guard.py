"""Loaded explicitly by the platform's ordinary pytest subprocess."""

from pathlib import Path
import json
import os

import pytest


def ui_files():
    # The runner resolves this list in the platform process. Importing its app
    # package here can shadow (or be shadowed by) a tested project's own app.
    try:
        paths = json.loads(os.environ["PLATFORM_UI_BOUND_FILES"])
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise pytest.UsageError("缺少有效的 UI 收集保护清单，请通过平台执行") from None
    return {Path(p).resolve() for p in paths}


def pytest_ignore_collect(collection_path, config):
    if Path(collection_path).resolve() in ui_files():
        # Pytest also asks about sibling files when a non-UI file was selected.
        # Exclude those siblings, while still refusing directory/default scans.
        selected = [Path(str(a).split("::", 1)[0]).resolve() for a in config.args]
        if selected and all(p.is_file() and p.suffix == ".py" and p not in ui_files() for p in selected):
            return True
        raise pytest.UsageError("UI 用例仅允许 engine=airtest；pytest 引擎禁止收集该用例")


@pytest.hookimpl(tryfirst=True)
def pytest_collect_file(file_path, parent):
    # Explicit file arguments can bypass pytest_ignore_collect. Reject before
    # pytest's Python collector imports the UI module or its fixtures.
    if Path(file_path).resolve() in ui_files():
        raise pytest.UsageError("UI 用例仅允许 engine=airtest")


def pytest_collection_modifyitems(items):
    if any(Path(i.path).resolve() in ui_files() for i in items):
        raise pytest.UsageError("UI 用例仅允许 engine=airtest")
