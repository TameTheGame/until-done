#!/usr/bin/env python3
"""Checks for the babysit goal gate and stop hook."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import gate  # noqa: E402
import install_hook  # noqa: E402
import report  # noqa: E402
import stop_hook  # noqa: E402


def sample(**overrides) -> dict:
    data = {
        "version": 1,
        "status": "active",
        "conversation_id": None,
        "workspace": None,
        "goal": "Write the cheat sheet and save it on the Desktop.",
        "done_when": ["Desktop has cheat-sheet.txt"],
        "evidence": [],
        "remaining": ["Draft the cheat sheet"],
        "blocked_reason": None,
        "max_followups": 24,
        "followups": 0,
        "stalls": 0,
        "last_fingerprint": None,
    }
    data.update(overrides)
    return data


class BabysitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "active.json"
        os.environ["BABYSIT_LEDGER"] = str(self.path)

    def tearDown(self) -> None:
        os.environ.pop("BABYSIT_LEDGER", None)
        self.directory.cleanup()

    def write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_absent_when_no_file(self) -> None:
        self.assertEqual(gate.render(), "ABSENT")
        self.assertEqual(stop_hook.handle({"status": "completed", "conversation_id": "c1"}), {})

    def test_continue_while_open(self) -> None:
        self.write(sample())
        text = gate.render()
        self.assertTrue(text.startswith("CONTINUE"))
        self.assertIn("next: Draft the cheat sheet", text)

    def test_done_requires_matching_proof(self) -> None:
        self.write(
            sample(
                evidence=[{"criterion": "Desktop has cheat-sheet.txt", "proof": "wrote 20 lines"}],
                remaining=[],
            )
        )
        self.assertEqual(gate.render(), "DONE")

    def test_empty_proof_and_wrong_criterion_stay_open(self) -> None:
        self.write(
            sample(
                evidence=[{"criterion": "something else", "proof": "ok"}, {"criterion": "Desktop has cheat-sheet.txt", "proof": "  "}],
                remaining=[],
            )
        )
        self.assertTrue(gate.render().startswith("CONTINUE"))

    def test_permission_ask_is_not_a_block(self) -> None:
        self.write(sample(blocked_reason="Please confirm I should continue"))
        self.assertTrue(gate.render().startswith("CONTINUE"))

    def test_real_block_stops(self) -> None:
        self.write(sample(status="blocked", blocked_reason="needs-user: which desktop, iCloud or local?"))
        self.assertTrue(gate.render().startswith("BLOCKED"))
        result = stop_hook.handle({"status": "completed", "conversation_id": "c1"})
        self.assertEqual(result, {})

    def test_stop_hook_resumes_open_goal_and_binds_chat(self) -> None:
        self.write(sample())
        result = stop_hook.handle({"status": "completed", "conversation_id": "chat-a", "loop_count": 0})
        self.assertIn("Babysit resume", result["followup_message"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["conversation_id"], "chat-a")
        self.assertEqual(saved["followups"], 1)
        other = stop_hook.handle({"status": "completed", "conversation_id": "chat-b"})
        self.assertEqual(other, {})

    def test_aborted_turn_does_not_resume(self) -> None:
        self.write(sample(conversation_id="chat-a"))
        self.assertEqual(stop_hook.handle({"status": "aborted", "conversation_id": "chat-a"}), {})

    def test_cap_explains_once_then_stops(self) -> None:
        self.write(sample(conversation_id="chat-a", followups=24, max_followups=24))
        result = stop_hook.handle({"status": "completed", "conversation_id": "chat-a"})
        self.assertIn("cap was hit", result["followup_message"])
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(saved["blocked_reason"].startswith("cap:"))
        self.assertEqual(stop_hook.handle({"status": "completed", "conversation_id": "chat-a"}), {})

    def test_finished_goal_does_not_loop(self) -> None:
        self.write(
            sample(
                status="done",
                conversation_id="chat-a",
                evidence=[{"criterion": "Desktop has cheat-sheet.txt", "proof": "file exists"}],
                remaining=[],
            )
        )
        self.assertEqual(stop_hook.handle({"status": "completed", "conversation_id": "chat-a"}), {})

    def test_stall_message_after_no_progress(self) -> None:
        first = sample(conversation_id="chat-a")
        self.write(first)
        stop_hook.handle({"status": "completed", "conversation_id": "chat-a"})
        stop_hook.handle({"status": "completed", "conversation_id": "chat-a"})
        stop_hook.handle({"status": "completed", "conversation_id": "chat-a"})
        warned = stop_hook.handle({"status": "completed", "conversation_id": "chat-a"})
        self.assertIn("Change approach", warned["followup_message"])

    def test_failure_record_explains_why_it_stopped(self) -> None:
        transcript = Path(self.directory.name) / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "role": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "We're done with this part. The next step needs your approval.",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.write(sample(status="blocked", blocked_reason="needs-user: please confirm I should continue"))
        result = stop_hook.handle(
            {
                "status": "completed",
                "conversation_id": "chat-a",
                "loop_count": 2,
                "model": "grok-4.7",
                "transcript_path": str(transcript),
            }
        )
        self.assertEqual(result, {})
        failure = json.loads((self.path.parent / "last-failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["gap"], "permission_dressed_as_block")
        self.assertEqual(failure["quit_shape"], "permission_ask")
        self.assertEqual(failure["decision"], "blocked")
        self.assertEqual(failure["loop_count"], 2)
        self.assertIn("needs your approval", failure["last_assistant"])
        self.assertFalse(failure["returned_followup"])

    def test_resume_does_not_count_as_failure(self) -> None:
        self.write(sample())
        stop_hook.handle({"status": "completed", "conversation_id": "chat-a", "loop_count": 0})
        last_stop = json.loads((self.path.parent / "last-stop.json").read_text(encoding="utf-8"))
        self.assertEqual(last_stop["decision"], "resumed")
        self.assertTrue(last_stop["returned_followup"])
        self.assertFalse((self.path.parent / "last-failure.json").exists())
        heartbeat = json.loads((self.path.parent / "hook-heartbeat.json").read_text(encoding="utf-8"))
        self.assertTrue(heartbeat["had_goal"])

    def test_abort_is_its_own_gap(self) -> None:
        self.write(sample(conversation_id="chat-a"))
        stop_hook.handle({"status": "aborted", "conversation_id": "chat-a"})
        failure = json.loads((self.path.parent / "last-failure.json").read_text(encoding="utf-8"))
        self.assertEqual(failure["gap"], "user_or_product_abort")

    def test_classify_quit_shapes(self) -> None:
        self.assertEqual(report.classify_quit("Want me to continue?"), "permission_ask")
        self.assertEqual(report.classify_quit("I've outlined the approach. Next steps are below."), "recap")
        self.assertEqual(report.classify_quit(""), "unknown")

    def test_install_keeps_other_hooks(self) -> None:
        merged = install_hook.merge_hooks(
            {"version": 1, "hooks": {"sessionStart": [{"command": "echo hi"}]}},
            "/usr/bin/python3 /Users/mac/.cursor/babysit/stop_hook.py",
        )
        self.assertEqual(merged["hooks"]["sessionStart"][0]["command"], "echo hi")
        self.assertIn("stop_hook.py", merged["hooks"]["stop"][0]["command"])
        self.assertEqual(merged["hooks"]["stop"][0]["loop_limit"], 48)


if __name__ == "__main__":
    unittest.main()
