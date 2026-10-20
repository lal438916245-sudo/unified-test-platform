"""Colyseus MMO client smoke: server setup -> Begin -> Sign Up entry.

No account creation or external GameAutoTest-Pro autouse MongoDB cleanup.
Target client must already be in its ServerSetupMenu initial state.
"""
import os


def test_begin_opens_signup_entry(platform_poco):
    poco = platform_poco
    timeout = float(os.environ["PLATFORM_UI_WAIT_TIMEOUT"])
    begin = poco("BeginBtn")
    begin.wait_for_appearance(timeout=timeout)
    assert begin.exists(), "初始 ServerSetupMenu 中没有 BeginBtn"
    target = poco(text="Sign Up")
    assert not target.exists(), "已在注册入口，需恢复 ServerSetupMenu 初始状态"
    server = poco("ServerInput")
    port = poco("PortInput")
    assert server.exists() and port.exists(), "服务器输入控件缺失"
    server.set_text(os.environ["GAME_SERVER_HOST"])
    port.set_text(os.environ["GAME_SERVER_PORT"])
    begin.click()
    target.wait_for_appearance(timeout=timeout)
    assert target.exists(), "点击 Begin 后未出现 text=Sign Up"
