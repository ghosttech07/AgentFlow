"""CI helper: print a GitHub annotation for every failed/errored test in the JUnit reports (public, no login)."""

from __future__ import annotations

import glob
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    failed = 0
    for path in sorted(glob.glob("reports/junit-*.xml")):
        for case in ET.parse(path).getroot().iter("testcase"):
            for kind in ("failure", "error"):
                node = case.find(kind)
                if node is None:
                    continue
                failed += 1
                name = f"{case.get('classname')}::{case.get('name')}"
                message = (node.get("message") or node.text or "").strip().splitlines()
                first = message[0][:300] if message else kind
                print(f"::error title={kind}: {name}::{first}")
    print(f"{failed} failed test(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
