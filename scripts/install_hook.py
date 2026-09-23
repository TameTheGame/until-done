#!/usr/bin/env python3
"""Install the babysit stop hook for this user and copy the runtime scripts."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

HOOK_KEY = "stop"
LOOP_LIMIT = 48
RUNTIME_NAMES = ("ledger.py", "gate.py", "stop_hook.py", "install_hook.py", "report.py")


def runtime_dir() -> Path:
    return Path.home() / ".cursor" / "babysit"


def hooks_path() -> Path:
    return Path.home() / ".cursor" / "hooks.json"


def hook_command() -> str:
    script = runtime_dir() / "stop_hook.py"
    return f"{sys.executable} {script}"


def merge_hooks(existing: dict | None, command: str) -> dict:
    document = existing if isinstance(existing, dict) else {}
    version = document.get("version", 1)
    if not isinstance(version, int) or version < 1:
        version = 1
    hooks = document.get("hooks")
    hooks = dict(hooks) if isinstance(hooks, dict) else {}
    current = hooks.get(HOOK_KEY)
    entries = list(current) if isinstance(current, list) else []
    kept = []
    replaced = False
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        command_text = entry.get("command")
        if isinstance(command_text, str) and "stop_hook.py" in command_text:
            updated = dict(entry)
            updated["command"] = command
            updated["loop_limit"] = LOOP_LIMIT
            updated["timeout"] = 15
            kept.append(updated)
            replaced = True
        else:
            kept.append(entry)
    if not replaced:
        kept.append({"command": command, "loop_limit": LOOP_LIMIT, "timeout": 15})
    hooks[HOOK_KEY] = kept
    merged = dict(document)
    merged["version"] = version
    merged["hooks"] = hooks
    return merged


def install() -> Path:
    source = Path(__file__).resolve().parent
    destination = runtime_dir()
    destination.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_NAMES:
        shutil.copy2(source / name, destination / name)
        (destination / name).chmod(0o755)
    path = hooks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = None
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
    merged = merge_hooks(existing, hook_command())
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return path


def main() -> int:
    path = install()
    sys.stdout.write(f"Installed babysit stop hook in {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
