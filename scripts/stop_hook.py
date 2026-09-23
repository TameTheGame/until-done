#!/usr/bin/env python3
"""Continue the chat when a babysit goal is still open.

Cursor runs this on the stop event. Stdout must be JSON only.
No active goal, a real block, or a finished goal prints {}.
Every stop writes a heartbeat. Stops that end a goal also write last-failure.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ledger import fingerprint, is_real_block, load_ledger, save_ledger, work_gaps  # noqa: E402
from report import record_stop  # noqa: E402


def _workspace_matches(ledger: dict, payload: dict) -> bool:
    workspace = ledger.get("workspace")
    if not workspace:
        return True
    roots = payload.get("workspace_roots")
    if not isinstance(roots, list) or not roots:
        return True
    wanted = str(Path(workspace).expanduser())
    return any(str(Path(str(root)).expanduser()) == wanted for root in roots)


def _message(ledger: dict, gaps: list[str], turn_status: str) -> str:
    lines = [
        "Babysit resume. The goal is still open. Do the next step with tools now.",
        "Do not ask for permission to continue. Do not end with a recap while anything below is open.",
    ]
    if turn_status == "error":
        lines.append("The last turn errored. Continue from the next open step.")
    if int(ledger.get("stalls") or 0) >= 3:
        lines.append(
            "The goal record did not change on recent resumes. Change approach. Do not repeat the same step."
        )
    lines.append("")
    lines.append("<task>")
    lines.append(f"goal: {ledger.get('goal')}")
    lines.append("still open:")
    lines.extend(f"- {gap}" for gap in gaps[:12])
    if ledger.get("remaining"):
        lines.append(f"next: {ledger['remaining'][0]}")
    lines.append("</task>")
    return "\n".join(lines)


def _finish(
    payload: dict,
    decision: str,
    result: dict,
    ledger: dict | None = None,
    error: str | None = None,
) -> dict:
    try:
        record_stop(payload, decision, result, ledger, error)
    except Exception as exc:
        sys.stderr.write(f"babysit report: {exc}\n")
    return result


def handle(payload: dict) -> dict:
    turn_status = payload.get("status")
    path, ledger, error = load_ledger()
    saw_goal = ledger is not None or (error is not None and path.is_file())

    if turn_status == "aborted":
        return _finish(payload, "aborted" if saw_goal else "idle", {}, ledger, error)
    if turn_status not in ("completed", "error", None):
        return _finish(payload, "ignored_status" if saw_goal else "idle", {}, ledger, error)

    if ledger is None and error is not None and path.is_file():
        if int(payload.get("loop_count") or 0) >= 3:
            return _finish(payload, "invalid_ledger", {}, None, error)
        return _finish(
            payload,
            "resumed",
            {
                "followup_message": (
                    "Babysit resume. The goal file is invalid "
                    f"({error}). Rewrite ~/.cursor/babysit/active.json to version 1 and continue the goal. "
                    "Do not ask for permission to continue."
                )
            },
            None,
            error,
        )
    if ledger is None:
        return _finish(payload, "idle", {})
    if ledger.get("status") == "done" and not work_gaps(ledger):
        return _finish(payload, "done", {}, ledger)
    if not _workspace_matches(ledger, payload):
        return _finish(payload, "workspace_mismatch", {}, ledger)

    conversation_id = payload.get("conversation_id")
    bound = ledger.get("conversation_id")
    if isinstance(conversation_id, str) and conversation_id.strip():
        if bound and bound != conversation_id:
            return _finish(payload, "conversation_mismatch", {}, ledger)
        if not bound:
            ledger["conversation_id"] = conversation_id

    if is_real_block(ledger.get("blocked_reason")):
        save_ledger(ledger, path)
        return _finish(payload, "blocked", {}, ledger)

    if ledger.get("blocked_reason"):
        ledger["blocked_reason"] = None
        ledger["status"] = "active"

    gaps = work_gaps(ledger)
    if not gaps:
        ledger["status"] = "done"
        ledger["remaining"] = []
        save_ledger(ledger, path)
        return _finish(payload, "done", {}, ledger)

    followups = int(ledger.get("followups") or 0)
    if followups >= int(ledger["max_followups"]):
        preview = "; ".join(gaps[:4])
        ledger["status"] = "blocked"
        ledger["blocked_reason"] = (
            f"cap: stopped after {followups} resumes so this goal cannot run unbounded. Still open: {preview}"
        )
        save_ledger(ledger, path)
        return _finish(
            payload,
            "cap",
            {
                "followup_message": (
                    "Babysit resume. The automatic resume cap was hit. Tell the user what is still open "
                    "and that raising max_followups in ~/.cursor/babysit/active.json resumes the goal. "
                    "Do not keep working.\n\n"
                    f"<task>\ngoal: {ledger.get('goal')}\nstill open:\n"
                    + "\n".join(f"- {gap}" for gap in gaps[:12])
                    + "\n</task>"
                )
            },
            ledger,
        )

    current = fingerprint(ledger)
    if current == ledger.get("last_fingerprint"):
        ledger["stalls"] = int(ledger.get("stalls") or 0) + 1
    else:
        ledger["stalls"] = 0
    ledger["last_fingerprint"] = current
    ledger["followups"] = followups + 1
    ledger["status"] = "active"
    message = _message(ledger, gaps, str(turn_status or "completed"))
    save_ledger(ledger, path)
    return _finish(payload, "resumed", {"followup_message": message}, ledger)


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        result = handle(payload)
    except Exception as exc:
        sys.stderr.write(f"babysit stop hook: {exc}\n")
        try:
            record_stop(payload, "hook_error", {}, None, str(exc))
        except Exception as report_error:
            sys.stderr.write(f"babysit report: {report_error}\n")
        result = {}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
