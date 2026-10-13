#!/usr/bin/env python3
"""Checks for the until-done stop hook, its stop records, and the installer."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import goal  # noqa: E402
import hook  # noqa: E402
import install  # noqa: E402
import ledger as L  # noqa: E402
import report  # noqa: E402


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.workspace = self.root / "work"
        self.workspace.mkdir()
        os.environ["UNTIL_DONE_HOME"] = str(self.root / "state")
        os.environ.pop("UNTIL_DONE_GOAL", None)
        self.previous = Path.cwd()
        os.chdir(self.workspace)

    def tearDown(self) -> None:
        os.chdir(self.previous)
        os.environ.pop("UNTIL_DONE_HOME", None)
        os.environ.pop("UNTIL_DONE_GOAL", None)
        self.directory.cleanup()

    def cli(self, *argv: str) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            goal.main(list(argv))
        return buffer.getvalue()

    def start(self, *extra: str) -> str:
        out = self.cli("start", "--goal", "Write the cheat sheet", "--done", "cheat-sheet.txt exists", *extra)
        return out.split()[1]

    def transcript(self, *texts: str, name: str = "chat.jsonl") -> str:
        path = self.root / name
        rows = [{"role": "assistant", "message": {"content": [{"type": "text", "text": text}]}} for text in texts]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return str(path)

    def cursor(self, chat: str, transcript: str | None = None, **extra) -> dict:
        payload = {"status": "completed", "conversation_id": chat, "loop_count": 0, "workspace_roots": [str(self.workspace)]}
        if transcript:
            payload["transcript_path"] = transcript
        payload.update(extra)
        return hook.handle(payload, "cursor")

    def stored(self, goal_id: str) -> dict:
        return json.loads(L.goal_path(goal_id).read_text(encoding="utf-8"))

    def test_idle_without_goal(self) -> None:
        self.assertEqual(self.cursor("chat-a"), {})
        heartbeat = json.loads((L.home() / "hook-heartbeat.json").read_text(encoding="utf-8"))
        self.assertFalse(heartbeat["had_goal"])

    def test_resumes_the_chat_that_started_the_goal(self) -> None:
        goal_id = self.start()
        result = self.cursor("chat-a", self.transcript(f"Started {goal_id} in ..."))
        self.assertIn("Until-done resume", result["followup_message"])
        self.assertIn(goal_id, result["followup_message"])
        saved = self.stored(goal_id)
        self.assertEqual(saved["session_id"], "chat-a")
        self.assertEqual(saved["harness"], "cursor")
        self.assertEqual(saved["resumes"], 1)

    def test_other_chat_is_not_hijacked(self) -> None:
        goal_id = self.start()
        other = self.cursor("chat-b", self.transcript("unrelated work", name="b.jsonl"))
        self.assertEqual(other, {})
        self.assertIsNone(self.stored(goal_id)["session_id"])
        self.cursor("chat-a", self.transcript(goal_id))
        self.assertEqual(self.cursor("chat-b", self.transcript("still unrelated", name="b.jsonl")), {})

    def test_binds_without_transcript_only_when_unambiguous(self) -> None:
        goal_id = self.start()
        self.assertIn("followup_message", self.cursor("chat-a"))
        self.assertEqual(self.stored(goal_id)["session_id"], "chat-a")
        self.start()
        self.start()
        self.assertEqual(self.cursor("chat-z"), {})

    def test_session_from_start_binds_immediately(self) -> None:
        goal_id = self.start("--session", "claude-1")
        result = hook.handle({"session_id": "claude-1", "cwd": str(self.workspace), "stop_hook_active": False}, "claude")
        self.assertEqual(result["decision"], "block")
        self.assertIn(goal_id, result["reason"])
        self.assertEqual(hook.handle({"session_id": "claude-2", "cwd": str(self.workspace)}, "claude"), {})

    def test_claude_shape_is_detected(self) -> None:
        self.start("--session", "claude-1")
        result = hook.handle({"hook_event_name": "Stop", "session_id": "claude-1", "stop_hook_active": True})
        self.assertEqual(result["decision"], "block")

    def test_other_workspace_is_ignored(self) -> None:
        goal_id = self.start()
        result = self.cursor("chat-a", self.transcript(goal_id), workspace_roots=[str(self.root / "elsewhere")])
        self.assertEqual(result, {})

    def test_real_block_lets_the_turn_end(self) -> None:
        goal_id = self.start("--session", "chat-a")
        self.cli("block", "needs-user: which Google account to use")
        self.assertEqual(self.cursor("chat-a"), {})
        self.assertEqual(self.stored(goal_id)["status"], "blocked")

    def test_hand_written_permission_block_is_cleared(self) -> None:
        goal_id = self.start("--session", "chat-a")
        data = self.stored(goal_id)
        data["blocked_reason"] = "Please confirm I should continue"
        data["status"] = "blocked"
        L.goal_path(goal_id).write_text(json.dumps(data), encoding="utf-8")
        self.assertIn("followup_message", self.cursor("chat-a"))
        self.assertIsNone(self.stored(goal_id)["blocked_reason"])

    def test_done_goal_does_not_loop(self) -> None:
        goal_id = self.start("--session", "chat-a")
        self.cli("prove", "1", "ls shows cheat-sheet.txt")
        self.assertIn("goal.py close", self.cursor("chat-a")["followup_message"])
        self.cli("close")
        self.assertEqual(self.cursor("chat-a"), {})
        self.assertEqual(self.stored(goal_id)["status"], "done")

    def test_stopped_goal_does_not_resume(self) -> None:
        self.start("--session", "chat-a")
        self.cli("stop", "user said stop")
        self.assertEqual(self.cursor("chat-a"), {})

    def test_aborted_turn_does_not_resume(self) -> None:
        self.start("--session", "chat-a")
        self.assertEqual(self.cursor("chat-a", status="aborted"), {})
        failure = json.loads((L.home() / "last-failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["gap"], "user_or_product_abort")

    def test_error_turn_resumes_with_a_note(self) -> None:
        self.start("--session", "chat-a")
        result = self.cursor("chat-a", status="error")
        self.assertIn("ended with an error", result["followup_message"])

    def test_cap_explains_once_then_stops(self) -> None:
        goal_id = self.start("--session", "chat-a", "--max-resumes", "2")
        self.cursor("chat-a")
        self.cli("note", "progress one")
        self.cursor("chat-a")
        self.cli("note", "progress two")
        final = self.cursor("chat-a")
        self.assertIn("Until-done pause", final["followup_message"])
        self.assertIn("Do not keep working", final["followup_message"])
        self.assertTrue(self.stored(goal_id)["blocked_reason"].startswith("cap:"))
        self.assertEqual(self.cursor("chat-a"), {})
        self.cli("unblock", "--resumes", "3")
        self.assertIn("Until-done resume", self.cursor("chat-a")["followup_message"])

    def test_stalls_escalate_then_stop_as_stuck(self) -> None:
        goal_id = self.start("--session", "chat-a")
        messages = [self.cursor("chat-a").get("followup_message", "") for _ in range(L.DEFAULT_MAX_STALLS + 1)]
        self.assertNotIn("has not changed", messages[1])
        self.assertIn("has not changed for 2 resumes", messages[2])
        self.assertIn("fresh subagent", messages[4])
        self.assertIn("Until-done pause", messages[-1])
        self.assertTrue(self.stored(goal_id)["blocked_reason"].startswith("stuck:"))

    def test_progress_resets_stalls(self) -> None:
        goal_id = self.start("--session", "chat-a")
        for _ in range(3):
            self.cursor("chat-a")
        self.cli("note", "found the config loader")
        self.cursor("chat-a")
        self.assertEqual(self.stored(goal_id)["stalls"], 0)

    def test_forced_goal_for_headless_runs(self) -> None:
        goal_id = self.start("--session", "interactive-chat")
        os.environ["UNTIL_DONE_GOAL"] = goal_id
        result = hook.handle({"session_id": "headless-run", "cwd": str(self.workspace)}, "claude")
        self.assertIn(goal_id, result["reason"])

    def test_invalid_goal_file_gets_one_repair_prompt(self) -> None:
        goal_id = self.start("--session", "chat-a")
        L.goal_path(goal_id).write_text('{"session_id": "chat-a", broken', encoding="utf-8")
        result = self.cursor("chat-a")
        self.assertIn("is invalid", result["followup_message"])
        self.assertEqual(self.cursor("chat-a", loop_count=3), {})

    def test_context_reinjects_goal(self) -> None:
        goal_id = self.start("--session", "claude-1")
        result = hook.context({"session_id": "claude-1", "source": "compact"}, "claude")
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn(goal_id, text)
        self.assertIn("next:", text)
        self.assertEqual(hook.context({"session_id": "claude-9"}, "claude"), {})

    def test_failure_record_explains_why_it_stopped(self) -> None:
        self.start("--session", "chat-a")
        self.cli("block", "needs-user: please confirm I should continue")
        transcript = self.transcript("We're done with this part. The next step needs your approval.")
        self.cursor("chat-a", transcript, loop_count=2, model="grok-4.7")
        failure = json.loads((L.home() / "last-failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["gap"], "permission_dressed_as_block")
        self.assertEqual(failure["quit_shape"], "permission_ask")
        self.assertEqual(failure["loop_count"], 2)
        self.assertEqual(failure["model"], "grok-4.7")
        self.assertIn("needs your approval", failure["last_assistant"])

    def test_claude_last_message_is_used(self) -> None:
        self.start("--session", "claude-1")
        self.cli("block", "needs-user: want me to continue?")
        hook.handle({"session_id": "claude-1", "last_assistant_message": "Want me to continue?"}, "claude")
        failure = json.loads((L.home() / "last-failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["quit_shape"], "permission_ask")

    def test_resume_is_not_a_failure(self) -> None:
        self.start("--session", "chat-a")
        self.cursor("chat-a")
        last = json.loads((L.home() / "last-stop.json").read_text(encoding="utf-8"))
        self.assertEqual(last["decision"], "resumed")
        self.assertFalse((L.home() / "last-failure.json").exists())

    def test_claude_transcript_rows_are_read(self) -> None:
        path = self.root / "claude.jsonl"
        rows = [
            {"type": "user", "message": {"role": "user", "content": "go"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Shall I proceed?"}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash"}]}},
        ]
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        self.assertEqual(report.last_assistant_text(str(path)), "Shall I proceed?")

    def test_classify_quit_shapes(self) -> None:
        self.assertEqual(report.classify_quit("Want me to continue?"), "permission_ask")
        self.assertEqual(report.classify_quit("I've outlined the approach. Next steps are below."), "recap")
        self.assertEqual(report.classify_quit(""), "unknown")

    def test_claude_entry_stands_down_inside_cursor(self) -> None:
        self.start("--session", "chat-a")
        payload = {"status": "completed", "conversation_id": "chat-a", "cursor_version": "3.24.1", "loop_count": 0}
        self.assertEqual(hook.handle(payload, "claude"), {})
        self.assertEqual(self.stored_by_session("chat-a")["resumes"], 0)
        self.assertIn("followup_message", hook.handle(payload, "cursor"))
        self.assertEqual(self.stored_by_session("chat-a")["resumes"], 1)

    def test_payload_with_cursor_version_is_cursor(self) -> None:
        self.start("--session", "chat-a")
        result = hook.handle({"status": "completed", "conversation_id": "chat-a", "cursor_version": "3.24.1"})
        self.assertIn("followup_message", result)

    def test_resume_restates_the_rules(self) -> None:
        self.start("--session", "chat-a")
        self.assertIn("Rules:", self.cursor("chat-a")["followup_message"])

    def test_main_reads_stdin_with_byte_order_mark(self) -> None:
        self.start("--session", "chat-a")
        raw = b"\xef\xbb\xbf" + json.dumps({"status": "completed", "conversation_id": "chat-a"}).encode("utf-8")
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw))), redirect_stdout(buffer):
            hook.main(["stop", "--harness", "cursor"])
        self.assertIn("followup_message", json.loads(buffer.getvalue()))

    def stored_by_session(self, session: str) -> dict:
        return next(ledger for _, ledger, _ in L.all_goals() if ledger and ledger["session_id"] == session)

    def test_main_prints_json_only(self) -> None:
        buffer = io.StringIO()
        with mock.patch.object(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json"))), redirect_stdout(buffer):
            hook.main(["stop", "--harness", "cursor"])
        self.assertEqual(json.loads(buffer.getvalue()), {})


STOP = '"/usr/bin/python3" "/Users/me/.cursor/skills/until-done/scripts/hook.py" stop --harness cursor'
STOP2 = '"/opt/py/bin/python3" "/Users/me/.cursor/skills/until-done/scripts/hook.py" stop --harness cursor'
CONTEXT = '"/usr/bin/python3" "/Users/me/.cursor/skills/until-done/scripts/hook.py" context --harness cursor'


class InstallTests(unittest.TestCase):
    def test_cursor_merge_keeps_other_hooks_and_replaces_babysit(self) -> None:
        merged = install.merge_cursor(
            {
                "version": 1,
                "hooks": {
                    "sessionStart": [{"command": "echo hi"}],
                    "stop": [{"command": "/usr/bin/python3 /Users/me/.cursor/babysit/stop_hook.py"}, {"command": "other"}],
                },
            },
            STOP,
        )
        self.assertEqual(merged["hooks"]["sessionStart"][0]["command"], "echo hi")
        stops = [entry["command"] for entry in merged["hooks"]["stop"]]
        self.assertEqual(stops, ["other", STOP])
        self.assertEqual(merged["hooks"]["stop"][-1]["loop_limit"], install.CURSOR_LOOP_LIMIT)
        again = install.merge_cursor(merged, STOP2)
        self.assertEqual([entry["command"] for entry in again["hooks"]["stop"]], ["other", STOP2])
        self.assertEqual([entry["command"] for entry in again["hooks"]["sessionStart"]], ["echo hi"])
        removed = install.merge_cursor(again, "")
        self.assertEqual(removed["hooks"], {"sessionStart": [{"command": "echo hi"}], "stop": [{"command": "other"}]})

    def test_claude_merge_keeps_other_settings(self) -> None:
        existing = {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "notify.sh"}]}]},
        }
        merged = install.merge_claude(existing, STOP, CONTEXT)
        self.assertEqual(merged["permissions"], existing["permissions"])
        commands = [h["command"] for group in merged["hooks"]["Stop"] for h in group["hooks"]]
        self.assertEqual(commands, ["notify.sh", STOP])
        self.assertEqual(merged["hooks"]["SessionStart"][0]["matcher"], "compact|resume")
        again = install.merge_claude(merged, STOP2, CONTEXT)
        commands = [h["command"] for group in again["hooks"]["Stop"] for h in group["hooks"]]
        self.assertEqual(commands, ["notify.sh", STOP2])
        self.assertEqual(len(again["hooks"]["SessionStart"]), 1)
        removed = install.merge_claude(again, "", "")
        self.assertEqual(removed["hooks"], existing["hooks"])

    def test_cursor_merge_removes_an_old_context_hook(self) -> None:
        merged = install.merge_cursor({"version": 1, "hooks": {"sessionStart": [{"command": CONTEXT}]}}, STOP)
        self.assertNotIn("sessionStart", merged["hooks"])

    def test_neutral_command_has_no_quotes_without_spaces(self) -> None:
        command = install.neutral_command("/usr/bin/python3", Path("/home/me/.cursor/skills/until-done/scripts/hook.py"), "stop", "cursor")
        self.assertEqual(command, "/usr/bin/python3 /home/me/.cursor/skills/until-done/scripts/hook.py stop --harness cursor")

    def test_powershell_fallback_uses_call_operator(self) -> None:
        command = install.command_line("C:/Python313/python.exe", Path("C:/x/hook.py"), "stop", "claude", powershell=True)
        self.assertTrue(command.startswith('& "C:/Python313/python.exe"'))
        merged = install.merge_claude({}, command, command, shell="powershell")
        self.assertEqual(merged["hooks"]["Stop"][0]["hooks"][0]["shell"], "powershell")

    def test_command_quotes_paths_with_spaces(self) -> None:
        command = install.command_line("/opt/My Python/bin/python3", Path("/Users/Jo Smith/skills/hook.py"), "stop", "claude")
        self.assertIn('"/Users/Jo Smith/skills/hook.py"', command)
        self.assertIn('"/opt/My Python/bin/python3"', command)


if __name__ == "__main__":
    unittest.main()
