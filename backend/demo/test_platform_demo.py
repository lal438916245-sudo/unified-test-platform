"""自包含的 pytest 冒烟测试：用于演示平台的纵向闭环（不依赖任何被测服务）。
执行时会生成一个 junit.xml 产物，控制台打印两段信息，供日志流式展示。"""
import time


def test_platform_demo_pass():
    print("[platform] hello from pytest_runner (subprocess)")
    assert True


def test_platform_demo_heartbeat():
    payload = {"rps_probe": 1, "ok": True}
    print(f"[platform] heartbeat payload={payload}")
    assert payload["ok"] is True


def test_platform_demo_sleeps_a_little():
    time.sleep(0.2)
    assert True