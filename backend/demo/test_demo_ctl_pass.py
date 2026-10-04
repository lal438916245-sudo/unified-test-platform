"""控制加固验证：正常通过的 demo。"""
import time


def test_ctl_pass_a():
    time.sleep(0.1)
    assert True


def test_ctl_pass_b():
    print("[ctl] pass-demo ok")
    assert True