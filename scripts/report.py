"""Write a small record when a babysit run stops.

The record is for refining the skill. It stores why the hook allowed the
stop, how far the goal had gotten, and the shape of the last reply.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ledger import ledger_path, work_gaps

EXCERPT_LIMIT = 600


def state_dir() -> Path:
    return ledger_path().parent


def classify_quit(text: str) -> str:
    lowered = text.lower()
    if not lowered.strip():
        return "unknown"
    permission = (
        "should i",
        "shall i",
        "want me to",
        "let me know",
        "do you want",
        "need your approval",
        "needs your approval",
        "please confirm",
        "if you'd like",
        "if you want",
        "waiting for",
    )
    if any(phrase in lowered for phrase in permission):
        return "permission_ask"
    if any(phrase in lowered for phrase in ("outlined", "here's the plan", "here is the plan", "next steps", "i can continue")):
        return "recap"
    if any(phrase in lowered for phrase in ("i've finished", "i have finished", "all set", "this should work", "we're done", "we are done")):
        return "claimed_done"
    if "?" in text:
        return "question"
    if any(phrase in lowered for phrase in ("traceback", "error:", "exception", "failed to")):
        return "tool_error"
    return "other"


def skill_gap(decision: str, quit_shape: str, stalls: int, unproved: int) -> str | None:
    if decision in ("idle", "resumed"):
        return None
    if decision == "aborted":
        return "user_or_product_abort"
    if decision == "conversation_mismatch":
        return "wrong_chat"
    if decision == "workspace_mismatch":
        return "wrong_workspace"
    if decision == "invalid_ledger":
        return "ledger_unreadable"
    if decision == "hook_error":
        return "hook_crashed"
    if decision == "ignored_status":
        return "status_not_resumed"
    if decision == "cap":
        return "resume_not_changing_behavior" if stalls >= 3 else "cap_during_progress"
    if decision == "blocked":
        if quit_shape in ("permission_ask", "recap", "question"):
            return "permission_dressed_as_block"
        return "real_block"
    if decision == "done":
        if unproved:
            return "claimed_done_unproved"
        if quit_shape in ("permission_ask", "recap", "question"):
            return "stopped_while_asking"
        return None
    return "unknown_stop"


def last_assistant_text(transcript_path: object) -> str:
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        return ""
    path = Path(transcript_path)
    try:
        if not path.is_file():
            return ""
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > 262144:
                handle.seek(size - 262144)
            raw = handle.read().decode("utf-8", "ignore")
    except OSError:
        return ""
    text = ""
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("role") != "assistant":
            continue
        message = row.get("message") if isinstance(row.get("message"), dict) else row
        content = message.get("content") if isinstance(message, dict) else None
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
        if parts:
            text = "\n".join(part for part in parts if part).strip()
    if len(text) > EXCERPT_LIMIT:
        return text[-EXCERPT_LIMIT:]
    return text


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def record_stop(
    payload: dict,
    decision: str,
    result: dict,
    ledger: dict | None = None,
    error: str | None = None,
) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    excerpt = last_assistant_text(payload.get("transcript_path"))
    quit_shape = classify_quit(excerpt)
    gaps = work_gaps(ledger) if ledger else []
    proved = len(ledger.get("evidence") or []) if ledger else 0
    done_count = len(ledger.get("done_when") or []) if ledger else 0
    stalls = int(ledger.get("stalls") or 0) if ledger else 0
    gap = skill_gap(decision, quit_shape, stalls, len(gaps))
    loop_count = payload.get("loop_count")
    record = {
        "at": now,
        "decision": decision,
        "gap": gap,
        "terminal": gap is not None,
        "returned_followup": bool(result.get("followup_message")),
        "turn_status": payload.get("status"),
        "loop_count": loop_count if isinstance(loop_count, int) else None,
        "near_cursor_loop_limit": isinstance(loop_count, int) and loop_count >= 45,
        "followups": ledger.get("followups") if ledger else None,
        "stalls": stalls if ledger else None,
        "model": payload.get("model") or payload.get("model_id"),
        "cursor_version": payload.get("cursor_version"),
        "conversation_id": payload.get("conversation_id"),
        "bound_conversation_id": ledger.get("conversation_id") if ledger else None,
        "generation_id": payload.get("generation_id"),
        "goal": (ledger.get("goal") or "")[:300] if ledger else None,
        "unproved": gaps[:12],
        "remaining": (ledger.get("remaining") or [])[:6] if ledger else [],
        "proved": proved,
        "done_when_count": done_count,
        "blocked_reason": ledger.get("blocked_reason") if ledger else None,
        "quit_shape": quit_shape,
        "last_assistant": excerpt,
        "error": (error or "")[:400] or None,
        "transcript_path": payload.get("transcript_path"),
    }
    heartbeat = {
        "at": now,
        "conversation_id": payload.get("conversation_id"),
        "turn_status": payload.get("status"),
        "loop_count": record["loop_count"],
        "had_goal": ledger is not None or bool(error),
        "decision": decision,
    }
    _write(directory / "hook-heartbeat.json", heartbeat)
    if decision == "idle":
        return
    _write(directory / "last-stop.json", record)
    with (directory / "trace.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    if gap is not None:
        _write(directory / "last-failure.json", record)
