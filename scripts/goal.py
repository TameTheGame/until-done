#!/usr/bin/env python3
"""Command line for an until-done goal. Run `goal.py -h` for the commands.

The first word that `status`, `check`, and `close` print is the gate:
CONTINUE, DONE, BLOCKED, STOPPED, or ABSENT. CONTINUE means keep working
in this turn. Exit status is 0 unless the command itself was misused.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger as L  # noqa: E402

CHECK_TIMEOUT = 600
SHOW_OPEN = 12


class UsageError(Exception):
    pass


def cli_hint() -> str:
    return f'"{sys.executable}" "{Path(__file__).resolve()}"'


def clean_session(value: str | None) -> str | None:
    # An unexpanded placeholder means the harness did not fill it in.
    if not value or not value.strip() or "{" in value or value.strip().startswith("$"):
        return None
    return value.strip()


def inside(child: Path, parent: str | None) -> bool:
    if not parent:
        return False
    try:
        return child.resolve().is_relative_to(Path(parent).expanduser().resolve())
    except (OSError, ValueError):
        return False


def resolve(goal_id: str | None) -> tuple[Path, dict]:
    # The supervisor sets UNTIL_DONE_GOAL so headless runs never guess.
    goal_id = goal_id or os.environ.get("UNTIL_DONE_GOAL")
    if goal_id:
        path = L.goal_path(goal_id)
        ledger, error = L.load(path)
        if error:
            raise UsageError(f"{path} is invalid: {error}. Fix the JSON or start a new goal.")
        if ledger is None:
            raise UsageError(f"no goal {goal_id}. Run `goal.py list`.")
        return path, ledger
    rows = [(path, ledger) for path, ledger, _ in L.all_goals() if ledger and L.live(ledger)]
    if not rows:
        raise LookupError
    local = [row for row in rows if inside(Path.cwd(), row[1]["workspace"])]
    if len(local) == 1:
        return local[0]
    if not local and len(rows) == 1:
        return rows[0]
    # Never guess between goals: another chat may own the other one.
    listing = "\n".join(f"  {ledger['id']}  {ledger['workspace']}  {ledger['goal'][:60]}" for _, ledger in local or rows)
    raise UsageError(f"more than one live goal could be yours. Pass --id with your goal id:\n{listing}")


def render(ledger: dict) -> str:
    word = L.verdict(ledger)
    head = f"{word} {ledger['id']}: {ledger['goal'][:120]}"
    if word == "BLOCKED":
        return f"{head}\nreason: {ledger['blocked_reason']}"
    if word == "STOPPED":
        return head
    if word == "DONE":
        return f"{head}\n{report(ledger)}"
    items = L.open_items(ledger)
    lines = [head, f"open ({len(items)}):" if items else "every criterion is proved, but the goal is not closed"]
    lines.extend(f"- {item}" for item in items[:SHOW_OPEN])
    if len(items) > SHOW_OPEN:
        lines.append(f"- ... and {len(items) - SHOW_OPEN} more (`goal.py show`)")
    lines.append(f"next: {L.next_action(ledger)}")
    if ledger["blocked_reason"]:
        lines.append("The recorded block is not a real block. Run `goal.py unblock` and keep working.")
    return "\n".join(lines)


def report(ledger: dict) -> str:
    lines = ["proved:"]
    lines.extend(f"- [{i}] {c['text']} -- {c['proof']}" for i, c in enumerate(ledger["criteria"], 1))
    if ledger["decisions"]:
        lines.append("decided without asking (tell the user):")
        lines.extend(f"- {item}" for item in ledger["decisions"])
    if ledger["non_goals"]:
        lines.append("left out on purpose:")
        lines.extend(f"- {item}" for item in ledger["non_goals"])
    return "\n".join(lines)


def run_check(command: str, cwd: str | None, timeout: int) -> tuple[bool, str]:
    folder = cwd if cwd and Path(cwd).is_dir() else None
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=folder,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as exc:
        return False, f"could not run: {exc}"
    output = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace")
    tail = " | ".join(line.strip() for line in output.splitlines() if line.strip())[-300:]
    if result.returncode == 0:
        return True, f"`{command}` exited 0" + (f": {tail[-160:]}" if tail else "")
    return False, f"exit {result.returncode}: {tail}" if tail else f"exit {result.returncode}"


def run_checks(ledger: dict, numbers: list[int], timeout: int) -> list[str]:
    lines = []
    for number in numbers:
        item = ledger["criteria"][number - 1]
        ok, detail = run_check(item["check"], ledger["workspace"], timeout)
        item["ok"] = ok
        item["proof"] = detail if ok else None
        item["at"] = L.now() if ok else None
        item["last_failure"] = None if ok else detail
        lines.append(f"[{number}] {'PASS' if ok else 'FAIL'} {detail}")
    return lines


def criterion_number(ledger: dict, number: int) -> dict:
    if number < 1 or number > len(ledger["criteria"]):
        raise UsageError(f"there is no criterion [{number}]. Run `goal.py status`.")
    return ledger["criteria"][number - 1]


def spec_from(args: argparse.Namespace) -> dict:
    spec: dict = {}
    if args.spec:
        text = sys.stdin.read() if args.spec == "-" else Path(args.spec).read_text(encoding="utf-8")
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise UsageError("the spec must be a JSON object")
        spec = loaded
    criteria = list(spec.get("done_when") or spec.get("criteria") or [])
    criteria.extend(args.done or [])
    criteria.extend({"text": text, "check": command} for text, command in args.check or [])
    for item in criteria:
        if isinstance(item, dict) and (item.get("ok") or item.get("proof")):
            raise UsageError("a new goal cannot start with proof. Prove criteria after the work.")
    return {
        "goal": args.goal or spec.get("goal"),
        "criteria": criteria,
        "steps": list(spec.get("steps") or []) + (args.step or []),
        "non_goals": list(spec.get("non_goals") or []) + (args.non_goal or []),
        "authorized": list(spec.get("authorized") or []) + (args.allow or []),
        "max_resumes": args.max_resumes or spec.get("max_resumes"),
    }


def cmd_start(args: argparse.Namespace) -> str:
    spec = spec_from(args)
    if not spec["criteria"]:
        raise UsageError("give at least one done-when criterion (--done, --check, or done_when in the spec)")
    session = clean_session(args.session)
    ledger = L.normalize(
        {
            "version": L.VERSION,
            "id": L.new_id(),
            "status": "active",
            "workspace": str(Path(args.workspace or Path.cwd()).expanduser().resolve()),
            "session_id": session,
            **{key: value for key, value in spec.items() if value is not None},
        }
    )
    if not ledger["criteria"]:
        raise UsageError("every done-when criterion needs text")
    replaced = []
    if session:
        for path, other, _ in L.all_goals():
            if other and L.live(other) and other["session_id"] == session:
                other["status"] = "stopped"
                other["notes"].append(f"replaced by {ledger['id']}")
                L.save(other, path)
                replaced.append(other["id"])
    path = L.save(ledger)
    lines = [f"Started {ledger['id']} in {ledger['workspace']}", f"file: {path}", f"cli: {cli_hint()}"]
    if replaced:
        lines.append(f"replaced: {', '.join(replaced)}")
    return "\n".join(lines) + "\n" + render(ledger)


def cmd_status(args: argparse.Namespace) -> str:
    _, ledger = resolve(args.id)
    return render(ledger)


def cmd_prove(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    item = criterion_number(ledger, args.number)
    if item["check"]:
        raise UsageError(f"[{args.number}] has a check command. Run `goal.py check {args.number}` instead.")
    proof = args.proof.strip()
    if not proof:
        raise UsageError("the proof must quote what a tool showed")
    item.update(ok=True, proof=proof, at=L.now(), last_failure=None)
    L.save(ledger, path)
    return render(ledger)


def cmd_check(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    numbers = args.numbers or [i for i, c in enumerate(ledger["criteria"], 1) if c["check"]]
    for number in numbers:
        if not criterion_number(ledger, number)["check"]:
            raise UsageError(f"[{number}] has no check command. Use `goal.py prove {number} \"<proof>\"`.")
    if not numbers:
        return "No criterion has a check command.\n" + render(ledger)
    lines = run_checks(ledger, numbers, args.timeout)
    L.save(ledger, path)
    return render(ledger) + "\n" + "\n".join(lines)


def cmd_close(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    numbers = [i for i, c in enumerate(ledger["criteria"], 1) if c["check"]]
    lines = run_checks(ledger, numbers, args.timeout) if numbers else []
    if not L.open_items(ledger) and not L.is_real_block(ledger["blocked_reason"]):
        ledger["status"] = "done"
        ledger["blocked_reason"] = None
    elif ledger["status"] == "done":
        ledger["status"] = "active"
    L.save(ledger, path)
    return render(ledger) + ("\n" + "\n".join(lines) if lines else "")


def cmd_criterion(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    ledger["criteria"].append({"text": args.text, "check": args.check, "ok": False})
    if ledger["status"] == "done":
        ledger["status"] = "active"
    L.save(ledger, path)
    return render(ledger)


def cmd_drop(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    item = criterion_number(ledger, args.number)
    ledger["criteria"].remove(item)
    ledger["decisions"].append(f"dropped done-when \"{item['text']}\": {args.why}")
    L.save(ledger, path)
    return render(ledger)


def cmd_todo(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    if args.first:
        ledger["steps"].insert(0, args.text)
    else:
        ledger["steps"].append(args.text)
    if ledger["status"] == "done":
        ledger["status"] = "active"
    L.save(ledger, path)
    return render(ledger)


def cmd_did(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    finished = None
    if ledger["steps"]:
        if args.step < 1 or args.step > len(ledger["steps"]):
            raise UsageError(f"there is no step {args.step}")
        finished = ledger["steps"].pop(args.step - 1)
    if finished is None and not args.text:
        raise UsageError("no steps are open. Use `goal.py note` to log progress.")
    note = f"did: {finished}" if finished else ""
    if args.text:
        note = f"{note} -- {args.text}" if note else args.text
    ledger["notes"].append(note)
    L.save(ledger, path)
    return render(ledger)


def cmd_note(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    ledger["notes"].append(args.text)
    L.save(ledger, path)
    return render(ledger)


def cmd_decide(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    ledger["decisions"].append(args.text)
    L.save(ledger, path)
    return render(ledger)


def cmd_block(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    kind = L.block_kind(args.reason)
    if kind is None or kind in L.HOOK_BLOCKS:
        allowed = ", ".join(f"{name}:" for name in L.AGENT_BLOCKS)
        return (
            "NOT A BLOCK. Asking to continue, recapping, or wanting approval is not a reason to stop.\n"
            f"A block starts with one of {allowed} and names the exact fact or action. Keep working.\n"
            + render(ledger)
        )
    ledger["blocked_reason"] = args.reason.strip()
    ledger["status"] = "blocked"
    L.save(ledger, path)
    lines = [render(ledger)]
    if L.open_items(ledger):
        lines.append(
            "Before ending the turn, finish every open item that does not depend on this answer, "
            "then ask for everything you need in one message."
        )
    return "\n".join(lines)


def cmd_unblock(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    ledger["blocked_reason"] = None
    ledger["status"] = "active"
    ledger["stalls"] = 0
    if args.resumes:
        ledger["max_resumes"] = ledger["resumes"] + args.resumes
    L.save(ledger, path)
    return render(ledger)


def cmd_stop(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    ledger["status"] = "stopped"
    if args.reason:
        ledger["notes"].append(f"stopped: {args.reason}")
    L.save(ledger, path)
    return render(ledger)


def cmd_show(args: argparse.Namespace) -> str:
    path, ledger = resolve(args.id)
    return f"file: {path}\ncli: {cli_hint()}\n" + json.dumps(ledger, indent=2, ensure_ascii=False)


def cmd_list(args: argparse.Namespace) -> str:
    rows = L.all_goals()
    if not rows:
        return "No goals."
    lines = []
    for path, ledger, error in rows:
        if ledger is None:
            lines.append(f"INVALID  {path.name}  {error}")
            continue
        if not args.all and not L.live(ledger):
            continue
        lines.append(f"{L.verdict(ledger):9} {ledger['id']}  {ledger['workspace']}  {ledger['goal'][:60]}")
    return "\n".join(lines) or "No live goals. Use --all to see finished ones."


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="goal.py", description="Track one until-done goal and gate the end of a turn.")
    sub = top.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str, needs_id: bool = True) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text)
        if needs_id:
            command.add_argument("--id", help="goal id (default: the live goal for this folder)")
        command.set_defaults(handler=handler)
        return command

    start = add("start", cmd_start, "Start a goal from a JSON spec file, flags, or both.", needs_id=False)
    start.add_argument("spec", nargs="?", help="JSON spec file, or - for stdin")
    start.add_argument("--goal", help="the outcome, in the user's words")
    start.add_argument("--done", action="append", metavar="TEXT", help="a done-when criterion you will prove")
    start.add_argument("--check", action="append", nargs=2, metavar=("TEXT", "COMMAND"), help="a criterion proved by a command exiting 0")
    start.add_argument("--step", action="append", metavar="TEXT", help="a planned step, in order")
    start.add_argument("--non-goal", action="append", metavar="TEXT", help="something deliberately left out")
    start.add_argument("--allow", action="append", metavar="TEXT", help="an outward or destructive action the user approved")
    start.add_argument("--max-resumes", type=int, help=f"automatic resumes before pausing (default {L.DEFAULT_MAX_RESUMES})")
    start.add_argument("--session", help="the chat or session id, when the harness provides it")
    start.add_argument("--workspace", help="folder the checks run in (default: current folder)")

    add("status", cmd_status, "Print the gate: CONTINUE, DONE, BLOCKED, STOPPED, or ABSENT.")

    prove = add("prove", cmd_prove, "Record tool proof for a criterion that has no check command.")
    prove.add_argument("number", type=int)
    prove.add_argument("proof", help="what the tool showed, quoted or summarized")

    check = add("check", cmd_check, "Run check commands (all, or the numbers given) and record the results.")
    check.add_argument("numbers", type=int, nargs="*")
    check.add_argument("--timeout", type=int, default=CHECK_TIMEOUT)

    close = add("close", cmd_close, "Re-run every check. Mark the goal done if nothing is open.")
    close.add_argument("--timeout", type=int, default=CHECK_TIMEOUT)

    criterion = add("criterion", cmd_criterion, "Add a done-when criterion.")
    criterion.add_argument("text")
    criterion.add_argument("--check", metavar="COMMAND", help="command that proves it by exiting 0")

    drop = add("drop", cmd_drop, "Remove a criterion. The reason goes in the final report.")
    drop.add_argument("number", type=int)
    drop.add_argument("why")

    todo = add("todo", cmd_todo, "Add a planned step.")
    todo.add_argument("text")
    todo.add_argument("--first", action="store_true", help="put it at the front")

    did = add("did", cmd_did, "Finish a step (default the first) and log what happened.")
    did.add_argument("text", nargs="?", help="what happened, briefly")
    did.add_argument("--step", type=int, default=1)

    note = add("note", cmd_note, "Log progress, a failed attempt, or where you left off.")
    note.add_argument("text")

    decide = add("decide", cmd_decide, "Record a choice you made without asking. The user sees it at the end.")
    decide.add_argument("text")

    block = add("block", cmd_block, "Stop for a real block: needs-user:, destructive:, safety:, or oversight:.")
    block.add_argument("reason")

    unblock = add("unblock", cmd_unblock, "Clear a block (and optionally grant more automatic resumes).")
    unblock.add_argument("--resumes", type=int, help="allow this many more automatic resumes")

    stop = add("stop", cmd_stop, "The user ended the goal. The hook stops resuming it.")
    stop.add_argument("reason", nargs="?")

    add("show", cmd_show, "Print the whole goal file.")

    listing = add("list", cmd_list, "List goals.", needs_id=False)
    listing.add_argument("--all", action="store_true", help="include done and stopped goals")
    return top


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = parser().parse_args(argv)
    try:
        output = args.handler(args)
    except LookupError:
        output = "ABSENT\nNo live goal. Start one with `goal.py start`."
    except UsageError as exc:
        sys.stdout.write(f"error: {exc}\n")
        return 2
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.stdout.write(f"error: {exc}\n")
        return 2
    sys.stdout.write(output.rstrip() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
