"""Exercise the external project's real fixture without its account-cleanup hooks.

Only the fixture AST node (including its actual decorator) is copied into an
isolated suite. No external conftest is imported, no Unity/Mongo service is used.
"""
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from test_airtest import fake_device


SOURCE = Path(__file__).resolve().parents[3] / "GameAutoTest-Pro" / "conftest.py"
pytestmark = pytest.mark.skipif(not SOURCE.is_file(), reason="External GameAutoTest-Pro fixture is not deployed")

HARNESS = r'''
import json, logging, sys, types
import pytest
MODE = __MODE__
EVENT_PATH = __EVENT_PATH__
logger = logging.getLogger(__name__)
POCO_DEVICE, POCO_HOST, POCO_ADDR = 'Windows:///fake', '127.0.0.1', ('127.0.0.1', 5001)
def event(name):
    with open(EVENT_PATH, 'a', encoding='utf-8') as f:
        f.write(json.dumps(name) + '\n')
class Clock:
    calls = 0
    def time(self):
        self.calls += 1
        return 0 if self.calls <= 2 else 11
    def sleep(self, seconds): pass
time = Clock()
class FakePoco:
    def __init__(self, *args, **kwargs):
        event('construct')
        if MODE == 'standalone_ctor_fail': raise RuntimeError('constructor-sentinel')
        self.agent = types.SimpleNamespace(hierarchy=self, rpc=self)
    def dump(self):
        event('dump')
        return None if MODE == 'standalone_tree_fail' else {'root': 'fake'}
    def close(self):
        event('close')
        if MODE == 'standalone_close_fail': raise RuntimeError('close-sentinel')
def connect_device(uri):
    event('connect')
    if MODE == 'standalone_connect_fail': raise RuntimeError('connect-sentinel')
    return types.SimpleNamespace()
for name in ('airtest', 'airtest.core', 'airtest.core.api', 'poco', 'poco.drivers', 'poco.drivers.unity3d'):
    sys.modules[name] = types.ModuleType(name)
sys.modules['airtest.core.api'].connect_device = connect_device
sys.modules['poco.drivers.unity3d'].UnityPoco = FakePoco
'''


@pytest.mark.parametrize("mode,exit_code,connections,closes,expected", [
    ("borrow", 0, 0, 0, ""),
    ("borrow_fail", 1, 0, 0, "platform-setup-sentinel"),
    ("borrow_nested", 1, 0, 0, "unknown_platform_dependency"),
    ("borrow_none", 1, 0, 0, "平台没有提供可用的 Poco 连接"),
    ("standalone", 0, 1, 1, ""),
    ("standalone_assert", 1, 1, 1, "test-assertion-sentinel"),
    ("standalone_tree_fail", 1, 1, 1, "Poco 连接失败或游戏未初始化"),
    ("standalone_close_fail", 0, 1, 1, ""),
    ("standalone_ctor_fail", 1, 1, 0, "constructor-sentinel"),
    ("standalone_connect_fail", 1, 1, 0, "connect-sentinel"),
])
def test_dual_mode_fixture(tmp_path, mode, exit_code, connections, closes, expected):
    source = SOURCE.read_text(encoding="utf-8")
    fixture = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "init_game_poco")
    lines = source.splitlines()
    extracted = "\n".join(lines[fixture.decorator_list[0].lineno - 1:fixture.end_lineno])
    event_file = tmp_path / "events.jsonl"
    harness = HARNESS.replace("__MODE__", repr(mode)).replace("__EVENT_PATH__", repr(str(event_file)))
    if mode == "borrow":
        harness += '''
borrowed = object()
@pytest.fixture
def platform_poco():
    event('platform-setup')
    yield borrowed
    event('platform-teardown')
'''
    elif mode == "borrow_fail":
        harness += "\n@pytest.fixture\ndef platform_poco(): raise RuntimeError('platform-setup-sentinel')\n"
    elif mode == "borrow_nested":
        harness += "\n@pytest.fixture\ndef platform_poco(unknown_platform_dependency): return object()\n"
    elif mode == "borrow_none":
        harness += "\n@pytest.fixture\ndef platform_poco(): return None\n"
    (tmp_path / "conftest.py").write_text(harness + "\n" + extracted + "\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "test_adapter.py").write_text('''
from conftest import MODE, event
def test_flow(init_game_poco, request):
    assert init_game_poco is not None
    if MODE == 'borrow':
        assert init_game_poco is request.getfixturevalue('platform_poco')
    event('case')
    assert MODE != 'standalone_assert', 'test-assertion-sentinel'
''', encoding="utf-8")
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "",
           "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
        "--confcutdir", str(tmp_path), "-c", str(tmp_path / "pytest.ini"), "test_adapter.py"],
        cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert expected in result.stdout
    events = [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines()] if event_file.exists() else []
    assert events.count("connect") == connections
    assert events.count("close") == closes
    if mode == "borrow":
        assert events == ["platform-setup", "case", "platform-teardown"]
    if mode == "standalone":
        assert events.index("case") < events.index("close")


@pytest.mark.parametrize("fails", [False, True])
def test_worker_borrows_fixture_until_final_evidence(tmp_path, fake_device, monkeypatch, fails):
    from app.executors.airtest_runner import AirtestRunner
    root, params = fake_device
    source = SOURCE.read_text(encoding="utf-8")
    fixture = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "init_game_poco")
    extracted = "\n".join(source.splitlines()[fixture.decorator_list[0].lineno - 1:fixture.end_lineno])
    (root / "conftest.py").write_text("import pytest, logging\nlogger=logging.getLogger(__name__)\n" + extracted, encoding="utf-8")
    (root / "test_ui.py").write_text("def test_flow(init_game_poco, platform_poco):\n"
        " assert init_game_poco is platform_poco\n assert " + ("False" if fails else "True"), encoding="utf-8")
    close_log = tmp_path / "close.txt"
    monkeypatch.setenv("FAKE_CLOSE_LOG", str(close_log))
    result = AirtestRunner().run({"params": params}, {}, {"python_executable": sys.executable},
        str(tmp_path / "output/junit.xml"), lambda _: None, lambda: False)
    assert result.status == ("failed" if fails else "success"), result.error
    assert result.engine_data["evidence_errors"] == []
    assert result.engine_data["connection_cleanup"]["state"] == "closed"
    assert (tmp_path / "output/final-hierarchy.json").is_file()
    assert close_log.read_text(encoding="utf-8").splitlines() == ["closed"]
