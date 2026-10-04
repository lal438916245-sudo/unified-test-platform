"""控制加固验证：短慢 demo（约 4s），用于观察"同 Executor 独占锁排队"。"""
import time


def test_ctl_wait():
    print("[ctl] wait-demo running 4s")
    time.sleep(4)
    assert True