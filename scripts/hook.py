#!/usr/bin/env python3
"""Resume the chat while its until-done goal is still open.

    hook.py stop --harness cursor|claude   the end-of-turn gate
    hook.py context --harness claude       SessionStart: re-inject the goal after compaction

One script serves two harnesses:
  Cursor       stop hook  -> prints {"followup_message": ...} to continue
  Claude Code  Stop hook  -> prints {"decision": "block", "reason": ...} to continue
Printing {} lets the turn end. Stdout is JSON only. Every stop is recorded
by report.py so a run that quit early can be explained afterwards.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger as L  # noqa: E402
from report import record_stop  # noqa: E402

SHOW_OPEN = 10
SCAN_LIMIT = 64 * 1024 * 1024


@dataclass
class Event:
    harness: str
    session_id: str | None
    status: str
    roots: list[str] = field(default_factory=list)
    loop_count: int | None = None
    transcript_path: str | None = None
    last_message: str | None = None
    continuing: bool = False
    raw: dict = field(default_factory=dict)


def running_in_cursor(payload: dict) -> bool:
    return "cursor_version" in payload or bool(os.environ.get("CURSOR_VERSION"))


def read_event(payload: dict, harness: str | None) -> Event:
    if harness is None:
        harness = "cursor" if running_in_cursor(payload) else "claude"
    transcript = payload.get("transcript_path")
    transcript = transcript if isinstance(transcript, str) and transcript.strip() else None
    if harness == "claude":
        cwd = payload.get("cwd")
        last = payload.get("last_assistant_message")
        return Event(
            harness="claude",
            session_id=L._text(payload.get("session_id")),
            status="completed",
            roots=[cwd] if isinstance(cwd, str) and cwd else [],
            transcript_path=transcript,
            last_message=last if isinstance(last, str) else None,
            continuing=payload.get("stop_hook_active") is True,
            raw=payload,
        )
    roots = payload.get("workspace_roots")
    loop_count = payload.get("loop_count")
    return Event(
        harness="cursor",
        session_id=L._text(payload.get("conversation_id")),
        status=str(payload.get("status") or "completed"),
        roots=[str(root) for root in roots] if isinstance(roots, list) else [],
        loop_count=loop_count if isinstance(loop_count, int) else None,
        transcript_path=transcript,
        continuing=isinstance(loop_count, int) and loop_count > 0,
        raw=payload,
    )


def reply(event: Event, message: str | None) -> dict:
    if not message:
        return {}
    if event.harness == "claude":
        return {"decision": "block", "reason": message}
    return {"followup_message": message}


def workspace_matches(ledger: dict, roots: list[str]) -> bool:
    workspace = ledger["workspace"]
    if not workspace or not roots:
        return True
    target = Path(workspace).expanduser()
    for root in roots:
        base = Path(root).expanduser()
        try:
            if L.same_path(str(base), str(target)):
                return True
            if target.resolve().is_relative_to(base.resolve()) or base.resolve().is_relative_to(target.resolve()):
                return True
        except (OSError, ValueError):
            continue
    return False


def transcript_mentions(path: str | None, needle: str) -> bool | None:
    """True or False when the transcript could be read, None when it could not."""
    if not path:
        return None
    token = needle.encode("utf-8")
    overlap = b""
    scanned = 0
    try:
        with open(path, "rb") as handle:
            while scanned < SCAN_LIMIT:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    return False
                if token in overlap + chunk:
                    return True
                overlap = chunk[-len(token):]
                scanned += len(chunk)
    except OSError:
        return None
    return False


def pick_goal(event: Event) -> tuple[Path, dict] | tuple[None, None]:
    rows = [(path, ledger) for path, ledger, _ in L.all_goals() if ledger is not None]
    forced = os.environ.get("UNTIL_DONE_GOAL")
    if forced:
        for path, ledger in rows:
            if ledger["id"] == forced:
                return path, ledger
        return None, None
    if not event.session_id:
        return None, None
    for path, ledger in rows:
        if ledger["session_id"] == event.session_id and L.live(ledger):
            return path, ledger
    unbound = [
        (path, ledger)
        for path, ledger in rows
        if ledger["session_id"] is None and ledger["status"] == "active" and workspace_matches(ledger, event.roots)
    ]
    # Bind only the goal this chat created: its id is in this chat's transcript.
    readable = False
    for path, ledger in unbound:
        seen = transcript_mentions(event.transcript_path, ledger["id"])
        readable = readable or seen is not None
        if seen:
            return path, ledger
    if not readable and len(unbound) == 1:
        return unbound[0]
    return None, None


def invalid_goal_for(event: Event) -> tuple[Path, str] | None:
    if not event.session_id:
        return None
    for path, ledger, error in L.all_goals():
        if ledger is None and error:
            try:
                if event.session_id in path.read_text(encoding="utf-8", errors="ignore"):
                    return path, error
            except OSError:
                continue
    return None


def cli(goal_id: str) -> str:
    script = Path(__file__).resolve().parent / "goal.py"
    return f'"{sys.executable}" "{script}" <command> --id {goal_id}'


def task_block(ledger: dict) -> list[str]:
    items = L.open_items(ledger)
    lines = [f'<goal id="{ledger["id"]}">', f"goal: {ledger['goal']}", "open:"]
    lines.extend(f"- {item}" for item in items[:SHOW_OPEN])
    if len(items) > SHOW_OPEN:
        lines.append(f"- ... and {len(items) - SHOW_OPEN} more")
    if not items:
        lines.append("- nothing: every criterion is proved")
    lines.append(f"next: {L.next_action(ledger)}")
    lines.append("</goal>")
    return lines


def elapsed(since: str) -> str:
    try:
        start = datetime.fromisoformat(since)
    except ValueError:
        return "unknown"
    minutes = max(0, int((datetime.now(timezone.utc) - start).total_seconds() // 60))
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


RULES = (
    "Rules: record each step with `did`, `note`, `prove N`, or `check N`. Pick reasonable defaults yourself and "
    "log them with `decide`. Fix the work, never the checks. Before ending a turn, if your last paragraph is a plan, "
    "a question, or a promise, do it now instead. Use `block` only for needs-user:, destructive:, safety:, or "
    "oversight:. When nothing is open, run `close`."
)


def resume_message(ledger: dict, event: Event) -> str:
    lines = [
        f"Until-done resume {ledger['resumes']}/{ledger['max_resumes']}, {elapsed(ledger['created_at'])} in. "
        f"Goal {ledger['id']} is still open.",
        "Do the next step with tools now. Do not ask permission to continue and do not end on a recap or plan.",
        f"CLI: {cli(ledger['id'])}",
        RULES,
    ]
    if event.status == "error":
        lines.append("The last turn ended with an error. Pick up from the next open step.")
    stalls = ledger["stalls"]
    if stalls >= 2:
        lines.append(
            f"The goal file has not changed for {stalls} resumes. Do not repeat the same step. "
            "Try a different approach and `note` what you tried."
        )
    if stalls >= max(3, ledger["max_stalls"] // 2):
        lines.append(
            "If one item keeps failing, hand it to a fresh subagent with the error and what you tried, "
            "or re-plan with `todo`. If only the user can unblock it, `block` with needs-user:."
        )
    lines.append("")
    lines.extend(task_block(ledger))
    return "\n".join(lines)


def final_message(ledger: dict, why: str) -> str:
    lines = [
        f"Until-done pause. {why}",
        "Do not keep working on the goal. In this reply, tell the user plainly: what is proved, what is still open, "
        "what you tried on the open items, and what they can say to continue (for example \"continue\", which you "
        "handle with `goal.py unblock --resumes 20`).",
        "",
    ]
    lines.extend(task_block(ledger))
    return "\n".join(lines)


def duplicate(payload: dict, harness: str | None) -> bool:
    # Cursor also loads Claude Code hooks from ~/.claude/settings.json. The Cursor
    # entry in ~/.cursor/hooks.json handles Cursor, so the Claude entry stands down
    # there instead of counting every resume twice.
    return harness == "claude" and running_in_cursor(payload)


def handle(payload: dict, harness: str | None = None) -> dict:
    if os.environ.get("UNTIL_DONE_HOOK") == "off" or duplicate(payload, harness):
        # supervise.py runs each session once and does the looping itself.
        return {}
    event = read_event(payload, harness)

    def finish(decision: str, message: str | None = None, ledger: dict | None = None, error: str | None = None) -> dict:
        result = reply(event, message)
        try:
            record_stop(event, decision, result, ledger, error)
        except Exception as exc:  # the record must never break the hook
            sys.stderr.write(f"until-done report: {exc}\n")
        return result

    if event.status == "aborted":
        path, ledger = pick_goal(event)
        return finish("aborted" if ledger else "idle", ledger=ledger)
    if event.status not in ("completed", "error"):
        return finish("ignored_status")

    path, ledger = pick_goal(event)
    if ledger is None:
        broken = invalid_goal_for(event)
        if broken and not (event.loop_count or 0) >= 3 and not (event.harness == "claude" and event.continuing):
            path, error = broken
            return finish(
                "invalid_goal",
                f"Until-done resume. The goal file {path} is invalid ({error}). Fix the JSON so `goal.py status` "
                "reads it, then continue the goal. Do not ask permission to continue.",
                error=error,
            )
        return finish("invalid_goal" if broken else "idle", error=broken[1] if broken else None)

    if ledger["session_id"] is None and event.session_id:
        ledger["session_id"] = event.session_id
        ledger["harness"] = event.harness
        for other_path, other, _ in L.all_goals():
            if other and other["id"] != ledger["id"] and other["session_id"] == event.session_id and L.live(other):
                other["status"] = "stopped"
                other["notes"].append(f"replaced by {ledger['id']}")
                L.save(other, other_path)

    verdict = L.verdict(ledger)
    if verdict in ("DONE", "STOPPED"):
        L.save(ledger, path)
        return finish(verdict.lower(), ledger=ledger)
    if verdict == "BLOCKED":
        ledger["status"] = "blocked"
        L.save(ledger, path)
        return finish("blocked", ledger=ledger)
    if ledger["blocked_reason"]:
        ledger["blocked_reason"] = None
    ledger["status"] = "active"

    current = L.fingerprint(ledger)
    ledger["stalls"] = ledger["stalls"] + 1 if current == ledger["last_fingerprint"] else 0
    ledger["last_fingerprint"] = current

    if ledger["resumes"] >= ledger["max_resumes"]:
        ledger["status"] = "blocked"
        ledger["blocked_reason"] = f"cap: used all {ledger['max_resumes']} automatic resumes"
        L.save(ledger, path)
        return finish("cap", final_message(ledger, f"All {ledger['max_resumes']} automatic resumes are used."), ledger)
    if ledger["stalls"] >= ledger["max_stalls"]:
        ledger["status"] = "blocked"
        ledger["blocked_reason"] = f"stuck: the goal file did not change for {ledger['stalls']} resumes"
        L.save(ledger, path)
        return finish("stuck", final_message(ledger, "The goal stopped making recorded progress."), ledger)

    ledger["resumes"] += 1
    message = resume_message(ledger, event)
    L.save(ledger, path)
    return finish("resumed", message, ledger)


def context(payload: dict, harness: str | None = None) -> dict:
    """After compaction or a resumed session, put this chat's goal back in front of the agent."""
    if duplicate(payload, harness):
        return {}
    event = read_event(payload, harness)
    path, ledger = pick_goal(event)
    if ledger is None or not L.live(ledger):
        return {}
    text = "\n".join(
        [
            f"An until-done goal is active in this chat. Keep working on it. CLI: {cli(ledger['id'])}",
            *task_block(ledger),
        ]
    )
    if event.harness == "claude":
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    return {"additional_context": text}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode = args.pop(0) if args and not args[0].startswith("-") else "stop"
    harness = None
    if "--harness" in args:
        index = args.index("--harness")
        harness = args[index + 1] if index + 1 < len(args) else None
    harness = harness if harness in ("cursor", "claude") else None
    raw = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        result = context(payload, harness) if mode == "context" else handle(payload, harness)
    except Exception as exc:
        sys.stderr.write(f"until-done hook: {exc}\n")
        try:
            record_stop(read_event(payload, harness), "hook_error", {}, None, str(exc))
        except Exception as report_error:
            sys.stderr.write(f"until-done report: {report_error}\n")
        result = {}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
