"""Tests for file_scope normalization + bash redirect detection.

Regressions from run-phase3-reader-24c1c0260e85:
- file_scope paths arrived with markdown backticks from plan.json (`apps/x.ts`),
  so the literal-string comparison in the Write/Edit hook and
  `_bash_writes_outside_scope` always said "outside scope". Stage 2 hit this
  6+ times on files that WERE listed in the plan.
- `_REDIR_RE` scanned the raw command, including content inside `node -e "..."`
  quotes, so JS arrow functions (`c=>d+=c`) and comparisons (`w.length>=1`)
  were treated as shell redirects with bogus targets.

Runs with stdlib unittest. Invoke from repo root:
    python3 -m unittest sdk.tests.test_file_scope -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sdk.agent_dispatch import (  # noqa: E402
    AgentDispatcher,
    _bash_writes_outside_scope,
    _normalize_file_scope,
)
from sdk.events import EventBus  # noqa: E402


class NormalizeFileScopeTests(unittest.TestCase):
    """Plan.json stores files as markdown code spans. Strip the noise once."""

    def test_strips_leading_and_trailing_backticks(self):
        self.assertEqual(
            _normalize_file_scope(["`apps/web/lib/x.ts`"]),
            ["apps/web/lib/x.ts"],
        )

    def test_strips_only_outer_backticks(self):
        # Inner backticks (rare in real paths) should survive.
        self.assertEqual(
            _normalize_file_scope(["`weird`name.ts`"]),
            ["weird`name.ts"],
        )

    def test_strips_trailing_slash(self):
        # Makes "apps/" and "apps" equivalent prefixes for subtree matches.
        self.assertEqual(
            _normalize_file_scope(["`apps/web/`"]),
            ["apps/web"],
        )

    def test_strips_whitespace(self):
        self.assertEqual(
            _normalize_file_scope(["  `apps/x.ts`  "]),
            ["apps/x.ts"],
        )

    def test_drops_empty_entries(self):
        self.assertEqual(
            _normalize_file_scope(["", "``", "   ", "`apps/x.ts`"]),
            ["apps/x.ts"],
        )

    def test_preserves_already_clean(self):
        self.assertEqual(
            _normalize_file_scope(["apps/x.ts", "apps/y.ts"]),
            ["apps/x.ts", "apps/y.ts"],
        )

    def test_passes_through_none(self):
        self.assertIsNone(_normalize_file_scope(None))


class BashRedirectFalsePositiveTests(unittest.TestCase):
    """_REDIR_RE must not scan inside quoted strings.

    Real production samples from phase3 that should NOT be flagged as writes:
    - `node -e "process.stdin.on('data', c=>d+=c)"`  (JS arrow function)
    - `node -e "if (w.length>=1) ..."`               (JS comparison)
    - `echo "a > b is HTML-like"`                     (string containing >)
    """

    def setUp(self):
        self.cwd = str(_REPO_ROOT)
        self.scope = ["apps/backend/src/app/api/v1/projects/route.ts"]

    def test_arrow_function_in_node_e_not_flagged(self):
        cmd = 'node -e "process.stdin.on(\'data\', c=>d+=c)"'
        self.assertIsNone(
            _bash_writes_outside_scope(cmd, self.scope, self.cwd),
            msg="arrow function '=>' should not count as redirect",
        )

    def test_ge_comparison_in_node_e_not_flagged(self):
        cmd = 'node -e "if (w.length>=1) console.log(w)"'
        self.assertIsNone(
            _bash_writes_outside_scope(cmd, self.scope, self.cwd),
            msg="'>=' inside quotes should not count as redirect",
        )

    def test_gt_in_double_quoted_string_not_flagged(self):
        cmd = 'echo "cookies a>b are weird"'
        self.assertIsNone(
            _bash_writes_outside_scope(cmd, self.scope, self.cwd),
            msg="'>' inside \"...\" should not count as redirect",
        )

    def test_gt_in_single_quoted_string_not_flagged(self):
        cmd = "echo 'data > value'"
        self.assertIsNone(
            _bash_writes_outside_scope(cmd, self.scope, self.cwd),
            msg="'>' inside '...' should not count as redirect",
        )

    def test_redirect_in_backtick_command_substitution_is_detected(self):
        cmd = "echo `echo hello > /tmp/out`"
        reason = _bash_writes_outside_scope(cmd, self.scope, self.cwd)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("redirection", reason.lower())

    def test_redirect_in_double_quoted_backtick_substitution_is_detected(self):
        cmd = 'echo "prefix `echo hello > /tmp/out`"'
        reason = _bash_writes_outside_scope(cmd, self.scope, self.cwd)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("redirection", reason.lower())

    def test_real_redirect_still_detected(self):
        # Spaces around `>` — canonical shell redirect outside any quoting.
        cmd = "echo hello > /etc/passwd"
        reason = _bash_writes_outside_scope(cmd, self.scope, self.cwd)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("redirection", reason.lower())

    def test_real_append_redirect_still_detected(self):
        cmd = "echo hello >> /tmp/log.txt"
        reason = _bash_writes_outside_scope(
            cmd, ["apps/backend/x.ts"], self.cwd,
        )
        self.assertIsNotNone(reason)

    def test_double_quoted_redirect_target_still_caught(self):
        # Codex P1: blanking quotes ate the target too. A legal shell
        # redirect to a quoted path must still scope-check against the
        # unquoted content.
        cmd = 'echo hi > "apps/outside-scope.ts"'
        reason = _bash_writes_outside_scope(cmd, self.scope, self.cwd)
        self.assertIsNotNone(reason, msg="out-of-scope quoted target must be flagged")
        assert reason is not None
        self.assertIn("apps/outside-scope.ts", reason)

    def test_single_quoted_redirect_target_still_caught(self):
        cmd = "echo hi > 'apps/outside-scope.ts'"
        reason = _bash_writes_outside_scope(cmd, self.scope, self.cwd)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertIn("apps/outside-scope.ts", reason)

    def test_quoted_in_scope_target_still_allowed(self):
        # Symmetry: legitimate in-scope writes with quoted paths must pass.
        cmd = 'echo hi > "apps/in-scope.ts"'
        self.assertIsNone(
            _bash_writes_outside_scope(cmd, ["apps/in-scope.ts"], self.cwd),
        )


class DispatcherConstructorTests(unittest.TestCase):
    """Dispatcher.file_scope should be clean the moment the object exists,
    so every downstream user (Write/Edit hook, bash checker) sees normal paths."""

    def test_constructor_normalizes_backticks(self):
        d = AgentDispatcher(
            agents_dir=str(_REPO_ROOT / "agents"),
            cwd=str(_REPO_ROOT),
            bus=EventBus(run_id="test"),
            file_scope=["`apps/web/x.ts`", "`apps/web/y.ts`"],
        )
        self.assertEqual(d.file_scope, ["apps/web/x.ts", "apps/web/y.ts"])

    def test_constructor_preserves_none(self):
        d = AgentDispatcher(
            agents_dir=str(_REPO_ROOT / "agents"),
            cwd=str(_REPO_ROOT),
            bus=EventBus(run_id="test"),
            file_scope=None,
        )
        self.assertIsNone(d.file_scope)


class BashScopeBacktickTests(unittest.TestCase):
    """_bash_writes_outside_scope must accept backtick-wrapped scope paths.

    This is the "stage-2 wrote reading-time.ts 6 times and got denied" case.
    """

    def test_write_inside_backtick_scope_is_allowed(self):
        cwd = str(_REPO_ROOT)
        # Scope strings as they literally appear in plan.json
        scope_with_backticks = ["`apps/web/lib/reading-time.ts`"]
        # A shell command that appends to the in-scope file.
        cmd = "echo 'export const x=1;' >> apps/web/lib/reading-time.ts"
        self.assertIsNone(
            _bash_writes_outside_scope(cmd, scope_with_backticks, cwd),
            msg="file listed in plan.json (with markdown backticks) should be in scope",
        )


if __name__ == "__main__":
    unittest.main()
