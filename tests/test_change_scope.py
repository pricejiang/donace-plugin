from __future__ import annotations

import unittest

from sdk.change_scope import (
    is_documentation_only_change,
    is_documentation_path,
    looks_like_trivial_doc_task,
    parse_changed_files,
    relevant_changed_files,
)


class ChangeScopeTests(unittest.TestCase):
    def test_detects_documentation_paths(self) -> None:
        self.assertTrue(is_documentation_path("README.md"))
        self.assertTrue(is_documentation_path(".ai/sessions/2026-04/log.md"))
        self.assertFalse(is_documentation_path("sdk/orchestrator.py"))

    def test_documentation_only_change_ignores_runtime_artifacts(self) -> None:
        paths = [
            "README.md",
            ".ai/runs/run-1.json",
            "unittest_results.txt",
        ]
        self.assertEqual(relevant_changed_files(paths), ["README.md"])
        self.assertTrue(is_documentation_only_change(paths))

    def test_parses_git_style_changed_file_list(self) -> None:
        parsed = parse_changed_files("README.md\nsdk/orchestrator.py\n(no changed files detected)\n")
        self.assertEqual(parsed, ["README.md", "sdk/orchestrator.py"])

    def test_trivial_doc_task_keywords(self) -> None:
        self.assertTrue(looks_like_trivial_doc_task("Rename README title and keep changes minimal"))
        self.assertFalse(looks_like_trivial_doc_task("Implement websocket reconnect backoff"))


if __name__ == "__main__":
    unittest.main()
