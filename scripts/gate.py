#!/usr/bin/env python3
"""Print whether the active goal may end this turn.

The first line is CONTINUE, DONE, BLOCKED, or ABSENT. Exit 0 in every case.
CONTINUE means the agent must keep using tools in this turn.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ledger import is_real_block, load_ledger, work_gaps  # noqa: E402


def render() -> str:
    _path, ledger, error = load_ledger()
    if error is not None and ledger is None:
        return (
            "CONTINUE\n"
            f"The goal file is invalid ({error}). Rewrite ~/.cursor/babysit/active.json "
            "to version 1 and keep working."
        )
    if ledger is None:
        return "ABSENT"
    if is_real_block(ledger.get("blocked_reason")):
        return f"BLOCKED\nreason: {ledger.get('blocked_reason')}"
    gaps = work_gaps(ledger)
    if not gaps:
        return "DONE"
    lines = ["CONTINUE", "Still open:"]
    lines.extend(f"- {gap}" for gap in gaps[:12])
    if ledger["remaining"]:
        lines.append(f"next: {ledger['remaining'][0]}")
    elif gaps:
        lines.append(f"next: {gaps[0]}")
    if ledger.get("blocked_reason"):
        lines.append("The recorded block was only a permission ask. Clear it and continue.")
    return "\n".join(lines)


def main() -> int:
    sys.stdout.write(render() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
