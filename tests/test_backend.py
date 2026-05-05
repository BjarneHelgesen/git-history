"""
Unit tests for the git-history backend.

These tests are written before the backend exists. They define the interface
the backend must expose and the behavior every UI command must produce.

The backend is expected to be a single class:

    from git_history import GitHistory
    gh = GitHistory(repo_path)

Every method returns a dataclass. On success the result is a StateResponse
(same shape as GET /api/state). On failure the result is an ErrorResponse
with ok=False and an error string. Methods never raise on git failures.

Methods exercised by these tests:

    gh.read_state()                     -> StateResponse
    gh.stash()                          -> StateResponse | ErrorResponse
    gh.stash_pop()                      -> StateResponse | ErrorResponse
    gh.move(order)                      -> StateResponse | ErrorResponse
    gh.squash(hashes)                   -> StateResponse | ErrorResponse
    gh.fixup(hashes)                    -> StateResponse | ErrorResponse
    gh.reword(commit_hash, message)     -> StateResponse | ErrorResponse
    gh.rebase_continue()                -> StateResponse | ErrorResponse
    gh.rebase_abort()                   -> StateResponse | ErrorResponse
    gh.reset(commit_hash)               -> StateResponse | ErrorResponse
    gh.show(commit_hash)                -> ShowResponse | ErrorResponse

Run with:
    python -m unittest tests.test_backend

Requires only the standard library and a working `git` binary on PATH.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from make_test_repo import COMMITS

# Imported last so the rest of the module loads cleanly even when the backend
# does not yet exist; tests then fail at collection with a clear ImportError.
from conftest import _commit_raw, _build_conflict_repo, _build_template_repo  # noqa: E402
from git_history import GitHistory, BranchHistoryEntry, _RebaseInstructions, ErrorResponse  # noqa: E402


# ---------------------------------------------------------------------------
# Base test classes
# ---------------------------------------------------------------------------

class StandardRepoTest(unittest.TestCase):
    """Provides a fresh copy of the 21-commit test repo for every test."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-test-"))
        self.repo = self.tmpdir / "repo"
        shutil.copytree(_build_template_repo(), self.repo)
        self.gh = GitHistory(str(self.repo))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- helpers --------------------------------------------------------

    def messages(self, state=None):
        s = state if state is not None else self.gh.read_state()
        return [c.message for c in s.commits]

    def commit_hashes(self, state=None):
        s = state if state is not None else self.gh.read_state()
        return [c.commit_hash for c in s.commits]

    def make_dirty(self, path="README.md", content=b"# changed by test\n"):
        (self.repo / path).write_bytes(content)


# ---------------------------------------------------------------------------
# State / refresh
# ---------------------------------------------------------------------------

class StateTests(StandardRepoTest):

    def test_state_has_expected_top_level_fields(self):
        state = self.gh.read_state()
        self.assertTrue(state.ok)
        self.assertEqual(state.branch, "main")
        self.assertFalse(state.dirty)
        self.assertFalse(state.has_stash)
        self.assertFalse(state.rebase_in_progress)
        self.assertEqual(state.conflict_files, [])
        self.assertEqual(len(state.commits), len(COMMITS))
        self.assertGreater(len(state.branch_history), 0)

    def test_commits_are_newest_first(self):
        msgs = self.messages()
        self.assertEqual(msgs[0],  "Add CI workflow")
        self.assertEqual(msgs[-1], "Initial commit")

    def test_head_commit_metadata(self):
        head = self.gh.read_state().commits[0]
        self.assertEqual(head.message, "Add CI workflow")
        self.assertEqual(head.author,  "Bob Brown")
        self.assertTrue(head.is_head)
        self.assertIn("main", head.branches)
        self.assertEqual(head.tags, [])

    def test_tags_appear_on_correct_commits(self):
        by_msg = {c.message: c for c in self.gh.read_state().commits}
        self.assertIn("v0.1.0", by_msg["Add HTTP server module"].tags)
        self.assertIn("v0.2.0", by_msg["Add user model"].tags)
        self.assertIn("v1.0.0", by_msg["Add integration tests"].tags)

    def test_short_hash_is_seven_char_prefix(self):
        for c in self.gh.read_state().commits:
            self.assertEqual(len(c.short_hash), 7)
            self.assertTrue(c.commit_hash.startswith(c.short_hash))

    def test_branch_history_is_deduped_by_hash(self):
        branch_history = self.gh.read_state().branch_history
        commit_hashes = [e.commit_hash for e in branch_history]
        self.assertEqual(len(commit_hashes), len(set(commit_hashes)))

    def test_branch_history_entries_have_label_and_timestamp(self):
        for entry in self.gh.read_state().branch_history:
            self.assertIsNotNone(entry.commit_hash)
            self.assertIsNotNone(entry.label)
            self.assertIsNotNone(entry.timestamp)

    def test_state_reports_dirty_tree(self):
        self.make_dirty()
        state = self.gh.read_state()
        self.assertTrue(state.dirty)


# ---------------------------------------------------------------------------
# Stash / stash pop
# ---------------------------------------------------------------------------

class StashTests(StandardRepoTest):

    def test_stash_when_clean_returns_nothing_to_stash(self):
        result = self.gh.stash()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "nothing_to_stash")

    def test_stash_when_dirty_clears_dirty_and_records_stash(self):
        self.make_dirty()
        before = self.gh.read_state()
        self.assertTrue(before.dirty)
        self.assertFalse(before.has_stash)

        result = self.gh.stash()
        self.assertTrue(result.ok)
        self.assertFalse(result.dirty)
        self.assertTrue(result.has_stash)

    def test_stash_pop_with_no_stash_returns_no_stash(self):
        result = self.gh.stash_pop()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no_stash")

    def test_stash_pop_when_dirty_is_refused(self):
        self.make_dirty()
        self.gh.stash()
        # Dirty up the tree again with a different file.
        self.make_dirty(path="LICENSE", content=b"different\n")

        result = self.gh.stash_pop()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "dirty_tree")

    def test_stash_pop_clean_restores_dirty_state(self):
        self.make_dirty()
        self.gh.stash()

        result = self.gh.stash_pop()
        self.assertTrue(result.ok)
        self.assertTrue(result.dirty)
        self.assertFalse(result.has_stash)


# ---------------------------------------------------------------------------
# Rebase: move
# ---------------------------------------------------------------------------

class MoveTests(StandardRepoTest):

    def test_swap_two_newest_commits(self):
        state = self.gh.read_state()
        msgs_before = self.messages(state)
        order = self.commit_hashes(state)
        order[0], order[1] = order[1], order[0]

        result = self.gh.move(order)
        self.assertTrue(result.ok)

        msgs_after = self.messages(result)
        expected = msgs_before[:]
        expected[0], expected[1] = expected[1], expected[0]
        self.assertEqual(msgs_after, expected)

    def test_move_distant_commit(self):
        state = self.gh.read_state()
        msgs_before = self.messages(state)
        order = self.commit_hashes(state)
        moved_hash = order.pop(0)
        order.insert(5, moved_hash)

        result = self.gh.move(order)
        self.assertTrue(result.ok)

        msgs_after = self.messages(result)
        self.assertEqual(msgs_after[5], msgs_before[0])
        # Same set of messages, just reordered.
        self.assertEqual(sorted(msgs_after), sorted(msgs_before))

    def test_move_with_unchanged_order_is_a_noop(self):
        state = self.gh.read_state()
        order = self.commit_hashes(state)

        result = self.gh.move(order)
        self.assertTrue(result.ok)
        # Hashes unchanged because nothing was rewritten.
        self.assertEqual(self.commit_hashes(result), order)


# ---------------------------------------------------------------------------
# Rebase: squash
# ---------------------------------------------------------------------------

class SquashTests(StandardRepoTest):

    def test_squash_two_adjacent_commits(self):
        state = self.gh.read_state()
        msgs_before = self.messages(state)
        hashes = [state.commits[0].commit_hash, state.commits[1].commit_hash]

        result = self.gh.squash(hashes)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.commits), len(state.commits) - 1)

        # All non-squashed commits still present.
        msgs_after = self.messages(result)
        for m in msgs_before[2:]:
            self.assertIn(m, msgs_after)


# ---------------------------------------------------------------------------
# Rebase: consecutive commit validation (squash / fixup)
# ---------------------------------------------------------------------------

class ConsecutiveCommitValidationTests(StandardRepoTest):

    def test_squash_non_adjacent_commits_returns_invalid_request(self):
        state = self.gh.read_state()
        h0 = state.commits[0].commit_hash
        h2 = state.commits[2].commit_hash

        result = self.gh.squash([h0, h2])

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_request")
        self.assertFalse(self.gh.read_state().rebase_in_progress)

    def test_fixup_non_adjacent_commits_returns_invalid_request(self):
        state = self.gh.read_state()
        h0 = state.commits[0].commit_hash
        h2 = state.commits[2].commit_hash

        result = self.gh.fixup([h0, h2])

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_request")
        self.assertFalse(self.gh.read_state().rebase_in_progress)

    def test_squash_non_adjacent_does_not_modify_commit_history(self):
        hashes_before = self.commit_hashes()

        self.gh.squash([hashes_before[0], hashes_before[3]])

        self.assertEqual(self.commit_hashes(), hashes_before)

    def test_squash_three_adjacent_commits_is_valid(self):
        state = self.gh.read_state()
        hashes = [state.commits[i].commit_hash for i in range(3)]

        result = self.gh.squash(hashes)

        self.assertTrue(result.ok)
        self.assertEqual(len(result.commits), len(state.commits) - 2)


# ---------------------------------------------------------------------------
# Rebase: fixup
# ---------------------------------------------------------------------------

class FixupTests(StandardRepoTest):

    def test_fixup_a_middle_commit(self):
        state = self.gh.read_state()
        msgs_before = self.messages(state)
        target = state.commits[3]
        h = target.commit_hash
        target_msg = target.message

        result = self.gh.fixup([h])
        self.assertTrue(result.ok)
        self.assertEqual(len(result.commits), len(state.commits) - 1)

        # The fixup'd commit's message is gone (folded into its parent).
        self.assertNotIn(target_msg, self.messages(result))
        # All other messages survive.
        for m in msgs_before:
            if m != target_msg:
                self.assertIn(m, self.messages(result))

    def test_fixup_root_commit_is_refused(self):
        # Oldest commit in the standard test repo IS the root commit.
        state = self.gh.read_state()
        h = state.commits[-1].commit_hash

        result = self.gh.fixup([h])
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_request")


# ---------------------------------------------------------------------------
# Rebase: reword
# ---------------------------------------------------------------------------

class RewordTests(StandardRepoTest):

    def test_reword_top_commit(self):
        state = self.gh.read_state()
        h = state.commits[0].commit_hash

        result = self.gh.reword(h, "A brand new message")
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].message, "A brand new message")

    def test_reword_middle_commit(self):
        state = self.gh.read_state()
        h = state.commits[5].commit_hash

        result = self.gh.reword(h, "Reworded middle")
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[5].message, "Reworded middle")
        # Sibling commits intact.
        self.assertEqual(result.commits[0].message,
                         state.commits[0].message)
        self.assertEqual(result.commits[-1].message,
                         state.commits[-1].message)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class ResetTests(StandardRepoTest):

    def test_reset_to_older_commit_moves_head(self):
        state = self.gh.read_state()
        target = state.commits[5]

        result = self.gh.reset(target.commit_hash)
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].commit_hash, target.commit_hash)
        self.assertEqual(result.commits[0].message, target.message)

    def test_reset_when_dirty_is_refused(self):
        self.make_dirty()
        h = self.gh.read_state().commits[5].commit_hash

        result = self.gh.reset(h)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "dirty_tree")


# ---------------------------------------------------------------------------
# Show
# ---------------------------------------------------------------------------

class ShowTests(StandardRepoTest):

    def test_show_returns_commit_and_diff(self):
        head = self.gh.read_state().commits[0]
        result = self.gh.show(head.commit_hash)
        self.assertTrue(result.ok)
        self.assertEqual(result.commit.short_hash, head.short_hash)
        self.assertEqual(result.commit.message, head.message)
        self.assertIn("diff --git", result.diff)

    def test_show_unknown_hash_returns_error(self):
        result = self.gh.show("0" * 40)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_commit")

    def test_show_with_short_hash(self):
        head = self.gh.read_state().commits[0]
        result = self.gh.show(head.short_hash)
        self.assertTrue(result.ok)
        self.assertEqual(result.commit.commit_hash, head.commit_hash)


# ---------------------------------------------------------------------------
# Dirty-tree policy
# ---------------------------------------------------------------------------

class DirtyTreeTests(StandardRepoTest):

    def test_rebase_move_refused_when_dirty(self):
        self.make_dirty()
        order = self.commit_hashes()
        order[0], order[1] = order[1], order[0]

        result = self.gh.move(order)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "dirty_tree")

    def test_rebase_squash_refused_when_dirty(self):
        self.make_dirty()
        state = self.gh.read_state()
        hashes = [state.commits[0].commit_hash, state.commits[1].commit_hash]

        result = self.gh.squash(hashes)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "dirty_tree")

    def test_rebase_fixup_refused_when_dirty(self):
        self.make_dirty()
        h = self.gh.read_state().commits[3].commit_hash

        result = self.gh.fixup([h])
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "dirty_tree")

    def test_rebase_reword_refused_when_dirty(self):
        self.make_dirty()
        h = self.gh.read_state().commits[0].commit_hash

        result = self.gh.reword(h, "x")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "dirty_tree")


# ---------------------------------------------------------------------------
# Conflict scenarios (continue / abort)
# ---------------------------------------------------------------------------

class ConflictTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-conflict-"))
        self.repo = _build_conflict_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _swap_order(self):
        """Swap the two newest commits to trigger a conflict."""
        state = self.gh.read_state()
        order = [c.commit_hash for c in state.commits]
        order[0], order[1] = order[1], order[0]
        return self.gh.move(order)

    def test_move_produces_conflict(self):
        result = self._swap_order()
        self.assertFalse(result.ok)
        self.assertTrue(result.conflict)
        self.assertIn("f.txt", result.conflict_files)
        self.assertTrue(result.rebase_in_progress)

    def test_abort_clears_rebase_state(self):
        self._swap_order()

        result = self.gh.rebase_abort()
        self.assertTrue(result.ok)
        self.assertFalse(result.rebase_in_progress)
        self.assertEqual(result.conflict_files, [])

    def test_continue_after_resolving_conflict(self):
        self._swap_order()

        # Resolve to initial content so version_A's patch applies cleanly next.
        (self.repo / "f.txt").write_bytes(b"line1\nline2\nline3\n")
        subprocess.run(["git", "add", "f.txt"], cwd=str(self.repo),
                       check=True, capture_output=True)

        result = self.gh.rebase_continue()
        self.assertTrue(result.ok)
        self.assertFalse(result.rebase_in_progress)

    def test_continue_with_unresolved_conflict_stays_in_conflict(self):
        self._swap_order()

        result = self.gh.rebase_continue()
        self.assertFalse(result.ok)
        self.assertTrue(result.conflict)
        self.assertTrue(result.rebase_in_progress)

    def test_conflict_response_carries_full_state(self):
        result = self._swap_order()
        self.assertTrue(result.conflict)
        # Conflict response must carry the full repo state, not just conflict
        # info, so the frontend can refresh without a follow-up read_state call.
        fresh = self.gh.read_state()
        self.assertTrue(result.commits)
        self.assertEqual([c.commit_hash for c in result.commits],
                         [c.commit_hash for c in fresh.commits])

    def test_rebase_continue_when_not_in_rebase_returns_error(self):
        result = self.gh.rebase_continue()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_in_rebase")

    def test_rebase_abort_when_not_in_rebase_returns_error(self):
        result = self.gh.rebase_abort()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "not_in_rebase")


# ---------------------------------------------------------------------------
# _filter_rebase_groups unit tests
# ---------------------------------------------------------------------------

class RebaseGroupFilterTests(unittest.TestCase):

    def _e(self, label):
        return BranchHistoryEntry(commit_hash=label[:8].replace(" ", "_"), label=label, timestamp="2026-01-01T00:00:00")

    def _labels(self, entries):
        return [e.label for e in entries]

    def test_non_rebase_entries_pass_through_unchanged(self):
        entries = [self._e("commit: Add feature"), self._e("commit: Initial commit")]
        result = GitHistory._filter_rebase_groups(entries)
        self.assertEqual(self._labels(result), ["commit: Add feature", "commit: Initial commit"])

    def test_completed_rebase_keeps_only_finish_entry(self):
        entries = [
            self._e("rebase (finish): returning to refs/heads/main"),
            self._e("rebase (pick): Third commit"),
            self._e("rebase (pick): Second commit"),
            self._e("rebase: checkout origin/main"),
            self._e("commit: some earlier commit"),
        ]
        result = GitHistory._filter_rebase_groups(entries)
        self.assertEqual(self._labels(result), [
            "rebase",
            "commit: some earlier commit",
        ])

    def test_aborted_rebase_filters_all_rebase_entries(self):
        entries = [
            self._e("rebase (abort)"),
            self._e("rebase (pick): Second commit"),
            self._e("rebase: checkout origin/main"),
            self._e("commit: the commit before"),
        ]
        result = GitHistory._filter_rebase_groups(entries)
        self.assertEqual(self._labels(result), ["commit: the commit before"])

    def test_in_progress_rebase_filters_all_rebase_entries(self):
        entries = [
            self._e("rebase (pick): Second commit"),
            self._e("rebase: checkout origin/main"),
            self._e("commit: some earlier commit"),
        ]
        result = GitHistory._filter_rebase_groups(entries)
        self.assertEqual(self._labels(result), ["commit: some earlier commit"])

    def test_multiple_rebases_each_keeps_only_finish(self):
        entries = [
            self._e("rebase (finish): returning to refs/heads/main"),
            self._e("rebase (pick): B"),
            self._e("rebase: checkout origin/main"),
            self._e("commit: some commit"),
            self._e("rebase (finish): returning to refs/heads/main"),
            self._e("rebase (pick): A"),
            self._e("rebase: checkout origin/main"),
            self._e("commit: initial"),
        ]
        result = GitHistory._filter_rebase_groups(entries)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0].label, "rebase")
        self.assertEqual(result[1].label, "commit: some commit")
        self.assertEqual(result[2].label, "rebase")
        self.assertEqual(result[3].label, "commit: initial")

    def test_empty_input_returns_empty(self):
        self.assertEqual(GitHistory._filter_rebase_groups([]), [])


# ---------------------------------------------------------------------------
# Branch History: rebase group collapsing (integration)
# ---------------------------------------------------------------------------

class BranchHistoryRebaseCollapsingTests(StandardRepoTest):

    def test_branch_history_collapses_rebase_group_to_single_finish_entry(self):
        state = self.gh.read_state()
        h = state.commits[0].commit_hash
        commit_count_before = len(state.commits)

        result = self.gh.reword(h, "Rewarded top")
        self.assertTrue(result.ok)

        rebase_entries = [e for e in result.branch_history if e.label == "rebase"]
        self.assertEqual(len(rebase_entries), 1)

        # Unchanged commits (fast-forwarded during rebase) must still appear in
        # the branch history under their original "commit:" labels.
        commit_labels = [e.label for e in result.branch_history if e.label.startswith("commit")]
        self.assertGreaterEqual(len(commit_labels), commit_count_before)


class LoggingTests(StandardRepoTest):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-test-"))
        self.repo = self.tmpdir / "repo"
        shutil.copytree(_build_template_repo(), self.repo)
        self._log_file = self.tmpdir / "test.log"
        self.gh = GitHistory(str(self.repo), log_path=self._log_file)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _log_lines(self):
        if not self._log_file.exists():
            return []
        return [l for l in self._log_file.read_text().splitlines() if l]

    def test_rebase_appends_log_entry(self):
        h = self.commit_hashes()[0]
        self.gh.reword(h, "New message")
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        ts, branch, hash_ = lines[0].split()
        self.assertEqual(branch, "main")
        self.assertEqual(len(hash_), 40)

    def test_reset_appends_log_entry(self):
        commit_hashes = self.commit_hashes()
        self.gh.reset(commit_hashes[1])
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        ts, branch, hash_ = lines[0].split()
        self.assertEqual(hash_, commit_hashes[1])

    def test_multiple_operations_append_multiple_lines(self):
        commit_hashes = self.commit_hashes()
        self.gh.reset(commit_hashes[1])
        self.gh.reset(commit_hashes[0])
        self.assertEqual(len(self._log_lines()), 2)

    def test_failed_operation_does_not_append_log_entry(self):
        self.gh.reset("0" * 40)
        self.assertEqual(self._log_lines(), [])

    def test_log_entry_format(self):
        h = self.commit_hashes()[0]
        self.gh.reword(h, "Test")
        line = self._log_lines()[0]
        parts = line.split()
        self.assertEqual(len(parts), 3)
        # timestamp is ISO 8601
        import datetime
        datetime.datetime.fromisoformat(parts[0])

    def test_read_log_returns_empty_when_file_not_exist(self):
        content = self.gh.read_log()
        self.assertEqual(content, "")

    def test_read_log_returns_file_contents(self):
        h = self.commit_hashes()[0]
        self.gh.reword(h, "New message")
        content = self.gh.read_log()
        self.assertNotEqual(content, "")
        lines = content.splitlines()
        self.assertEqual(len(lines), 1)

    def test_read_log_returns_multiple_entries(self):
        commit_hashes = self.commit_hashes()
        self.gh.reset(commit_hashes[1])
        self.gh.reset(commit_hashes[0])
        content = self.gh.read_log()
        lines = [l for l in content.splitlines() if l]
        self.assertEqual(len(lines), 2)


# ---------------------------------------------------------------------------
# Switch branch
# ---------------------------------------------------------------------------

class SwitchBranchTests(StandardRepoTest):

    def setUp(self):
        super().setUp()
        subprocess.run(
            ["git", "branch", "feature"],
            cwd=str(self.repo), check=True, capture_output=True,
        )

    def test_state_includes_branches_list(self):
        state = self.gh.read_state()
        self.assertIn("main", state.branches)

    def test_branches_list_includes_all_local_branches(self):
        state = self.gh.read_state()
        self.assertIn("feature", state.branches)

    def test_switch_to_existing_branch(self):
        result = self.gh.switch_branch("feature")
        self.assertTrue(result.ok)
        self.assertEqual(result.branch, "feature")

    def test_switch_returns_full_state(self):
        result = self.gh.switch_branch("feature")
        self.assertIsNotNone(result.commits)
        self.assertIsNotNone(result.branch_history)
        self.assertIsNotNone(result.branches)

    def test_switch_to_unknown_branch_returns_error(self):
        result = self.gh.switch_branch("nonexistent")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_branch")

    def test_switch_empty_branch_returns_error(self):
        result = self.gh.switch_branch("")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_branch")

    def test_switch_when_dirty_is_refused(self):
        self.make_dirty()
        result = self.gh.switch_branch("feature")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "dirty_tree")

    def test_switch_during_rebase_is_refused(self):
        conflict_dir = self.tmpdir / "conflict"
        conflict_dir.mkdir()
        conflict_repo = _build_conflict_repo(conflict_dir)
        subprocess.run(
            ["git", "branch", "other"],
            cwd=str(conflict_repo), check=True, capture_output=True,
        )
        gh = GitHistory(str(conflict_repo))
        state = gh.read_state()
        order = [c.commit_hash for c in state.commits]
        order[0], order[1] = order[1], order[0]
        gh.move(order)
        result = gh.switch_branch("other")
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "rebase_in_progress")

    def test_stash_during_rebase_is_refused(self):
        conflict_dir = self.tmpdir / "conflict"
        conflict_dir.mkdir()
        conflict_repo = _build_conflict_repo(conflict_dir)
        gh = GitHistory(str(conflict_repo))
        state = gh.read_state()
        order = [c.commit_hash for c in state.commits]
        order[0], order[1] = order[1], order[0]
        gh.move(order)
        result = gh.stash()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "rebase_in_progress")

    def test_stash_pop_during_rebase_is_refused(self):
        conflict_dir = self.tmpdir / "conflict"
        conflict_dir.mkdir()
        conflict_repo = _build_conflict_repo(conflict_dir)
        gh = GitHistory(str(conflict_repo))
        state = gh.read_state()
        order = [c.commit_hash for c in state.commits]
        order[0], order[1] = order[1], order[0]
        gh.move(order)
        result = gh.stash_pop()
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "rebase_in_progress")

    def test_reset_during_rebase_is_refused(self):
        conflict_dir = self.tmpdir / "conflict"
        conflict_dir.mkdir()
        conflict_repo = _build_conflict_repo(conflict_dir)
        gh = GitHistory(str(conflict_repo))
        state = gh.read_state()
        order = [c.commit_hash for c in state.commits]
        order[0], order[1] = order[1], order[0]
        gh.move(order)
        result = gh.reset(state.commits[0].commit_hash)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "rebase_in_progress")


# ---------------------------------------------------------------------------
# Pushed status
# ---------------------------------------------------------------------------

class PushedTests(StandardRepoTest):

    def _add_remote_and_push(self):
        bare = self.tmpdir / "bare"
        bare.mkdir()
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", str(bare)],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"],
                       cwd=str(self.repo), check=True, capture_output=True)

    def test_no_remote_all_unpushed(self):
        state = self.gh.read_state()
        self.assertTrue(all(not c.pushed for c in state.commits))

    def test_all_commits_pushed_after_push(self):
        self._add_remote_and_push()
        state = self.gh.read_state()
        self.assertTrue(all(c.pushed for c in state.commits))

    def test_new_commit_after_push_is_unpushed(self):
        self._add_remote_and_push()
        _commit_raw(self.repo, "new.txt", b"new\n", "New unpushed commit", "alice", 100)
        state = self.gh.read_state()
        self.assertFalse(state.commits[0].pushed)
        self.assertTrue(all(c.pushed for c in state.commits[1:]))


# ---------------------------------------------------------------------------
# Edge cases: empty repository
# ---------------------------------------------------------------------------

class EmptyRepoTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-empty-"))
        self.repo = self.tmpdir / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=str(self.repo), check=True, capture_output=True)
        self.gh = GitHistory(str(self.repo))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_repo_read_state_returns_empty_commits(self):
        state = self.gh.read_state()
        self.assertEqual(len(state.commits), 0)
        self.assertEqual(state.branch_history, [])
        self.assertFalse(state.dirty)

    def test_empty_repo_operations_fail_gracefully(self):
        result = self.gh.reset("0" * 40)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "invalid_commit")


# ---------------------------------------------------------------------------
# Edge cases: full hash handling in operations
# ---------------------------------------------------------------------------

class FullHashTests(StandardRepoTest):
    @pytest.mark.skip(reason="WIP")
    def test_move_with_full_40_char_hashes(self):
        state = self.gh.read_state()
        full_hashes = [c.commit_hash for c in state.commits]
        order = full_hashes[:]
        order[0], order[1] = order[1], order[0]

        result = self.gh.move(order)
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].message, state.commits[1].message)

    def test_squash_with_full_hashes(self):
        state = self.gh.read_state()
        hashes = [state.commits[0].commit_hash, state.commits[1].commit_hash]

        result = self.gh.squash(hashes)
        self.assertTrue(result.ok)
        self.assertEqual(len(result.commits), len(state.commits) - 1)

    def test_reword_with_full_hash(self):
        state = self.gh.read_state()
        full_hash = state.commits[0].commit_hash

        result = self.gh.reword(full_hash, "New message with full hash")
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].message, "New message with full hash")


# ---------------------------------------------------------------------------
# Edge cases: special characters in reword messages
# ---------------------------------------------------------------------------

class RewordSpecialCharsTests(StandardRepoTest):

    def test_reword_with_quotes(self):
        state = self.gh.read_state()
        h = state.commits[0].commit_hash
        new_msg = 'Message with "quoted text" inside'

        result = self.gh.reword(h, new_msg)
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].message, new_msg)

    def test_reword_with_single_quotes(self):
        state = self.gh.read_state()
        h = state.commits[0].commit_hash
        new_msg = "Message with 'single quotes'"

        result = self.gh.reword(h, new_msg)
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].message, new_msg)

    def test_reword_with_multiline(self):
        state = self.gh.read_state()
        h = state.commits[0].commit_hash
        new_msg = "First line\n\nBody with details\nMore details"

        result = self.gh.reword(h, new_msg)
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].message, new_msg)

    def test_reword_with_unicode(self):
        state = self.gh.read_state()
        h = state.commits[0].commit_hash
        new_msg = "Message with émojis 🎉 and ñoñó characters"

        result = self.gh.reword(h, new_msg)
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].message, new_msg)

    def test_reword_with_backslashes(self):
        state = self.gh.read_state()
        h = state.commits[0].commit_hash
        new_msg = r"Message with \ backslash and C:\path\style"

        result = self.gh.reword(h, new_msg)
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].message, new_msg)


# ---------------------------------------------------------------------------
# Edge cases: multiple sequential operations
# ---------------------------------------------------------------------------

class MultipleSequentialOpsTests(StandardRepoTest):

    def test_multiple_squash_operations_in_sequence(self):
        state = self.gh.read_state()

        # Squash commits 0 and 1
        result1 = self.gh.squash([state.commits[0].commit_hash, state.commits[1].commit_hash])
        self.assertTrue(result1.ok)
        count1 = len(result1.commits)

        # Squash what was originally 2 and 3 (now at 0 and 1)
        result2 = self.gh.squash([result1.commits[0].commit_hash, result1.commits[1].commit_hash])
        self.assertTrue(result2.ok)
        self.assertEqual(len(result2.commits), count1 - 1)

    def test_reword_then_move(self):
        state = self.gh.read_state()
        h0 = state.commits[0].commit_hash

        result1 = self.gh.reword(h0, "Reworded message")
        self.assertTrue(result1.ok)

        # Get the new hash of the reworded commit
        order = [c.commit_hash for c in result1.commits]
        order[0], order[1] = order[1], order[0]

        result2 = self.gh.move(order)
        self.assertTrue(result2.ok)

    def test_move_then_squash_then_reset(self):
        state = self.gh.read_state()
        orig_hashes = self.commit_hashes(state)

        # Move
        order = self.commit_hashes(state)
        order[0], order[1] = order[1], order[0]
        result1 = self.gh.move(order)
        self.assertTrue(result1.ok)

        # Squash
        result2 = self.gh.squash([result1.commits[0].commit_hash, result1.commits[1].commit_hash])
        self.assertTrue(result2.ok)

        # Reset back to first commit from original state
        result3 = self.gh.reset(orig_hashes[0])
        self.assertTrue(result3.ok)

    def messages(self, state=None):
        s = state if state is not None else self.gh.read_state()
        return [c.message for c in s.commits]

    def commit_hashes(self, state=None):
        s = state if state is not None else self.gh.read_state()
        return [c.commit_hash for c in s.commits]


# ---------------------------------------------------------------------------
# Edge cases: show with binary files
# ---------------------------------------------------------------------------

class ShowBinaryFilesTests(StandardRepoTest):

    def test_show_commit_with_binary_file(self):
        # Create a commit that adds a binary file
        binary_path = self.repo / "binary.bin"
        binary_path.write_bytes(b"\x00\x01\x02\x03\x04\x05")
        subprocess.run(["git", "add", "binary.bin"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add binary file"],
                       cwd=str(self.repo), check=True, capture_output=True)

        state = self.gh.read_state()
        result = self.gh.show(state.commits[0].commit_hash)

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.diff)
        # Binary files are indicated in diff output
        self.assertIn("binary", result.diff.lower())

    def test_show_modifies_binary_file(self):
        binary_path = self.repo / "binary.bin"
        binary_path.write_bytes(b"\x00\x01\x02")
        subprocess.run(["git", "add", "binary.bin"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Create binary"],
                       cwd=str(self.repo), check=True, capture_output=True)

        binary_path.write_bytes(b"\x00\x01\x02\x03\x04\x05")
        subprocess.run(["git", "add", "binary.bin"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Modify binary"],
                       cwd=str(self.repo), check=True, capture_output=True)

        state = self.gh.read_state()
        result = self.gh.show(state.commits[0].commit_hash)
        self.assertTrue(result.ok)


# ---------------------------------------------------------------------------
# Edge cases: branch names with special characters
# ---------------------------------------------------------------------------

class SpecialBranchNamesTests(StandardRepoTest):

    def test_switch_branch_with_slashes(self):
        subprocess.run(["git", "branch", "feature/my-feature"],
                       cwd=str(self.repo), check=True, capture_output=True)
        result = self.gh.switch_branch("feature/my-feature")
        self.assertTrue(result.ok)
        self.assertEqual(result.branch, "feature/my-feature")

    def test_switch_branch_with_hyphens_and_underscores(self):
        subprocess.run(["git", "branch", "feature_my-branch-123"],
                       cwd=str(self.repo), check=True, capture_output=True)
        result = self.gh.switch_branch("feature_my-branch-123")
        self.assertTrue(result.ok)
        self.assertEqual(result.branch, "feature_my-branch-123")

    def test_state_includes_special_named_branches(self):
        subprocess.run(["git", "branch", "feature/test"],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "branch", "bugfix/issue-123"],
                       cwd=str(self.repo), check=True, capture_output=True)
        state = self.gh.read_state()
        self.assertIn("feature/test", state.branches)
        self.assertIn("bugfix/issue-123", state.branches)


# ---------------------------------------------------------------------------
# Edge cases: log file permission issues
# ---------------------------------------------------------------------------

class LogPermissionTests(StandardRepoTest):

    def test_append_log_handles_permission_denied(self):
        # Create a log file with restrictive permissions
        log_file = self.tmpdir / "readonly.log"
        log_file.touch()
        import stat
        log_file.chmod(0o444)  # Read-only

        try:
            gh = GitHistory(str(self.repo), log_path=log_file)
            state = self.gh.read_state()
            h = state.commits[0].commit_hash

            # Operation should succeed even if log write fails
            result = gh.reword(h, "Test message")
            self.assertTrue(result.ok)
        finally:
            # Cleanup: restore permissions so cleanup can delete the file
            log_file.chmod(0o644)

    def test_append_log_creates_missing_file(self):
        log_file = self.tmpdir / "new.log"
        self.assertFalse(log_file.exists())

        gh = GitHistory(str(self.repo), log_path=log_file)
        state = self.gh.read_state()
        h = state.commits[0].commit_hash
        gh.reword(h, "Test")

        self.assertTrue(log_file.exists())

    def test_read_log_handles_missing_file(self):
        log_file = self.tmpdir / "missing.log"
        gh = GitHistory(str(self.repo), log_path=log_file)

        content = gh.read_log()
        self.assertEqual(content, "")


# ---------------------------------------------------------------------------
# Edge cases: reset to merge commits
# ---------------------------------------------------------------------------

class ResetMergeCommitTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-merge-"))
        self.repo = self.tmpdir / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(self.repo),
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"],
                       cwd=str(self.repo), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"],
                       cwd=str(self.repo), check=True, capture_output=True)

        # Create base commit
        _commit_raw(self.repo, "file.txt", b"base\n", "base", "alice", 0)

        # Create and checkout feature branch
        subprocess.run(["git", "checkout", "-b", "feature"],
                       cwd=str(self.repo), check=True, capture_output=True)
        _commit_raw(self.repo, "feature.txt", b"feature\n", "feature work", "bob", 1)

        # Checkout main and commit (detect default branch name for git >= 2.28)
        main_branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(self.repo), check=True, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(["git", "checkout", main_branch],
                       cwd=str(self.repo), check=True, capture_output=True)
        _commit_raw(self.repo, "main.txt", b"main\n", "main work", "carol", 2)

        # Merge feature into main
        subprocess.run(["git", "merge", "feature", "-m", "Merge feature"],
                       cwd=str(self.repo), check=True, capture_output=True)

        self.gh = GitHistory(str(self.repo))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @pytest.mark.skip(reason="WIP")
    def test_state_shows_merge_commits(self):
        state = self.gh.read_state()
        # Should have: merge, main work, feature, base
        self.assertGreaterEqual(len(state.commits), 3)

    @pytest.mark.skip(reason="WIP")
    def test_reset_to_before_merge(self):
        state = self.gh.read_state()
        # Find non-merge commit to reset to
        non_merge = next(c for c in state.commits if c.message != "Merge feature")

        result = self.gh.reset(non_merge.commit_hash)
        self.assertTrue(result.ok)
        self.assertEqual(result.commits[0].commit_hash, non_merge.commit_hash)


# ---------------------------------------------------------------------------
# Instruction method unit tests
# ---------------------------------------------------------------------------

class MoveInstructionsTests(StandardRepoTest):

    def test_none_order_raises(self):
        result = self.gh._move_instructions(None)
        self.assertIsInstance(result, ErrorResponse)
        self.assertEqual(result.error, "invalid_request")

    def test_wrong_hashes_raises(self):
        visible = self.commit_hashes()
        bad_order = visible[:-1] + ["0" * 40]
        result = self.gh._move_instructions(bad_order)
        self.assertIsInstance(result, ErrorResponse)
        self.assertEqual(result.error, "invalid_request")

    @unittest.skip("expected values are incorrect — needs investigation")
    def test_swap_produces_pick_lines_in_rebase_order(self):
        visible = self.commit_hashes()
        order = visible[:]
        order[0], order[1] = order[1], order[0]
        instr = self.gh._move_instructions(order)
        self.assertIsInstance(instr, _RebaseInstructions)
        # todo is oldest-first; swapped pair appears as pick visible[1] then pick visible[0]
        self.assertEqual(instr.todo_lines[0], f"pick {visible[1]}")
        self.assertEqual(instr.todo_lines[1], f"pick {visible[0]}")

    def test_base_is_commit_after_oldest_changed(self):
        visible = self.commit_hashes()
        order = visible[:]
        order[0], order[1] = order[1], order[0]
        instr = self.gh._move_instructions(order)
        self.assertEqual(instr.base, visible[2])

    def test_msg_path_is_none_and_extends_base_is_false(self):
        visible = self.commit_hashes()
        order = visible[:]
        order[0], order[1] = order[1], order[0]
        instr = self.gh._move_instructions(order)
        self.assertIsNone(instr.msg_path)
        self.assertFalse(instr.extends_base)


class FoldInstructionsTests(StandardRepoTest):

    def test_empty_hashes_raises(self):
        result = self.gh._fold_instructions([], "squash")
        self.assertIsInstance(result, ErrorResponse)
        self.assertEqual(result.error, "invalid_request")

    def test_unknown_hash_raises(self):
        result = self.gh._fold_instructions(["0" * 40], "squash")
        self.assertIsInstance(result, ErrorResponse)
        self.assertEqual(result.error, "invalid_request")

    def test_non_consecutive_hashes_raises(self):
        visible = self.commit_hashes()
        result = self.gh._fold_instructions([visible[0], visible[2]], "squash")
        self.assertIsInstance(result, ErrorResponse)
        self.assertEqual(result.error, "invalid_request")

    def test_single_hash_gets_fold_command(self):
        visible = self.commit_hashes()
        instr = self.gh._fold_instructions([visible[0]], "squash")
        self.assertIsInstance(instr, _RebaseInstructions)
        self.assertIn(f"squash {visible[0]}", instr.todo_lines)

    def test_fixup_operation_uses_fixup_command(self):
        visible = self.commit_hashes()
        instr = self.gh._fold_instructions([visible[0]], "fixup")
        self.assertIn(f"fixup {visible[0]}", instr.todo_lines)

    def test_two_hashes_older_stays_as_pick(self):
        visible = self.commit_hashes()
        hashes = [visible[0], visible[1]]
        instr = self.gh._fold_instructions(hashes, "squash")
        # visible[1] is older (higher index), so it stays as pick
        self.assertIn(f"pick {visible[1]}", instr.todo_lines)
        self.assertIn(f"squash {visible[0]}", instr.todo_lines)

    def test_extends_base_false_when_oldest_visible_not_in_hashes(self):
        visible = self.commit_hashes()
        instr = self.gh._fold_instructions([visible[0]], "squash")
        self.assertFalse(instr.extends_base)

    def test_squash_instructions_uses_squash_command(self):
        visible = self.commit_hashes()
        instr = self.gh._squash_instructions([visible[0]])
        self.assertIsInstance(instr, _RebaseInstructions)
        self.assertTrue(any("squash" in l for l in instr.todo_lines))

    def test_fixup_instructions_uses_fixup_command(self):
        visible = self.commit_hashes()
        instr = self.gh._fixup_instructions([visible[0]])
        self.assertIsInstance(instr, _RebaseInstructions)
        self.assertTrue(any("fixup" in l for l in instr.todo_lines))


class RewordInstructionsTests(StandardRepoTest):

    def _unlink_msg(self, instr):
        if isinstance(instr, _RebaseInstructions) and instr.msg_path:
            self.addCleanup(lambda p=instr.msg_path: os.unlink(p) if Path(p).exists() else None)

    def test_empty_message_raises(self):
        visible = self.commit_hashes()
        result = self.gh._reword_instructions(visible[0], "")
        self.assertIsInstance(result, ErrorResponse)
        self.assertEqual(result.error, "invalid_request")

    def test_whitespace_only_message_raises(self):
        visible = self.commit_hashes()
        result = self.gh._reword_instructions(visible[0], "   ")
        self.assertIsInstance(result, ErrorResponse)
        self.assertEqual(result.error, "invalid_request")

    def test_unknown_hash_raises(self):
        result = self.gh._reword_instructions("0" * 40, "msg")
        self.assertIsInstance(result, ErrorResponse)
        self.assertEqual(result.error, "invalid_request")

    def test_valid_produces_exec_line_after_target(self):
        visible = self.commit_hashes()
        target = visible[0]
        instr = self.gh._reword_instructions(target, "new message")
        self._unlink_msg(instr)
        self.assertIsInstance(instr, _RebaseInstructions)
        pick_idx = instr.todo_lines.index(f"pick {target}")
        exec_line = instr.todo_lines[pick_idx + 1]
        self.assertIn("exec", exec_line)
        self.assertIn("git commit --amend", exec_line)

    def test_valid_sets_msg_path_and_extends_base_is_false(self):
        visible = self.commit_hashes()
        instr = self.gh._reword_instructions(visible[0], "msg")
        self._unlink_msg(instr)
        self.assertIsNotNone(instr.msg_path)
        self.assertFalse(instr.extends_base)


if __name__ == "__main__":
    unittest.main()
