#!/usr/bin/env python3
"""Checks for the goal ledger and the goal.py command line."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import goal  # noqa: E402
import ledger as L  # noqa: E402

PASS = f'"{sys.executable}" -c "raise SystemExit(0)"'
FAIL = f'"{sys.executable}" -c "print(\'boom\'); raise SystemExit(3)"'


class GoalCliTests(unittest.TestCase):
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

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = goal.main(list(argv))
        return code, buffer.getvalue()

    def start(self, *extra: str) -> str:
        code, out = self.run_cli("start", "--goal", "Ship the thing", "--done", "README updated", *extra)
        self.assertEqual(code, 0, out)
        return out.split()[1]

    def stored(self, goal_id: str) -> dict:
        return json.loads(L.goal_path(goal_id).read_text(encoding="utf-8"))

    def test_absent_without_goal(self) -> None:
        self.assertTrue(self.run_cli("status")[1].startswith("ABSENT"))

    def test_start_needs_a_criterion(self) -> None:
        code, out = self.run_cli("start", "--goal", "Do it")
        self.assertEqual(code, 2)
        self.assertIn("done-when", out)

    def test_start_rejects_prefilled_proof(self) -> None:
        spec = self.root / "spec.json"
        spec.write_text(json.dumps({"goal": "x", "done_when": [{"text": "y", "ok": True, "proof": "z"}]}), encoding="utf-8")
        code, out = self.run_cli("start", str(spec))
        self.assertEqual(code, 2)
        self.assertIn("cannot start with proof", out)

    def test_spec_file_and_flags_combine(self) -> None:
        spec = self.root / "spec.json"
        spec.write_text(
            json.dumps({"goal": "From spec", "done_when": ["a", {"text": "b", "check": PASS}], "steps": ["first"]}),
            encoding="utf-8",
        )
        code, out = self.run_cli("start", str(spec), "--done", "c", "--step", "second")
        self.assertEqual(code, 0, out)
        saved = self.stored(out.split()[1])
        self.assertEqual([c["text"] for c in saved["criteria"]], ["a", "b", "c"])
        self.assertEqual(saved["criteria"][1]["check"], PASS)
        self.assertEqual(saved["steps"], ["first", "second"])
        self.assertEqual(saved["workspace"], str(self.workspace.resolve()))

    def test_status_continues_while_open(self) -> None:
        self.start("--step", "write the README")
        out = self.run_cli("status")[1]
        self.assertTrue(out.startswith("CONTINUE"))
        self.assertIn("next: write the README", out)

    def test_done_needs_proof_and_close(self) -> None:
        goal_id = self.start("--check", "tests pass", PASS)
        self.assertTrue(self.run_cli("close")[1].startswith("CONTINUE"))
        self.run_cli("prove", "1", "README line 4 reads: run make")
        out = self.run_cli("status")[1]
        self.assertTrue(out.startswith("CONTINUE"))
        self.assertIn("goal.py close", out)
        out = self.run_cli("close")[1]
        self.assertTrue(out.startswith("DONE"), out)
        self.assertEqual(self.stored(goal_id)["status"], "done")

    def test_failed_check_stays_open_and_explains(self) -> None:
        goal_id = self.start("--check", "tests pass", FAIL)
        self.run_cli("prove", "1", "README shows it")
        out = self.run_cli("close")[1]
        self.assertTrue(out.startswith("CONTINUE"))
        self.assertIn("exit 3", out)
        self.assertIn("boom", self.stored(goal_id)["criteria"][1]["last_failure"])

    def test_close_reruns_checks_that_passed_earlier(self) -> None:
        marker = self.workspace / "ok"
        marker.write_text("1", encoding="utf-8")
        check = f'"{sys.executable}" -c "import os,sys; sys.exit(0 if os.path.exists(\'ok\') else 1)"'
        self.start("--check", "marker exists", check)
        self.run_cli("prove", "1", "done")
        self.assertIn("PASS", self.run_cli("check")[1])
        marker.unlink()
        self.assertTrue(self.run_cli("close")[1].startswith("CONTINUE"))

    def test_prove_refuses_criteria_with_checks(self) -> None:
        self.start("--check", "tests pass", PASS)
        code, out = self.run_cli("prove", "2", "trust me")
        self.assertEqual(code, 2)
        self.assertIn("check 2", out)

    def test_permission_ask_is_not_a_block(self) -> None:
        self.start()
        out = self.run_cli("block", "Please confirm I should continue")[1]
        self.assertTrue(out.startswith("NOT A BLOCK"))
        out = self.run_cli("block", "needs-user:")[1]
        self.assertTrue(out.startswith("NOT A BLOCK"))
        out = self.run_cli("block", "cap: pretend the hook stopped")[1]
        self.assertTrue(out.startswith("NOT A BLOCK"))

    def test_real_block_and_unblock(self) -> None:
        goal_id = self.start()
        out = self.run_cli("block", "needs-user: the Stripe test key")[1]
        self.assertTrue(out.startswith("BLOCKED"))
        self.assertIn("one message", out)
        self.run_cli("unblock", "--resumes", "5")
        saved = self.stored(goal_id)
        self.assertEqual(saved["status"], "active")
        self.assertIsNone(saved["blocked_reason"])
        self.assertEqual(saved["max_resumes"], 5)

    def test_steps_notes_and_decisions(self) -> None:
        goal_id = self.start("--step", "one", "--step", "two")
        self.run_cli("did", "it worked")
        self.run_cli("todo", "zero", "--first")
        self.run_cli("decide", "Picked SQLite because the app is single user")
        saved = self.stored(goal_id)
        self.assertEqual(saved["steps"], ["zero", "two"])
        self.assertEqual(saved["notes"], ["did: one -- it worked"])
        self.assertIn("Picked SQLite", saved["decisions"][0])

    def test_drop_is_reported(self) -> None:
        goal_id = self.start("--done", "second")
        self.run_cli("drop", "2", "the user said mobile is out of scope")
        saved = self.stored(goal_id)
        self.assertEqual(len(saved["criteria"]), 1)
        self.assertIn("mobile is out of scope", saved["decisions"][0])

    def test_new_goal_in_same_session_replaces_old(self) -> None:
        first = self.start("--session", "chat-a")
        second = self.start("--session", "chat-a")
        self.assertEqual(self.stored(first)["status"], "stopped")
        self.assertEqual(self.stored(second)["session_id"], "chat-a")

    def test_unfilled_session_placeholder_is_ignored(self) -> None:
        goal_id = self.start("--session", "${CLAUDE_SESSION_ID}")
        self.assertIsNone(self.stored(goal_id)["session_id"])

    def test_goal_resolves_by_folder_then_env(self) -> None:
        here = self.start()
        other = self.root / "other"
        other.mkdir()
        os.chdir(other)
        there = self.start()
        self.assertIn(there, self.run_cli("status")[1])
        os.chdir(self.workspace)
        self.assertIn(here, self.run_cli("status")[1])
        os.environ["UNTIL_DONE_GOAL"] = there
        self.assertIn(there, self.run_cli("status")[1])

    def test_two_goals_in_one_folder_need_id(self) -> None:
        first = self.start()
        self.start()
        code, out = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn(first, out)
        self.assertTrue(self.run_cli("status", "--id", first)[1].startswith("CONTINUE"))

    def test_ambiguous_goal_needs_id(self) -> None:
        for name in ("a", "b"):
            folder = self.root / name
            folder.mkdir()
            os.chdir(folder)
            self.start()
        os.chdir(self.root)
        code, out = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("--id", out)

    def test_invalid_file_is_reported(self) -> None:
        goal_id = self.start()
        L.goal_path(goal_id).write_text("{not json", encoding="utf-8")
        code, out = self.run_cli("status", "--id", goal_id)
        self.assertEqual(code, 2)
        self.assertIn("invalid", out)

    def test_block_kinds(self) -> None:
        self.assertEqual(L.block_kind("needs-user: the password"), "needs-user")
        self.assertEqual(L.block_kind("Destructive: delete prod table"), "destructive")
        self.assertIsNone(L.block_kind("needs-user"))
        self.assertIsNone(L.block_kind("waiting: for approval"))


if __name__ == "__main__":
    unittest.main()
