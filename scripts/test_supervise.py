#!/usr/bin/env python3
"""Checks for supervise.py with a fake agent CLI standing in for claude or cursor-agent."""

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
import supervise  # noqa: E402

FAKE_AGENT = """#!{python}
import json, os, sys
sys.path.insert(0, {scripts!r})
import goal, ledger as L
mode = {mode!r}
assert os.environ.get("UNTIL_DONE_HOOK") == "off"
assert "-p" in sys.argv and "--extra-flag" in sys.argv
goal_id = os.environ["UNTIL_DONE_GOAL"]
data, _ = L.load(L.goal_path(goal_id))
if mode == "work":
    pending = L.unproved(data)
    if pending:
        goal.main(["prove", str(pending[0][0]), "fake agent saw it"])
    else:
        goal.main(["close"])
elif mode == "block":
    goal.main(["block", "needs-user: the API key"])
"""


class SuperviseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        os.environ["UNTIL_DONE_HOME"] = str(self.root / "state")
        os.environ.pop("UNTIL_DONE_GOAL", None)
        self.previous = Path.cwd()
        os.chdir(self.root)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            goal.main(["start", "--goal", "Two things", "--done", "first", "--done", "second"])
        self.goal_id = buffer.getvalue().split()[1]

    def tearDown(self) -> None:
        os.chdir(self.previous)
        os.environ.pop("UNTIL_DONE_HOME", None)
        os.environ.pop("UNTIL_DONE_GOAL", None)
        self.directory.cleanup()

    def fake(self, mode: str) -> str:
        path = self.root / f"fake-{mode}.py"
        path.write_text(FAKE_AGENT.format(python=sys.executable, scripts=str(SCRIPTS), mode=mode), encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def run_supervisor(self, mode: str, *extra: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = supervise.main(["--agent", "claude", "--bin", self.fake(mode), *extra, "--", "--extra-flag"])
        return code, buffer.getvalue()

    def stored(self) -> dict:
        return json.loads(L.goal_path(self.goal_id).read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "the fake agent relies on a shebang line")
    def test_runs_until_done(self) -> None:
        code, out = self.run_supervisor("work")
        self.assertEqual(code, 0, out)
        self.assertIn("run 3", out)
        self.assertNotIn("run 4", out)
        self.assertIn(f"DONE {self.goal_id}", out)
        self.assertEqual(self.stored()["status"], "done")
        self.assertTrue((L.home() / "runs" / self.goal_id / "001.log").is_file())

    @unittest.skipIf(os.name == "nt", "the fake agent relies on a shebang line")
    def test_stops_when_runs_change_nothing(self) -> None:
        code, out = self.run_supervisor("idle")
        self.assertEqual(code, 1)
        self.assertIn("recorded no progress", out)
        self.assertIn("run 2", out)
        self.assertNotIn("run 3", out)

    @unittest.skipIf(os.name == "nt", "the fake agent relies on a shebang line")
    def test_stops_on_a_real_block(self) -> None:
        code, out = self.run_supervisor("block")
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", out)
        self.assertNotIn("run 2", out)

    @unittest.skipIf(os.name == "nt", "the fake agent relies on a shebang line")
    def test_run_budget(self) -> None:
        code, out = self.run_supervisor("work", "--runs", "1")
        self.assertEqual(code, 1)
        self.assertIn("CONTINUE", out)

    def test_missing_cli_is_reported(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = supervise.main(["--agent", "cursor", "--bin", str(self.root / "missing-agent"), "--id", self.goal_id])
        self.assertEqual(code, 2)
        self.assertIn("Cannot find", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
