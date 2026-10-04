"""控制加固验证：长跑 demo，用于超时与取消场景（默认 sleep 长达 300s）。"""
import time


def test_ctl_slow():
    print("[ctl] slow-demo start, sleeping long")
    time.sleep(300)
    print("[ctl] slow-demo end (should not be reached)")
    assert True