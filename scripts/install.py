#!/usr/bin/env python3
"""Install the until-done skill and its hooks for Cursor and/or Claude Code.

    python3 scripts/install.py              install for every harness found
    python3 scripts/install.py --cursor     only Cursor
    python3 scripts/install.py --claude     only Claude Code
    python3 scripts/install.py --dry-run    show what would change
    python3 scripts/install.py --uninstall  remove the hooks (skill copies stay)

It copies the skill to ~/.cursor/skills/until-done and/or
~/.claude/skills/until-done, then points a stop hook (and a hook that
re-injects the goal after compaction) at that copy. Other hooks and settings
are kept. The first write to a settings file leaves a .until-done.bak copy.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

NAME = "until-done"
MARKERS = ("until-done", "babysit")  # entries this installer owns, including the old babysit hook
HOOK_FILES = ("hook.py", "stop_hook.py")
CURSOR_LOOP_LIMIT = None  # no Cursor-side cap: the goal file's max_resumes is the limit
HOOK_TIMEOUT = 30
SKILL_FILES = ("SKILL.md", "README.md")
SCRIPT_FILES = ("ledger.py", "goal.py", "hook.py", "report.py", "install.py", "supervise.py")


def source_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def skill_dir(harness: str) -> Path:
    base = Path.home() / (".cursor" if harness == "cursor" else ".claude")
    return base / "skills" / NAME


def portable(path: Path | str) -> str:
    # Forward slashes work in sh, Git Bash, cmd, PowerShell, and Python on Windows.
    return Path(path).as_posix() if os.name == "nt" else str(path)


def command_line(python: str, script: Path, mode: str, harness: str, powershell: bool = False) -> str:
    command = f'"{portable(python)}" "{portable(script)}" {mode} --harness {harness}'
    return f"& {command}" if powershell else command


def owns(command: object) -> bool:
    if not isinstance(command, str):
        return False
    lowered = command.replace("\\", "/").lower()
    return any(marker in lowered for marker in MARKERS) and any(name in lowered for name in HOOK_FILES)


def merge_cursor(existing: dict | None, stop: str, context: str) -> dict:
    document = dict(existing) if isinstance(existing, dict) else {}
    hooks = dict(document.get("hooks")) if isinstance(document.get("hooks"), dict) else {}
    for event, command in (("stop", stop), ("sessionStart", context)):
        entries = hooks.get(event) if isinstance(hooks.get(event), list) else []
        kept = [entry for entry in entries if not (isinstance(entry, dict) and owns(entry.get("command")))]
        if command:
            entry: dict = {"command": command, "timeout": HOOK_TIMEOUT}
            if event == "stop":
                entry["loop_limit"] = CURSOR_LOOP_LIMIT
            kept.append(entry)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    version = document.get("version")
    document["version"] = version if isinstance(version, int) and version >= 1 else 1
    document["hooks"] = hooks
    return document


def merge_claude(existing: dict | None, stop: str, context: str, shell: str | None = None) -> dict:
    document = dict(existing) if isinstance(existing, dict) else {}
    hooks = dict(document.get("hooks")) if isinstance(document.get("hooks"), dict) else {}
    for event, command, matcher in (("Stop", stop, None), ("SessionStart", context, "compact|resume")):
        groups = hooks.get(event) if isinstance(hooks.get(event), list) else []
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                kept.append(group)
                continue
            inner = [h for h in group["hooks"] if not (isinstance(h, dict) and owns(h.get("command")))]
            if inner:
                kept.append({**group, "hooks": inner})
        if command:
            handler: dict = {"type": "command", "command": command, "timeout": HOOK_TIMEOUT}
            if shell:
                handler["shell"] = shell
            group = {"hooks": [handler]}
            if matcher:
                group = {"matcher": matcher, **group}
            kept.append(group)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if hooks:
        document["hooks"] = hooks
    else:
        document.pop("hooks", None)
    return document


def git_bash() -> bool:
    if os.name != "nt":
        return True
    configured = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    candidates = [configured] if configured else []
    candidates += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]
    return any(path and Path(path).is_file() for path in candidates) or shutil.which("bash") is not None


def copy_skill(destination: Path, dry_run: bool) -> list[str]:
    source = source_dir()
    if destination.resolve() == source:
        return [f"skill already lives at {destination}"]
    if dry_run:
        return [f"would copy the skill to {destination}"]
    (destination / "scripts").mkdir(parents=True, exist_ok=True)
    for name in SKILL_FILES:
        if (source / name).is_file():
            shutil.copy2(source / name, destination / name)
    for name in SCRIPT_FILES:
        if (source / "scripts" / name).is_file():
            shutil.copy2(source / "scripts" / name, destination / "scripts" / name)
    return [f"copied the skill to {destination}"]


def read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return None
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def write_json(path: Path, data: dict, dry_run: bool) -> str:
    if dry_run:
        return f"would update {path}:\n{json.dumps(data, indent=2)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".until-done.bak")
    if path.is_file() and not backup.exists():
        shutil.copy2(path, backup)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return f"updated {path}"


def install(harness: str, dry_run: bool, uninstall: bool) -> list[str]:
    lines: list[str] = []
    target = skill_dir(harness)
    script = (target / "scripts" / "hook.py") if target.resolve() != source_dir() else source_dir() / "scripts" / "hook.py"
    if not uninstall:
        lines += copy_skill(target, dry_run)
    if harness == "cursor":
        settings = Path.home() / ".cursor" / "hooks.json"
        stop = "" if uninstall else command_line(sys.executable, script, "stop", "cursor")
        context = "" if uninstall else command_line(sys.executable, script, "context", "cursor")
        lines.append(write_json(settings, merge_cursor(read_json(settings), stop, context), dry_run))
        old = Path.home() / ".cursor" / "skills" / "babysit"
        if old.is_dir():
            lines.append(f"note: the old skill at {old} gives conflicting instructions. Delete it when you are ready.")
    else:
        settings = Path.home() / ".claude" / "settings.json"
        bash = git_bash()
        shell = None if bash else "powershell"
        stop = "" if uninstall else command_line(sys.executable, script, "stop", "claude", powershell=not bash)
        context = "" if uninstall else command_line(sys.executable, script, "context", "claude", powershell=not bash)
        lines.append(write_json(settings, merge_claude(read_json(settings), stop, context, shell), dry_run))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the until-done skill and hooks.")
    parser.add_argument("--cursor", action="store_true", help="install for Cursor")
    parser.add_argument("--claude", action="store_true", help="install for Claude Code")
    parser.add_argument("--dry-run", action="store_true", help="print the changes without writing")
    parser.add_argument("--uninstall", action="store_true", help="remove the hooks this installer added")
    args = parser.parse_args(argv)
    harnesses = [name for name in ("cursor", "claude") if getattr(args, name)]
    if not harnesses:
        harnesses = [name for name in ("cursor", "claude") if (Path.home() / f".{name}").is_dir()]
    if not harnesses:
        sys.stdout.write("Neither ~/.cursor nor ~/.claude exists. Pass --cursor or --claude.\n")
        return 2
    try:
        for harness in harnesses:
            for line in install(harness, args.dry_run, args.uninstall):
                sys.stdout.write(f"[{harness}] {line}\n")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stdout.write(f"error: {exc}. Nothing else was changed.\n")
        return 1
    if not args.dry_run and not args.uninstall:
        sys.stdout.write("Done. Restart Cursor or Claude Code so they load the hooks, then try /until-done.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
