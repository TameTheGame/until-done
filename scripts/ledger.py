"""Shared rules for the babysit goal.

The ledger is the record of what finished means. The stop hook and the gate
both use it, so a turn cannot talk itself into being done.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DEFAULT_MAX_FOLLOWUPS = 24
HARD_MAX_FOLLOWUPS = 200

REAL_BLOCK_PREFIXES = (
    "needs-user:",
    "safety:",
    "destructive:",
    "oversight:",
    "cap:",
)


def ledger_path() -> Path:
    override = os.environ.get("BABYSIT_LEDGER")
    if override:
        return Path(override)
    return Path.home() / ".cursor" / "babysit" / "active.json"


def is_real_block(reason: object) -> bool:
    if not isinstance(reason, str):
        return False
    text = reason.strip().lower()
    return any(text.startswith(prefix) for prefix in REAL_BLOCK_PREFIXES)


def work_gaps(ledger: dict) -> list[str]:
    evidenced = {
        item["criterion"]
        for item in ledger.get("evidence") or []
        if isinstance(item, dict) and item.get("criterion") and str(item.get("proof") or "").strip()
    }
    gaps: list[str] = []
    done_when = ledger.get("done_when") or []
    if not done_when:
        gaps.append("done_when is empty")
    for line in done_when:
        if line not in evidenced:
            gaps.append(line)
    for item in ledger.get("remaining") or []:
        if item not in gaps:
            gaps.append(item)
    return gaps


def fingerprint(ledger: dict) -> str:
    payload = {
        "goal": ledger.get("goal"),
        "done_when": ledger.get("done_when"),
        "evidence": ledger.get("evidence"),
        "remaining": ledger.get("remaining"),
        "blocked_reason": ledger.get("blocked_reason"),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def normalize(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("ledger is not an object")
    if data.get("version") != 1:
        raise ValueError("version must be 1")
    status = data.get("status") or "active"
    if status not in ("active", "done", "blocked"):
        raise ValueError("status must be active, done, or blocked")
    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal is required")
    done_when = data.get("done_when")
    if not isinstance(done_when, list) or not all(isinstance(item, str) and item.strip() for item in done_when):
        raise ValueError("done_when must be a list of strings")
    remaining_raw = data.get("remaining") if isinstance(data.get("remaining"), list) else []
    if not all(isinstance(item, str) for item in remaining_raw):
        raise ValueError("remaining must be a list of strings")
    evidence = []
    for item in data.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        criterion = item.get("criterion")
        proof = item.get("proof")
        if isinstance(criterion, str) and isinstance(proof, str) and proof.strip():
            evidence.append({"criterion": criterion, "proof": proof.strip()})
    max_followups = data.get("max_followups", DEFAULT_MAX_FOLLOWUPS)
    if not isinstance(max_followups, int):
        max_followups = DEFAULT_MAX_FOLLOWUPS
    max_followups = max(1, min(HARD_MAX_FOLLOWUPS, max_followups))
    followups = data.get("followups") or 0
    if not isinstance(followups, int) or followups < 0:
        followups = 0
    stalls = data.get("stalls") or 0
    if not isinstance(stalls, int) or stalls < 0:
        stalls = 0
    blocked = data.get("blocked_reason")
    if not isinstance(blocked, str):
        blocked = None
    conversation_id = data.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        conversation_id = None
    workspace = data.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        workspace = None
    last_fingerprint = data.get("last_fingerprint")
    if not isinstance(last_fingerprint, str):
        last_fingerprint = None
    return {
        "version": 1,
        "status": status,
        "conversation_id": conversation_id,
        "workspace": workspace,
        "goal": goal.strip(),
        "done_when": [item.strip() for item in done_when],
        "evidence": evidence,
        "remaining": [item.strip() for item in remaining_raw if item.strip()],
        "blocked_reason": blocked,
        "max_followups": max_followups,
        "followups": followups,
        "stalls": stalls,
        "last_fingerprint": last_fingerprint,
    }


def load_ledger(path: Path | None = None) -> tuple[Path, dict | None, str | None]:
    target = path or ledger_path()
    if not target.is_file():
        return target, None, None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return target, normalize(data), None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return target, None, str(exc)


def save_ledger(ledger: dict, path: Path | None = None) -> Path:
    target = path or ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(normalize(ledger), indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    return target
