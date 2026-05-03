"""Tests for `donace run_start` and `donace list_runs`."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk import cli


class RunStartTest(unittest.TestCase):
    def test_mints_run_id_and_creates_skeleton(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            run_id = cli.run_start(cwd=tmp_path)

            self.assertRegex(run_id, r"^run-[0-9a-f]{8}$")
            run_dir = tmp_path / ".ai" / "runs" / run_id
            self.assertTrue(run_dir.is_dir())
            meta = json.loads((run_dir / "meta.json").read_text())
            self.assertEqual(meta["status"], "spec")
            self.assertIn("created_at", meta)

    def test_two_calls_produce_different_ids(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            id1 = cli.run_start(cwd=tmp_path)
            id2 = cli.run_start(cwd=tmp_path)
            self.assertNotEqual(id1, id2)


class ListRunsTest(unittest.TestCase):
    def test_returns_runs_in_creation_order(self):
        with TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            id1 = cli.run_start(cwd=tmp_path)
            id2 = cli.run_start(cwd=tmp_path)
            runs = cli.list_runs(cwd=tmp_path)
            self.assertEqual(set(runs), {id1, id2})

    def test_empty_when_no_runs(self):
        with TemporaryDirectory() as tmp:
            self.assertEqual(cli.list_runs(cwd=Path(tmp)), [])


if __name__ == "__main__":
    unittest.main()
