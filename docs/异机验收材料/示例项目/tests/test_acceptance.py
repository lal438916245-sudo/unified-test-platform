"""Cross-PC calibration only: one success and one intentional failure.

Register the two selectors separately and run them in separate platform plans.
The tests use no SUT, network service or database.
"""
import os
from pathlib import Path
import sys


def test_should_pass():
    print('CROSS_PC_PASS_MARKER')
    print('Python:', sys.executable)
    print('Project:', Path(__file__).resolve().parents[1])
    assert Path.cwd().resolve() == Path(__file__).resolve().parents[1]
    assert os.environ.get('PLATFORM_ENV_KIND') == 'offline'


def test_should_fail():
    print('CROSS_PC_FAIL_MARKER')
    assert 1 == 2, 'CROSS_PC_EXPECTED_FAILURE: intentional failure for acceptance'
