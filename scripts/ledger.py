"""The goal ledger: what finished means for one until-done goal.

The CLI, the gate, and the stop hook all read the same file, so a turn
cannot talk itself into being done. Each goal is one JSON file under
~/.until-done/goals/ (or $UNTIL_DONE_HOME/goals/), so several chats can
each carry their own goal at the same time.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

VERSION = 2
DEFAULT_MAX_RESUMES = 40
HARD_MAX_RESUMES = 300
DEFAULT_MAX_STALLS = 5
KEEP_NOTES = 40

# Blocks the agent may set. Anything else is a permission ask and does not stop the loop.
AGENT_BLOCKS = ("needs-user", "destructive", "safety", "oversight")
# Blocks only the stop hook sets.
HOOK_BLOCKS = ("cap", "stuck")
STATUSES = ("active", "done", "blocked", "stopped")


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def home() -> Path:
    override = os.environ.get("UNTIL_DONE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".until-done"


def goals_dir() -> Path:
    return home() / "goals"


def goal_path(goal_id: str) -> Path:
    return goals_dir() / f"{goal_id}.json"


def new_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return f"g{stamp}-{secrets.token_hex(3)}"


def same_path(left: object, right: object) -> bool:
    if not isinstance(left, str) or not isinstance(right, str) or not left or not right:
        return False
    try:
        a = Path(left).expanduser().resolve()
        b = Path(right).expanduser().resolve()
    except OSError:
        return False
    if os.name == "nt":
        return str(a).lower() == str(b).lower()
    return a == b


def block_kind(reason: object) -> str | None:
    """Return the block prefix when the reason names a real block with some detail."""
    if not isinstance(reason, str):
        return None
    head, sep, rest = reason.strip().partition(":")
    kind = head.strip().lower()
    if not sep or not rest.strip():
        return None
    if kind in AGENT_BLOCKS or kind in HOOK_BLOCKS:
        return kind
    return None


def is_real_block(reason: object) -> bool:
    return block_kind(reason) is not None


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _count(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _criterion(item: object) -> dict | None:
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, dict):
        return None
    text = _text(item.get("text"))
    if text is None:
        return None
    proof = _text(item.get("proof"))
    ok = item.get("ok") is True and proof is not None
    return {
        "text": text,
        "check": _text(item.get("check")),
        "ok": ok,
        "proof": proof if ok else None,
        "at": _text(item.get("at")) if ok else None,
        "last_failure": None if ok else _text(item.get("last_failure")),
    }


def normalize(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("the goal file is not a JSON object")
    if data.get("version") != VERSION:
        raise ValueError(f"version must be {VERSION}")
    goal_id = _text(data.get("id"))
    if goal_id is None:
        raise ValueError("id is required")
    goal = _text(data.get("goal"))
    if goal is None:
        raise ValueError("goal is required")
    raw_criteria = data.get("criteria")
    if not isinstance(raw_criteria, list):
        raise ValueError("criteria must be a list")
    criteria = [c for c in (_criterion(item) for item in raw_criteria) if c is not None]
    status = data.get("status") or "active"
    if status not in STATUSES:
        raise ValueError("status must be active, done, blocked, or stopped")
    max_resumes = _count(data.get("max_resumes"), DEFAULT_MAX_RESUMES)
    return {
        "version": VERSION,
        "id": goal_id,
        "status": status,
        "goal": goal,
        "workspace": _text(data.get("workspace")),
        "harness": _text(data.get("harness")),
        "session_id": _text(data.get("session_id")),
        "criteria": criteria,
        "steps": _strings(data.get("steps")),
        "non_goals": _strings(data.get("non_goals")),
        "authorized": _strings(data.get("authorized")),
        "decisions": _strings(data.get("decisions")),
        "notes": _strings(data.get("notes"))[-KEEP_NOTES:],
        "blocked_reason": _text(data.get("blocked_reason")),
        "max_resumes": max(1, min(HARD_MAX_RESUMES, max_resumes or DEFAULT_MAX_RESUMES)),
        "max_stalls": max(2, _count(data.get("max_stalls"), DEFAULT_MAX_STALLS)),
        "resumes": _count(data.get("resumes")),
        "stalls": _count(data.get("stalls")),
        "last_fingerprint": _text(data.get("last_fingerprint")),
        "created_at": _text(data.get("created_at")) or now(),
        "updated_at": _text(data.get("updated_at")) or now(),
    }


def unproved(ledger: dict) -> list[tuple[int, dict]]:
    return [(index, item) for index, item in enumerate(ledger["criteria"], 1) if not item["ok"]]


def open_items(ledger: dict) -> list[str]:
    """Every reason the goal is not finished, as lines a person or agent can act on."""
    items: list[str] = []
    if not ledger["criteria"]:
        items.append("no done-when criteria: add at least one with `goal.py criterion`")
    for index, item in unproved(ledger):
        line = f"[{index}] {item['text']}"
        if item["check"]:
            line += f"  (check: {item['check']})"
        if item["last_failure"]:
            line += f"  last run: {item['last_failure']}"
        items.append(line)
    items.extend(f"step: {step}" for step in ledger["steps"])
    return items


def next_action(ledger: dict) -> str:
    if ledger["steps"]:
        return ledger["steps"][0]
    if not ledger["criteria"]:
        return "add done-when criteria"
    pending = unproved(ledger)
    if pending:
        index, item = pending[0]
        if item["check"]:
            return f"make criterion [{index}] pass, then run `goal.py check {index}`"
        return f"satisfy criterion [{index}], then record it with `goal.py prove {index} \"<proof>\"`"
    return "run `goal.py close` to re-run the checks and finish"


def verdict(ledger: dict) -> str:
    """CONTINUE, DONE, BLOCKED, or STOPPED. Only the CLI's close sets status to done."""
    if ledger["status"] == "stopped":
        return "STOPPED"
    if is_real_block(ledger["blocked_reason"]):
        return "BLOCKED"
    if ledger["status"] == "done" and not open_items(ledger):
        return "DONE"
    return "CONTINUE"


def fingerprint(ledger: dict) -> str:
    payload = {
        "goal": ledger["goal"],
        "criteria": [(c["text"], c["check"], c["ok"], c["proof"], c["last_failure"]) for c in ledger["criteria"]],
        "steps": ledger["steps"],
        "blocked_reason": ledger["blocked_reason"],
        "notes": ledger["notes"][-1:],
        "note_count": len(ledger["notes"]),
        "decisions": len(ledger["decisions"]),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load(path: Path) -> tuple[dict | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        return normalize(json.loads(path.read_text(encoding="utf-8"))), None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return None, str(exc)


def save(ledger: dict, path: Path | None = None) -> Path:
    clean = normalize(ledger)
    clean["updated_at"] = now()
    target = path or goal_path(clean["id"])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Windows refuses to replace a file another process has open, so retry briefly.
    for attempt in range(5):
        try:
            temporary.replace(target)
            break
        except PermissionError:
            if attempt == 4:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.1 * (attempt + 1))
    ledger.update(clean)
    return target


def all_goals() -> list[tuple[Path, dict | None, str | None]]:
    directory = goals_dir()
    if not directory.is_dir():
        return []
    found = []
    for path in directory.glob("*.json"):
        ledger, error = load(path)
        found.append((path, ledger, error))
    found.sort(key=lambda row: (row[1] or {}).get("updated_at") or "", reverse=True)
    return found


def live(ledger: dict) -> bool:
    return ledger["status"] in ("active", "blocked")
