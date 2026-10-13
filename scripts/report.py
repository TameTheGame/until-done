"""Write a small record each time the stop hook decides whether a turn may end.

The records are for refining the skill. They store why the hook allowed or
refused the stop, how far the goal had gotten, and the shape of the last
reply, so an early quit can be explained without the chat open.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import ledger as L

EXCERPT_LIMIT = 600
TAIL_BYTES = 262144
TRACE_LIMIT = 2 * 1024 * 1024

ASKING = (
    "should i",
    "shall i",
    "want me to",
    "would you like",
    "let me know",
    "do you want",
    "need your approval",
    "needs your approval",
    "please confirm",
    "if you'd like",
    "if you want",
    "waiting for",
    "ready when you are",
)
RECAP = ("outlined", "here's the plan", "here is the plan", "next steps", "i can continue", "phase 1", "first phase")
CLAIMED = ("i've finished", "i have finished", "all set", "this should work", "we're done", "we are done", "should now work")


def classify_quit(text: str) -> str:
    lowered = text.lower()
    if not lowered.strip():
        return "unknown"
    if any(phrase in lowered for phrase in ASKING):
        return "permission_ask"
    if any(phrase in lowered for phrase in RECAP):
        return "recap"
    if any(phrase in lowered for phrase in CLAIMED):
        return "claimed_done"
    if "?" in text:
        return "question"
    if any(phrase in lowered for phrase in ("traceback", "error:", "exception", "failed to")):
        return "tool_error"
    return "other"


def skill_gap(decision: str, quit_shape: str, open_count: int) -> str | None:
    """Name what went wrong when a stop ended a goal that was not proven done."""
    if decision in ("idle", "resumed", "done", "stopped"):
        if decision == "done" and quit_shape in ("permission_ask", "question"):
            return "finished_with_a_question"
        return None
    return {
        "aborted": "user_or_product_abort",
        "invalid_goal": "goal_file_unreadable",
        "hook_error": "hook_crashed",
        "ignored_status": "status_not_resumed",
        "cap": "cap_during_progress",
        "stuck": "resume_not_changing_behavior",
        "blocked": "permission_dressed_as_block" if quit_shape in ("permission_ask", "recap") else "real_block",
    }.get(decision, "unknown_stop")


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part)


def last_assistant_text(transcript_path: object) -> str:
    """Read the last assistant text from a Cursor or Claude Code JSONL transcript."""
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return ""
    path = Path(transcript_path)
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
            raw = handle.read().decode("utf-8", "ignore")
    except OSError:
        return ""
    text = ""
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        message = row.get("message") if isinstance(row.get("message"), dict) else row
        role = row.get("role") or row.get("type") or message.get("role")
        if role != "assistant":
            continue
        found = _text_of(message.get("content")).strip()
        if found:
            text = found
    return text


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def record_stop(event, decision: str, result: dict, ledger: dict | None = None, error: str | None = None) -> None:
    directory = L.home()
    now = datetime.now(timezone.utc).isoformat()
    heartbeat = {
        "at": now,
        "harness": event.harness,
        "session_id": event.session_id,
        "turn_status": event.status,
        "decision": decision,
        "had_goal": ledger is not None,
    }
    _write(directory / "hook-heartbeat.json", heartbeat)
    if decision == "idle":
        return
    excerpt = event.last_message or last_assistant_text(event.transcript_path)
    excerpt = excerpt[-EXCERPT_LIMIT:]
    quit_shape = classify_quit(excerpt)
    items = L.open_items(ledger) if ledger else []
    gap = skill_gap(decision, quit_shape, len(items))
    record = {
        "at": now,
        "decision": decision,
        "gap": gap,
        "returned_followup": bool(result),
        "harness": event.harness,
        "turn_status": event.status,
        "loop_count": event.loop_count,
        "session_id": event.session_id,
        "goal_id": ledger.get("id") if ledger else None,
        "goal": (ledger.get("goal") or "")[:300] if ledger else None,
        "resumes": ledger.get("resumes") if ledger else None,
        "stalls": ledger.get("stalls") if ledger else None,
        "open": items[:12],
        "proved": sum(1 for c in ledger["criteria"] if c["ok"]) if ledger else 0,
        "criteria": len(ledger["criteria"]) if ledger else 0,
        "blocked_reason": ledger.get("blocked_reason") if ledger else None,
        "quit_shape": quit_shape,
        "last_assistant": excerpt,
        "model": event.raw.get("model") or event.raw.get("model_id"),
        "harness_version": event.raw.get("cursor_version") or event.raw.get("version"),
        "error": (error or "")[:400] or None,
        "transcript_path": event.transcript_path,
    }
    _write(directory / "last-stop.json", record)
    trace = directory / "trace.jsonl"
    try:
        if trace.is_file() and trace.stat().st_size > TRACE_LIMIT:
            trace.replace(directory / "trace.1.jsonl")
    except OSError:
        pass
    with trace.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if gap is not None:
        _write(directory / "last-failure.json", record)
