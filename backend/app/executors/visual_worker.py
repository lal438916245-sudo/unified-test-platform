"""Launch an external visual adapter using an explicitly selected Airtest package.

Preloading the package binds its __path__ before legacy scripts insert their old
checkout into sys.path. No external script is edited and no algorithm is replaced.
"""

import importlib
import runpy
import sys
from pathlib import Path


def main():
    script, source, *arguments = sys.argv[1:]
    if source:
        root = Path(source).resolve()
        sys.path.insert(0, str(root))
        package = importlib.import_module("airtest")
        if not Path(package.__file__).resolve().is_relative_to(root):
            raise RuntimeError("Airtest 未从配置的源码根加载")
    sys.path.insert(0, str(Path(script).resolve().parent))
    sys.argv = [script, *arguments]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
