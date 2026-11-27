"""Run every AgentFlow demo in turn (fake model, offline, free) and print one PASS/FAIL table.

    python scripts/demo.py              # all five demos (about 5-7 minutes)
    python scripts/demo.py crash tools  # only some of them

Stop any other AgentFlow server that uses the same database first: each demo starts its own on port 8765.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMOS = {
    "crash": (["crash_demo.py", "--fake"], "crash mid-task, restart, completed work kept, the failed task resumes"),
    "controls": (["controls_demo.py"], "pause/resume, approve with an edit, reject + skip, cancel while running"),
    "budget": (["budget_demo.py"], "budget limit pauses the workflow, raise it, resume, complete"),
    "tools": (["tools_demo.py"], "tools + approvals; crash right after an email, exactly one email; injection blocked"),
    "adaptive": (["adaptive_demo.py"], "loop 5 -> v2 -> 9, branches, re-plan, crash during v2, memory, exfil held"),
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    chosen = sys.argv[1:] or list(DEMOS)
    unknown = [c for c in chosen if c not in DEMOS]
    if unknown:
        print(f"Unknown demo(s): {', '.join(unknown)}. Choose from: {', '.join(DEMOS)}")
        return 2
    results: list[tuple[str, bool, float]] = []
    for name in chosen:
        args, what = DEMOS[name]
        print(f"\n{'#' * 72}\n# {name}: {what}\n{'#' * 72}", flush=True)
        start = time.monotonic()
        code = subprocess.call([sys.executable, str(ROOT / "scripts" / args[0]), *args[1:]], cwd=ROOT)
        results.append((name, code == 0, time.monotonic() - start))
    print(f"\n{'=' * 72}\nAgentFlow demos\n{'=' * 72}")
    for name, ok, secs in results:
        print(f"   {'PASS' if ok else 'FAIL'}  {name:9} {DEMOS[name][1]}  ({secs:.0f} s)")
    passed = all(ok for _, ok, _ in results)
    print(f"\nAll demos - {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
