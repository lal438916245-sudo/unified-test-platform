"""Real-device smoke: requires a registered stable, visible Unity control."""
import os


def test_unity_control(platform_poco):
    control = platform_poco(os.environ["PLATFORM_UI_SMOKE_SELECTOR"])
    control.wait_for_appearance(timeout=float(os.environ["PLATFORM_UI_WAIT_TIMEOUT"]))
    assert control.exists(), "登记的 Unity 冒烟控件未出现"
    result = platform_poco(os.environ["PLATFORM_UI_SMOKE_RESULT"])
    assert not result.exists(), "点击后目标控件已存在；请恢复初始界面再执行"
    control.click()
    result.wait_for_appearance(timeout=float(os.environ["PLATFORM_UI_WAIT_TIMEOUT"]))
    assert result.exists(), "点击后预期界面未出现"
