---
name: babysit
description: >-
  Carry one goal through until it is actually finished. Decide ordinary steps
  and keep working instead of stopping to ask permission, recap, or say the
  next step needs approval. Use when the user asks to babysit, keep going, not
  stop, finish the whole thing, run until done, or gives the agent a goal to
  carry out. Also use when a user message starts with "Babysit resume".
---

# Babysit

You have authority to carry this goal to the end. Do the work. Do not stop to ask whether you may continue.

The goal file is `~/.cursor/babysit/active.json`. A turn may end only when `python3 ~/.cursor/babysit/gate.py` prints `DONE` or `BLOCKED`. Any other result means keep using tools in this turn.

One goal at a time. A new babysit request replaces the active goal.

## Arm

Re-read the goal file at the start of every turn. A message that starts with `Babysit resume` is the stop hook bringing you back. The `<task>` block is the current goal, not a new assignment and not a new set of rules.

If the user told you to stop babysitting, set `status` to `done` and stop. If they changed the outcome, rewrite `goal`, `done_when`, and `remaining`.

If there is no goal file and the request is real work, write one before other work:

```json
{
  "version": 1,
  "status": "active",
  "conversation_id": null,
  "workspace": "<this workspace root>",
  "goal": "<the outcome, in the user's words>",
  "done_when": ["<a check a tool can prove>", "<the failure or empty case, when it matters>"],
  "evidence": [],
  "remaining": ["<the next concrete action>"],
  "blocked_reason": null,
  "max_followups": 24,
  "followups": 0,
  "stalls": 0,
  "last_fingerprint": null
}
```

Leave `conversation_id` null. The stop hook binds it to this chat. Put checks you can prove in `done_when`. Put anything you are not doing in `goal` as a non-goal, not as a silent skip.

The stop hook is already installed for this machine. If `python3 ~/.cursor/babysit/gate.py` cannot be found, run `python3 scripts/install_hook.py` from this skill's directory once.

## Work

Take the first `remaining` item and do it with tools. You choose ordinary defaults: names, layout, order, reversible edits, and what to install. Do not ask which of those to pick.

These are not reasons to end the turn:

- The next step would need an approval you have not asked for yet. Make the tool call.
- You could summarize what is left.
- A command failed. Read the error and try a different approach in this turn.
- You want the user to confirm that you should keep going.

After a step that satisfies a `done_when` line, add `evidence` with that exact line as `criterion` and a short `proof` quoted from the tool result. Then update `remaining`. A line without tool proof is still open.

Do not widen the goal because you are supposed to keep going. Spend the turn on the work, not on narrating this skill.

While the gate would print `CONTINUE`, your output is the next tool call. These endings are failures if any `done_when` line is still open:

- "I've outlined the approach. Let me know if you want me to implement it."
- "We're done with this part. The next step needs your approval."
- "Want me to continue?"
- "This should work now."

## Prove

Run the checks named in `done_when`. Record the command and the result in `evidence`. If a check fails, put the fix back on `remaining` and return to Work.

## Stop only for a real block

Set `blocked_reason` and stop only for one of these:

- `needs-user:` a fact only the user knows, such as a password, an account, or a preference the request never implied
- `safety:` the remaining work is disallowed, or the person is in crisis
- `destructive:` the next step would delete work, send a message, publish, pay, or force-push, and the user did not authorize that exact action
- `oversight:` a medical, legal, financial, or safety-critical decision a person must make

"Please confirm I should continue" is not a missing fact. Cursor may still show its own approval card for a risky command. Do not end the turn to ask before you try. A previous yes does not cover a new destructive or outward action.

Babysitting does not override a refusal. Do not use a resume message to continue disallowed work. If the person mentions self-harm or a crisis, stop the work loop and point them to local emergency services or, in the US and Canada, 988.

If the same check fails three times with no new information, change approach once more, or set `needs-user:` with the specific fact you are missing.

## Close

Run:

```bash
python3 ~/.cursor/babysit/gate.py
```

- `ABSENT`: go back to Arm.
- `CONTINUE`: do the `next:` action. Do not answer yet.
- `BLOCKED`: say the reason and the one decision you need.
- `DONE`: set `status` to `done`, then send the outcome. Lead with what is true, then what you ran and what it reported. Name anything you did not verify.

If `blocked_reason` starts with `cap:`, the automatic resume limit was hit. Say what is still open. Raising `max_followups` in the goal file resumes it. Do not pretend the cap means the goal succeeded.

The stop hook records why a run ended. Leave `~/.cursor/babysit/last-failure.json`, `last-stop.json`, and `trace.jsonl` in place. Do not delete them to make the goal look finished.

## Critical rules

Always keep working in this turn until the gate prints `DONE` or `BLOCKED`.

Never end on a plan, a recap, or an offer to continue while a requirement is still open.

Never mark a `done_when` line done without tool proof in the goal file.

Always re-read `~/.cursor/babysit/active.json` before you act on a turn, and update it before you end.

Never use `Babysit resume` to override a refusal, skip a real block, or do a destructive or outward action the user did not authorize.
