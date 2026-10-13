#!/usr/bin/env python3
"""Run a goal unattended: start a fresh headless agent session until the goal is done.

    python3 supervise.py --agent claude [--id GOAL] -- --permission-mode auto
    python3 supervise.py --agent cursor [--id GOAL] -- --force

Each run gets a fresh context and the same instruction: read the goal, do the
next open items, record progress, end the turn. Between runs this script reads
the goal file, the same record the stop hook uses, and stops when it says DONE,
BLOCKED, or STOPPED, when two runs in a row change nothing, or when the run or
time budget is spent. Everything after `--` goes to the agent CLI unchanged.
Permission flags are your choice; without them most tools are denied in
headless mode and the goal will stall instead of doing damage.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger as L  # noqa: E402

AGENTS = {"claude": ("claude",), "cursor": ("cursor-agent", "agent")}
NO_PROGRESS_LIMIT = 2


def find_binary(agent: str, override: str | None) -> str | None:
    for name in [override] if override else AGENTS[agent]:
        found = shutil.which(name) or (name if Path(name).is_file() else None)
        if found:
            return found
    return None


def prompt(ledger: dict, run: int) -> str:
    script = Path(__file__).resolve().parent / "goal.py"
    cli = f'"{sys.executable}" "{script}"'
    return "\n".join(
        [
            f"Until-done resume, unattended run {run}. Use the until-done skill on goal {ledger['id']}.",
            "Nobody is watching this run and nobody can answer questions until it ends.",
            f"1. Run: {cli} status --id {ledger['id']}",
            "2. Read the open items and the notes (`show`), check the current state of the files, then do the next items.",
            "3. Record every step with `did`, `note`, `prove`, or `check`. Choices you make without asking go in `decide`.",
            "4. Keep going until `close` prints DONE. If only the user can unblock you, run `block` with needs-user: "
            "and end. Do not ask questions in text; they will not be answered.",
            "",
            f"goal: {ledger['goal']}",
        ]
    )


def run_once(command: list[str], workspace: str | None, goal_id: str, log: Path, minutes: int) -> int | None:
    env = dict(os.environ, UNTIL_DONE_GOAL=goal_id, UNTIL_DONE_HOOK="off")
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as handle:
        try:
            result = subprocess.run(
                command,
                cwd=workspace if workspace and Path(workspace).is_dir() else None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=minutes * 60,
            )
        except subprocess.TimeoutExpired:
            return None
        except OSError as exc:
            handle.write(f"could not start the agent: {exc}\n".encode("utf-8"))
            return -1
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, extra = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser(description="Run an until-done goal in fresh headless sessions until it is done.")
    parser.add_argument("--agent", choices=sorted(AGENTS), required=True)
    parser.add_argument("--id", help="goal id (default: the live goal for this folder)")
    parser.add_argument("--bin", help="path to the agent CLI, if it is not on PATH")
    parser.add_argument("--runs", type=int, default=20, help="most sessions to start (default 20)")
    parser.add_argument("--hours", type=float, default=8.0, help="wall-clock budget (default 8)")
    parser.add_argument("--run-minutes", type=int, default=90, help="kill a single session after this long (default 90)")
    args = parser.parse_args(argv)

    import goal as cli  # noqa: E402

    try:
        path, ledger = cli.resolve(args.id)
    except LookupError:
        sys.stdout.write("No live goal. Start one with goal.py start, or pass --id.\n")
        return 2
    except cli.UsageError as exc:
        sys.stdout.write(f"error: {exc}\n")
        return 2
    binary = find_binary(args.agent, args.bin)
    if binary is None:
        sys.stdout.write(f"Cannot find the {args.agent} CLI ({' or '.join(AGENTS[args.agent])}). Pass --bin.\n")
        return 2

    deadline = time.monotonic() + args.hours * 3600
    unchanged = 0
    for run in range(1, args.runs + 1):
        word = L.verdict(ledger)
        if word != "CONTINUE":
            break
        if time.monotonic() >= deadline:
            sys.stdout.write("Time budget spent.\n")
            break
        before = L.fingerprint(ledger)
        log = L.home() / "runs" / ledger["id"] / f"{run:03d}.log"
        command = [binary, "-p", prompt(ledger, run), *extra]
        sys.stdout.write(f"run {run}: {args.agent} (log: {log})\n")
        sys.stdout.flush()
        code = run_once(command, ledger["workspace"], ledger["id"], log, args.run_minutes)
        if code is None:
            sys.stdout.write(f"run {run}: killed after {args.run_minutes} minutes\n")
        elif code != 0:
            sys.stdout.write(f"run {run}: agent exited {code}\n")
        reloaded, error = L.load(path)
        if reloaded is None:
            sys.stdout.write(f"The goal file is unreadable now ({error}). Stopping.\n")
            return 1
        ledger = reloaded
        unchanged = unchanged + 1 if L.fingerprint(ledger) == before else 0
        ledger["notes"].append(f"unattended run {run} ended (exit {code})")
        L.save(ledger, path)
        if unchanged >= NO_PROGRESS_LIMIT:
            sys.stdout.write(f"{unchanged} runs in a row recorded no progress. Stopping. Check the logs.\n")
            break

    sys.stdout.write(cli.render(ledger) + "\n")
    return 0 if L.verdict(ledger) == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
