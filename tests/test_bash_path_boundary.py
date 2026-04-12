from __future__ import annotations

import unittest
from pathlib import Path

from sdk.agent_dispatch import _is_bash_path_outside_cwd, _is_blocked_command


REPO_ROOT = Path(__file__).resolve().parents[1]


class BashPathBoundaryTests(unittest.TestCase):
    def test_blocks_git_commit_in_automated_runs(self) -> None:
        reason = _is_blocked_command("git commit -m 'save progress'")
        self.assertIsNotNone(reason)
        self.assertIn("must not mutate git history", reason)

    def test_allows_repo_relative_commands(self) -> None:
        reason = _is_bash_path_outside_cwd("git diff README.md", "/tmp/project")
        self.assertIsNone(reason)

    def test_blocks_absolute_home_scan(self) -> None:
        reason = _is_bash_path_outside_cwd("find /Users/momo -name '*.py'", str(REPO_ROOT))
        self.assertIsNotNone(reason)
        self.assertIn("outside project directory", reason)

    def test_blocks_parent_directory_escape(self) -> None:
        reason = _is_bash_path_outside_cwd("find ../ -name '*.md'", str(REPO_ROOT))
        self.assertIsNotNone(reason)
        self.assertIn("outside project directory", reason)

    def test_allows_absolute_path_within_repo(self) -> None:
        reason = _is_bash_path_outside_cwd(
            f"cat {REPO_ROOT / 'README.md'}",
            str(REPO_ROOT),
        )
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
