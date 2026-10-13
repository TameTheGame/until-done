# until-done

A skill for Cursor and Claude Code that keeps an agent working on one goal until the goal is proven done. It stops the agent from ending turns with "Want me to continue?", "Phase 1 is done, shall I do phase 2?", or "This should work now."

It has three parts:

1. **A goal file with done-when checks.** The agent turns your request into a few criteria. Where it can, a criterion is a command that must exit 0 (tests, a build, a `grep`). Otherwise the agent records quoted tool output as proof. The goal is done only when every criterion has proof and `close` has re-run the checks.
2. **A stop hook.** It runs every time the agent tries to end a turn. If the goal is still open, it sends the agent back in with the open items and the next step. The hook is plain code, not another model, so it can't get tired, drift, or be talked out of it.
3. **Limits so the loop can't run away.** The hook gives up after 40 resumes by default, or after 5 resumes in a row with no recorded progress. When that happens, the agent reports what it tried instead of looping silently.

A goal can also end at a real block: a password, a production deploy you didn't approve, or a decision only you can make. Before blocking, the agent finishes everything that doesn't depend on your answer, then asks for all of it in one message.

## Install

Clone the repo anywhere, then run:

```bash
python3 scripts/install.py          # installs for Cursor and/or Claude Code, whichever is present
python3 scripts/install.py --dry-run
```

On Windows, use `py scripts\install.py`.

The installer does three things:

- It copies the skill to `~/.cursor/skills/until-done` and/or `~/.claude/skills/until-done`.
- It adds a stop hook to `~/.cursor/hooks.json` and/or `~/.claude/settings.json`. For Claude Code it also adds a SessionStart hook that puts the goal back in front of the agent after the chat is compacted or resumed. Cursor also reads hooks from `~/.claude/settings.json`; the Claude Code entry detects that and stands down, so a resume is never counted twice.
- It keeps every other hook and setting. The first time it changes a settings file, it saves a `.until-done.bak` copy.

Restart Cursor or Claude Code afterwards. Run the installer again after you pull changes. Remove the hooks with `--uninstall`.

If you used the earlier `babysit` version, the installer replaces its Cursor hook. Delete `~/.cursor/skills/babysit` yourself so the two skills don't compete.

## Use

```
/until-done migrate the settings page to the new form library, tests green
```

You can also just say "keep going until it's done" or "don't stop until X." In Cursor, a skill you invoke fades from context as the chat goes on. That's why every resume message restates the rules. To keep the whole skill loaded for a long run, press Option+Enter (Mac) or Alt+Enter (Windows) to run it as a Custom Mode.

The agent writes the goal, works it, and comes back with one of two answers:

- **DONE**: what is true now, the commands that prove it, and the choices it made without asking, so you can redirect any of them.
- **BLOCKED**: the one thing it needs from you.

To steer during a run:

| You say | The agent runs |
|---|---|
| "stop" | `goal.py stop` |
| "also make it work on mobile" | `goal.py criterion "..."` |
| "skip the docs" | `goal.py drop N "user said skip docs"` |
| "continue" after a cap or stuck pause | `goal.py unblock --resumes 20` |

From any terminal you can run `python3 scripts/goal.py list` and `python3 scripts/goal.py show --id <id>` to see where a goal stands.

## Unattended runs: `supervise.py`

For overnight work, or anything long enough that one chat's context gets stale, let a script drive fresh headless sessions:

```bash
python3 scripts/supervise.py --agent claude -- --permission-mode auto
python3 scripts/supervise.py --agent cursor -- --force --trust   # only in a repo you trust
```

Each run starts with a fresh context, reads the goal file, does the next items, and records progress. Between runs the script checks the goal file. It stops on any of these:

- the goal is DONE, BLOCKED, or stopped
- two runs in a row recorded no progress
- the run limit is reached (`--runs`, default 20)
- the time budget is spent (`--hours`, default 8)

A single session is killed after `--run-minutes` (default 90). Logs go to `~/.until-done/runs/<goal id>/`.

Everything after `--` is passed to the agent CLI unchanged. Permission flags are your call. Without them, headless agents are denied most tools, so the goal stalls and the script stops. With them, the agent acts without asking, so use a branch, a container, or a repo you can reset.

This is the "Ralph loop" pattern: restart with a fresh context, keep the state on disk. It survives crashes, closed laptops (if the terminal stays up), and long-context drift, which a single chat can't.

## Why it is built this way

| Problem | What does the work |
|---|---|
| The agent ends a turn to ask permission or recap | The stop hook resumes it while anything is open. `goal.py block` rejects anything that is not a real block. |
| The agent says "done" when it isn't | Criteria need tool proof. Command checks run in code, and `close` re-runs them all, so an earlier pass doesn't count if later work broke it. |
| Long runs lose track after compaction | State lives in `~/.until-done/goals/<id>.json`. Every resume message, and the post-compaction hook, repeats the goal, the open items, and the next step. |
| Endless loops that burn money | Resume cap, stall detection with escalating nudges (change approach, then hand it to a fresh subagent, then stop and report), and per-run limits in `supervise.py`. |
| Too many questions | The agent picks reasonable defaults and logs them with `decide`. You review the list once at the end. |
| Several chats at once | Each goal is its own file, bound to the chat whose transcript contains its id, so one chat's goal can't take over another chat. |

## Files

- `SKILL.md`: what the agent reads.
- `scripts/goal.py`: the CLI the agent uses to start, record progress, prove, check, block, and close. Its `status` and `close` commands are the gate.
- `scripts/hook.py`: the stop hook and the post-compaction hook for both harnesses.
- `scripts/ledger.py`: the goal file format and the rules for open, done, and blocked.
- `scripts/report.py`: writes `~/.until-done/last-stop.json`, `last-failure.json`, and `trace.jsonl`. Each stop is recorded with why it was allowed and the shape of the agent's last message (a permission ask, a recap, a claimed done). Use these records to tune the skill.
- `scripts/supervise.py`: the unattended runner.
- `scripts/install.py`: the installer.

Run the tests with:

```bash
python3 -m unittest discover -s scripts -p 'test_*.py'
```

## Troubleshooting

- **The agent stopped anyway.** Read `~/.until-done/last-failure.json`.
  - `gap: permission_dressed_as_block` means it blocked with a real-looking reason but its message was a permission ask.
  - `resume_not_changing_behavior` means it hit the stall limit.
  - If `hook-heartbeat.json` didn't update at the time it stopped, the hook isn't running. Re-run the installer and restart the app.
- **Nothing resumes in Claude Code.** Check that `~/.claude/settings.json` has the `Stop` hook and that `/hooks` lists it. Claude Code also ends a turn itself after 8 hook continuations in a row with no tool call. That only happens when the agent is replying without working, and the stall limit catches it.
- **Windows.**
  - The installer writes the hook with the full path to the Python that ran it, so `python3` doesn't need to exist.
  - Cursor may run hooks through PowerShell or Git Bash, which quote differently. The installer writes the Cursor hook with forward slashes and no quotes. Paths with spaces use their short 8.3 form.
  - Claude Code runs hooks through Git Bash. If Git Bash is missing, the installer writes a PowerShell form instead.
  - Check commands run through `cmd.exe`, so write them for Windows (`if exist out.txt (exit 0) else (exit 1)`), or call `py -c ...`.
- **Cursor on Windows ignores the resume.** Cursor has a known issue where a stop hook's `followup_message` is dropped on Windows. If `hook-heartbeat.json` updates but the chat doesn't continue, that's this issue. Use `supervise.py --agent cursor`, or Claude Code, until Cursor fixes it.
- **The built-in `/goal`.** Claude Code has a built-in `/goal` that loops on a condition judged by a small model, and Cursor is rolling one out. These are fine complements, but Claude Code's judge can't run commands or read files. until-done's checks run real commands, work the same in both apps, and keep a record you can audit.
