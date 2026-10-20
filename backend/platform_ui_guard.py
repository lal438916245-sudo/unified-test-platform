"""Loaded explicitly by the platform's ordinary pytest subprocess."""

from pathlib import Path

import pytest
from app.ui_config import ui_files


def pytest_ignore_collect(collection_path, config):
    if Path(collection_path).resolve() in ui_files():
        raise pytest.UsageError("UI 用例仅允许 engine=airtest；pytest 引擎禁止收集该用例")


def pytest_collection_modifyitems(items):
    if any(Path(i.path).resolve() in ui_files() for i in items):
        raise pytest.UsageError("UI 用例仅允许 engine=airtest")
