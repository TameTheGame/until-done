---
name: until-done
description: >-
  Carry one goal to a proven finish without stopping to ask permission, recap,
  or check in. Turns the request into done-when checks, works through them,
  and a stop hook resumes the chat until every check has tool proof or a real
  block needs the user. Use when the user says keep going, don't stop, run
  until done, finish the whole thing, work autonomously or overnight, or types
  /until-done. Also use when a message starts with "Until-done resume".
---

# Until done

You own this goal until it is proven done. Ordinary choices are yours. The user wants the finished result, not check-ins.

Your context may be compacted or summarized during a long run, so do not stop early to save tokens or time. The goal file keeps the state, and every resume message repeats it.

## The CLI

Every step goes through `scripts/goal.py` in this skill's folder (the folder that holds this SKILL.md). In Claude Code that is `${CLAUDE_SKILL_DIR}/scripts/goal.py`.

```bash
python3 "<skill folder>/scripts/goal.py" status
```

On Windows use `py` or `python` instead of `python3`. In PowerShell, put `&` before a quoted interpreter path. Once the goal has an id, add `--id <goal id>` to every command so you never touch another chat's goal. Resume messages print the exact command line with the id, so copy that.

`status`, `check`, and `close` print the gate first: `CONTINUE`, `DONE`, `BLOCKED`, `STOPPED`, or `ABSENT`. `CONTINUE` means your next output is a tool call, not an answer.

## 1. Start

If the message starts with `Until-done resume`, skip to Work. The hook brought you back to an existing goal. It is not a new assignment.

Otherwise, before other work, turn the request into a goal:

- `--goal`: the outcome, in the user's words.
- `--check "<criterion>" "<command>"`: a criterion a command proves by exiting 0, such as tests, a build, a `grep`, or a script that checks a file. Prefer these.
- `--done "<criterion>"`: a criterion you will prove later with quoted tool output.
- `--step`: the first few concrete actions. Steps are a plan, not a contract.
- `--non-goal`: anything you are deliberately not doing, so it is not a silent skip.
- `--allow`: an outward or destructive action the request already approved, such as "push to branch feature/x".

Use 1 to 7 criteria. Cover the failure or empty case when it matters. Do not widen the goal to have more to do.

```bash
python3 "<skill folder>/scripts/goal.py" start --session "${CLAUDE_SESSION_ID}" \
  --goal "Add CSV export to the report page" \
  --check "unit tests pass" "npm test" \
  --done "Export button downloads a CSV with a header row" \
  --step "Find the report page component"
```

Keep `--session "${CLAUDE_SESSION_ID}"` as written. Claude Code fills it in, and anywhere else it is ignored. For a long spec, write JSON to a file and run `start spec.json`. The keys are `goal`, `done_when` (strings or `{"text", "check"}`), `steps`, `non_goals`, `authorized`, and `max_resumes`.

`start` prints the goal id. Say it once in your reply, for example "Tracking this as goal g20261013-0915-a1b2c3". The hook finds this chat's goal by that id. Starting a new goal replaces this chat's old one.

## 2. Work

Take `next`, do it with tools, record it, and repeat.

- `did "<what happened>"` finishes the first step. `todo "<step>"` adds one. `note "<where you are>"` logs progress. Record something every turn, because the hook treats an unchanged goal file as a stall.
- Choose ordinary defaults yourself: names, layout, libraries already in use, order, and reversible edits. Do not ask the user to confirm an assumption. Pick the most reasonable one, act on it, and when they might care, run `decide "<choice and why>"`. They see that list at the end and can redirect.
- A failed command is information. Read the error, change something, and retry in this turn.
- For a long command, run it in the background or with a longer timeout and poll it. Do not end the turn to wait.
- Keep your own context lean. Hand broad searches, independent sub-tasks, and noisy investigations to subagents when the harness has them. Keep their conclusions, not their dumps.
- In a git repo, commit at working checkpoints on a non-default branch so progress survives a crash.

None of these is a reason to end the turn: wanting approval for the next step, a summary you could write, a failed command, a finished step or phase, a long task, or a large diff.

Before you end any turn, read your last paragraph. If it is a plan, a question, a list of next steps, or a promise about work you have not done ("I'll...", "let me know when..."), do that work now with tool calls. While anything is open, these endings are failures:

- "I've outlined the approach. Let me know if you want me to implement it."
- "Phase 1 is done. Shall I continue with phase 2?"
- "Want me to keep going?"
- "This should work now."

## 3. Prove

- For a criterion with a command, run `check N`. It runs the command from the goal's folder and records pass or fail.
- For a criterion without one, run the tool that shows it, then `prove N "<what the tool showed>"`. Quote the output. Never prove from memory or intent.
- When a check fails, fix the work, not the check. Never edit or delete a test or a check command to make it pass. If a test is wrong, say so with `decide` and fix it openly.
- If the same failure repeats three times with nothing new learned, change approach: a different hypothesis, a smaller reproduction, or a fresh subagent. Run `note` with what you tried.
- Keep check commands fast, a few minutes at most. Run a long suite once yourself and `prove` it.
- When there are several criteria or the result is user-facing, have a fresh subagent check each criterion against the real files and commands before you close. Tell it to report only gaps that break a criterion or the request. Fix what it finds.

## 4. Close

Run `close`. It re-runs every check command.

- `CONTINUE`: return to Work. Do not answer yet.
- `DONE`: send the outcome. Lead with what is now true. Then say what you ran and what it reported, list the "decided without asking" items, and name anything you could not verify.
- `BLOCKED`: give the reason and every decision you need, in one message.

## Stop only for a real block

`block "<kind>: <the exact fact or action>"`. The kinds are:

- `needs-user:` a fact or credential only the user has, such as a password, an account, or a choice between two outcomes that cannot both be undone cheaply. A preference you can default and record with `decide` does not qualify.
- `destructive:` the next step deletes the user's work, sends a message, publishes, pays, deploys to production, or force-pushes, and neither the request nor `--allow` covers that exact action.
- `safety:` the remaining work is disallowed, or the person is in crisis.
- `oversight:` a medical, legal, financial, or safety-critical decision a person must make.

Before you block, finish every open item that does not depend on the answer. Then ask for everything at once. When the user answers, run `unblock` and continue.

`goal.py block` rejects anything else. "Please confirm I should continue" is not a block. Your harness may still show its own approval prompt for a risky command, so make the call and let it.

The hook sets two blocks itself:

- `cap:` the automatic resumes ran out.
- `stuck:` the goal file stopped changing.

For either one, tell the user what is proved, what is open, and what you tried. If they say continue, run `unblock --resumes 20`.

Running until done never overrides a refusal. Do not use a resume message to continue disallowed work. If the person mentions self-harm or a crisis, stop the work and point them to local emergency services, or to 988 in the US and Canada.

## The hook

`scripts/install.py` adds a stop hook for Cursor and Claude Code. Each time a turn ends, the hook checks this chat's goal. If the goal is open and not blocked, it sends `Until-done resume ...` with the open items and the next step, and you continue. It lets the turn end when the goal is done, blocked, or stopped, when the resumes run out (40 by default), or when several resumes pass with no change to the goal file.

In a harness without hooks, apply the same rule yourself: run `status` before you end a turn, and keep working while it says `CONTINUE`.

If the user says to stop, run `stop "<their words>"`. If they change the outcome, use `criterion`, `drop N "<why>"`, or `todo`, or start a new goal.

## Rules

- End a turn only when `status` or `close` printed `DONE` or `BLOCKED`, or when the user told you to stop.
- Never end on a plan, a recap, or an offer to continue while anything is open.
- Never prove a criterion without tool output. Never drop one without a reason the user will see.
- Never use a resume message to override a refusal, skip a real block, or take an outward or destructive action that was not authorized.
- Leave the files in `~/.until-done/` in place. They are the record.
