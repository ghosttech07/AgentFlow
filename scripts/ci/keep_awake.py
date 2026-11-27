"""Run a command while keeping Windows awake (no idle sleep / Modern Standby), so long test runs are not paused.

    python scripts/ci/keep_awake.py -- <command...>

Elsewhere it just runs the command. Uses SetThreadExecutionState (process-scoped, nothing is changed permanently).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if sys.platform == "win32":
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
    args[0] = shutil.which(args[0]) or os.path.abspath(args[0])  # Windows does not resolve 'dir/x.exe'
    return subprocess.call(args)


if __name__ == "__main__":
    raise SystemExit(main())
