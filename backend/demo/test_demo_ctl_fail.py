"""控制加固验证：故意失败的 demo。"""
import pytest


def test_ctl_fail():
    print("[ctl] about-to-fail demo")
    assert False, "ctl 故意失败：" + str(surprise())


def surprise():
    # 仅为了演示失败是真实的被测失败（非 Runner 异常）
    return "FAIL_01"