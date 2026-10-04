"""控制加固验证：可翻转的重试 demo。
工作目录下若存在 demo/retry_green.flag 则通过（用于演示失败→重试→成功）。"""
import os
import pathlib


def test_ctl_retry():
    flag = pathlib.Path(__file__).resolve().parent / "retry_green.flag"
    print(f"[ctl] retry-demo, flag={flag.exists()}")
    assert flag.exists(), "重试开关未开启（期望重试时置绿）"