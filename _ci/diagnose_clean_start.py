"""Bounded startup traceback helper for the isolated installation check."""
import faulthandler
from pathlib import Path
import runpy
import sys

platform=Path(sys.argv.pop(1)).resolve()
sys.path.insert(0,str(platform/'backend'))
faulthandler.dump_traceback_later(20)
runpy.run_path(str(platform/'backend/run.py'),run_name='__main__')
