"""
Unit tests for the git-history backend.

These tests are written before the backend exists. They define the interface
the backend must expose and the behavior every UI command must produce.

The backend is expected to be a single class:

    from git_history import GitHistory
    gh = GitHistory(repo_path)

Every method returns a JSON-serializable dict. On success the dict is the
"state" object (the same shape as GET /api/state). On failure the dict has
"ok": False and an "error" string. Methods never raise on git failures.

Methods exercised by these tests:

    gh.read_state()                     -> state
    gh.stash()                          -> state | error
    gh.stash_pop()                      -> state | error
    gh.rebase(operation, hashes=None,
              order=None,
              new_message=None)         -> state | conflict | error
    gh.rebase_continue()                -> state | conflict | error
    gh.rebase_abort()                   -> state | error
    gh.reset(hash)                      -> state | error
    gh.show(hash)                       -> {ok, info, diff} | error

State dict shape:

    {
      "ok": True,
      "branch": "main",
      "dirty": False,
      "has_stash": False,
      "rebase_in_progress": False,
      "conflict_files": [],
      "commits": [ {hash, short_hash, message, author, date,
                    branches, tags, is_head}, ... ],   # newest first
      "branch_history": [ {hash, label, timestamp}, ... ],     # newest first, deduped
    }

Run with:
    python -m unittest tests.test_backend

Requires only the standard library and a working `git` binary on PATH.
"""
import atexit
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from make_test_repo import (
    COMMITS,
    create_lib_repo,
    init_repo,
    make_commit,
)

# Imported last so the rest of the module loads cleanly even when the backend
# does not yet exist; tests then fail at collection with a clear ImportError.
from conftest import _commit_raw, _build_conflict_repo  # noqa: E402
from git_history import GitHistory  # noqa: E402


# ---------------------------------------------------------------------------
# Test repo fixtures
# ---------------------------------------------------------------------------

_TEMPLATE_REPO = None


def _build_template_repo() -> Path:
    """Build the standard 21-commit test repo once and cache its path."""
    global _TEMPLATE_REPO
    if _TEMPLATE_REPO is not None:
        return _TEMPLATE_REPO

    template_dir = Path(tempfile.mkdtemp(prefix="git-history-template-"))
    lib = template_dir / "lib"
    lib_hash1, lib_hash2 = create_lib_repo(lib)
    sub = {"url": str(lib), "hash1": lib_hash1, "hash2": lib_hash2}
    repo = template_dir / "repo"
    repo.mkdir()
    init_repo(repo)
    for i, (msg, author_key, files, tag) in enumerate(COMMITS):
        make_commit(repo, i, msg, author_key, files, tag, sub=sub)
    atexit.register(lambda: shutil.rmtree(template_dir, ignore_errors=True))
    _TEMPLATE_REPO = repo
    return repo


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
        return [c["message"] for c in s["commits"]]

    def hashes(self, state=None):
        s = state if state is not None else self.gh.read_state()
        return [c["hash"] for c in s["commits"]]

    def make_dirty(self, path="README.md", content=b"# changed by test\n"):
        (self.repo / path).write_bytes(content)


# ---------------------------------------------------------------------------
# State / refresh
# ---------------------------------------------------------------------------

class StateTests(StandardRepoTest):

    def test_state_has_expected_top_level_fields(self):
        state = self.gh.read_state()
        self.assertTrue(state["ok"])
        self.assertEqual(state["branch"], "main")
        self.assertFalse(state["dirty"])
        self.assertFalse(state["has_stash"])
        self.assertFalse(state["rebase_in_progress"])
        self.assertEqual(state["conflict_files"], [])
        self.assertEqual(len(state["commits"]), len(COMMITS))
        self.assertGreater(len(state["branch_history"]), 0)

    def test_commits_are_newest_first(self):
        msgs = self.messages()
        self.assertEqual(msgs[0],  "Add CI workflow")
        self.assertEqual(msgs[-1], "Initial commit")

    def test_head_commit_metadata(self):
        head = self.gh.read_state()["commits"][0]
        self.assertEqual(head["message"], "Add CI workflow")
        self.assertEqual(head["author"],  "Bob Brown")
        self.assertTrue(head["is_head"])
        self.assertIn("main", head["branches"])
        self.assertEqual(head["tags"], [])

    def test_tags_appear_on_correct_commits(self):
        by_msg = {c["message"]: c for c in self.gh.read_state()["commits"]}
        self.assertIn("v0.1.0", by_msg["Add HTTP server module"]["tags"])
        self.assertIn("v0.2.0", by_msg["Add user model"]["tags"])
        self.assertIn("v1.0.0", by_msg["Add integration tests"]["tags"])

    def test_short_hash_is_seven_char_prefix(self):
        for c in self.gh.read_state()["commits"]:
            self.assertEqual(len(c["short_hash"]), 7)
            self.assertTrue(c["hash"].startswith(c["short_hash"]))

    def test_branch_history_is_deduped_by_hash(self):
        branch_history = self.gh.read_state()["branch_history"]
        hashes = [e["hash"] for e in branch_history]
        self.assertEqual(len(hashes), len(set(hashes)))

    def test_branch_history_entries_have_label_and_timestamp(self):
        for entry in self.gh.read_state()["branch_history"]:
            self.assertIn("hash", entry)
            self.assertIn("label", entry)
            self.assertIn("timestamp", entry)

    def test_state_reports_dirty_tree(self):
        self.make_dirty()
        state = self.gh.read_state()
        self.assertTrue(state["dirty"])


# ---------------------------------------------------------------------------
# Stash / stash pop
# ---------------------------------------------------------------------------

class StashTests(StandardRepoTest):

    def test_stash_when_clean_returns_nothing_to_stash(self):
        result = self.gh.stash()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "nothing_to_stash")

    def test_stash_when_dirty_clears_dirty_and_records_stash(self):
        self.make_dirty()
        before = self.gh.read_state()
        self.assertTrue(before["dirty"])
        self.assertFalse(before["has_stash"])

        result = self.gh.stash()
        self.assertTrue(result["ok"])
        self.assertFalse(result["dirty"])
        self.assertTrue(result["has_stash"])

    def test_stash_pop_with_no_stash_returns_no_stash(self):
        result = self.gh.stash_pop()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "no_stash")

    def test_stash_pop_when_dirty_is_refused(self):
        self.make_dirty()
        self.gh.stash()
        # Dirty up the tree again with a different file.
        self.make_dirty(path="LICENSE", content=b"different\n")

        result = self.gh.stash_pop()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dirty_tree")

    def test_stash_pop_clean_restores_dirty_state(self):
        self.make_dirty()
        self.gh.stash()

        result = self.gh.stash_pop()
        self.assertTrue(result["ok"])
        self.assertTrue(result["dirty"])
        self.assertFalse(result["has_stash"])


# ---------------------------------------------------------------------------
# Rebase: move
# ---------------------------------------------------------------------------

class MoveTests(StandardRepoTest):

    def test_swap_two_newest_commits(self):
        state = self.gh.read_state()
        msgs_before = self.messages(state)
        order = self.hashes(state)
        order[0], order[1] = order[1], order[0]

        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"])

        msgs_after = self.messages(result)
        expected = msgs_before[:]
        expected[0], expected[1] = expected[1], expected[0]
        self.assertEqual(msgs_after, expected)

    def test_move_distant_commit(self):
        state = self.gh.read_state()
        msgs_before = self.messages(state)
        order = self.hashes(state)
        moved_hash = order.pop(0)
        order.insert(5, moved_hash)

        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"])

        msgs_after = self.messages(result)
        self.assertEqual(msgs_after[5], msgs_before[0])
        # Same set of messages, just reordered.
        self.assertEqual(sorted(msgs_after), sorted(msgs_before))

    def test_move_with_unchanged_order_is_a_noop(self):
        state = self.gh.read_state()
        order = self.hashes(state)

        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"])
        # Hashes unchanged because nothing was rewritten.
        self.assertEqual(self.hashes(result), order)


# ---------------------------------------------------------------------------
# Rebase: squash
# ---------------------------------------------------------------------------

class SquashTests(StandardRepoTest):

    def test_squash_two_adjacent_commits(self):
        state = self.gh.read_state()
        msgs_before = self.messages(state)
        hashes = [state["commits"][0]["hash"], state["commits"][1]["hash"]]

        result = self.gh.rebase("squash", hashes=hashes)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

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
        h0 = state["commits"][0]["hash"]
        h2 = state["commits"][2]["hash"]

        result = self.gh.rebase("squash", hashes=[h0, h2])

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_request")
        self.assertFalse(self.gh.read_state()["rebase_in_progress"])

    def test_fixup_non_adjacent_commits_returns_invalid_request(self):
        state = self.gh.read_state()
        h0 = state["commits"][0]["hash"]
        h2 = state["commits"][2]["hash"]

        result = self.gh.rebase("fixup", hashes=[h0, h2])

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_request")
        self.assertFalse(self.gh.read_state()["rebase_in_progress"])

    def test_squash_non_adjacent_does_not_modify_commit_history(self):
        hashes_before = self.hashes()

        self.gh.rebase("squash", hashes=[hashes_before[0], hashes_before[3]])

        self.assertEqual(self.hashes(), hashes_before)

    def test_squash_three_adjacent_commits_is_valid(self):
        state = self.gh.read_state()
        hashes = [state["commits"][i]["hash"] for i in range(3)]

        result = self.gh.rebase("squash", hashes=hashes)

        self.assertTrue(result["ok"])
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 2)


# ---------------------------------------------------------------------------
# Rebase: fixup
# ---------------------------------------------------------------------------

class FixupTests(StandardRepoTest):

    def test_fixup_a_middle_commit(self):
        state = self.gh.read_state()
        msgs_before = self.messages(state)
        target = state["commits"][3]
        h = target["hash"]
        target_msg = target["message"]

        result = self.gh.rebase("fixup", hashes=[h])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

        # The fixup'd commit's message is gone (folded into its parent).
        self.assertNotIn(target_msg, self.messages(result))
        # All other messages survive.
        for m in msgs_before:
            if m != target_msg:
                self.assertIn(m, self.messages(result))

    def test_fixup_root_commit_is_refused(self):
        # Oldest commit in the standard test repo IS the root commit.
        state = self.gh.read_state()
        h = state["commits"][-1]["hash"]

        result = self.gh.rebase("fixup", hashes=[h])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_request")


# ---------------------------------------------------------------------------
# Rebase: reword
# ---------------------------------------------------------------------------

class RewordTests(StandardRepoTest):

    def test_reword_top_commit(self):
        state = self.gh.read_state()
        h = state["commits"][0]["hash"]

        result = self.gh.rebase("reword", hashes=[h],
                                new_message="A brand new message")
        self.assertTrue(result["ok"])
        self.assertEqual(result["commits"][0]["message"], "A brand new message")

    def test_reword_middle_commit(self):
        state = self.gh.read_state()
        h = state["commits"][5]["hash"]

        result = self.gh.rebase("reword", hashes=[h],
                                new_message="Reworded middle")
        self.assertTrue(result["ok"])
        self.assertEqual(result["commits"][5]["message"], "Reworded middle")
        # Sibling commits intact.
        self.assertEqual(result["commits"][0]["message"],
                         state["commits"][0]["message"])
        self.assertEqual(result["commits"][-1]["message"],
                         state["commits"][-1]["message"])


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class ResetTests(StandardRepoTest):

    def test_reset_to_older_commit_moves_head(self):
        state = self.gh.read_state()
        target = state["commits"][5]

        result = self.gh.reset(target["hash"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["commits"][0]["hash"], target["hash"])
        self.assertEqual(result["commits"][0]["message"], target["message"])

    def test_reset_when_dirty_is_refused(self):
        self.make_dirty()
        h = self.gh.read_state()["commits"][5]["hash"]

        result = self.gh.reset(h)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dirty_tree")


# ---------------------------------------------------------------------------
# Show
# ---------------------------------------------------------------------------

class ShowTests(StandardRepoTest):

    def test_show_returns_commit_and_diff(self):
        head = self.gh.read_state()["commits"][0]
        result = self.gh.show(head["hash"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["commit"]["short_hash"], head["short_hash"])
        self.assertEqual(result["commit"]["message"], head["message"])
        self.assertIn("diff --git", result["diff"])

    def test_show_unknown_hash_returns_error(self):
        result = self.gh.show("0" * 40)
        self.assertFalse(result["ok"])


# ---------------------------------------------------------------------------
# Dirty-tree policy
# ---------------------------------------------------------------------------

class DirtyTreeTests(StandardRepoTest):

    def test_rebase_move_refused_when_dirty(self):
        self.make_dirty()
        order = self.hashes()
        order[0], order[1] = order[1], order[0]

        result = self.gh.rebase("move", order=order)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dirty_tree")

    def test_rebase_squash_refused_when_dirty(self):
        self.make_dirty()
        state = self.gh.read_state()
        hashes = [state["commits"][0]["hash"], state["commits"][1]["hash"]]

        result = self.gh.rebase("squash", hashes=hashes)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dirty_tree")

    def test_rebase_fixup_refused_when_dirty(self):
        self.make_dirty()
        h = self.gh.read_state()["commits"][3]["hash"]

        result = self.gh.rebase("fixup", hashes=[h])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dirty_tree")

    def test_rebase_reword_refused_when_dirty(self):
        self.make_dirty()
        h = self.gh.read_state()["commits"][0]["hash"]

        result = self.gh.rebase("reword", hashes=[h], new_message="x")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dirty_tree")


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
        order = [c["hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]
        return self.gh.rebase("move", order=order)

    def test_move_produces_conflict(self):
        result = self._swap_order()
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("conflict"))
        self.assertIn("f.txt", result["conflict_files"])
        self.assertTrue(result["rebase_in_progress"])

    def test_abort_clears_rebase_state(self):
        self._swap_order()

        result = self.gh.rebase_abort()
        self.assertTrue(result["ok"])
        self.assertFalse(result["rebase_in_progress"])
        self.assertEqual(result["conflict_files"], [])

    def test_continue_after_resolving_conflict(self):
        self._swap_order()

        # Resolve to initial content so version_A's patch applies cleanly next.
        (self.repo / "f.txt").write_bytes(b"line1\nline2\nline3\n")
        subprocess.run(["git", "add", "f.txt"], cwd=str(self.repo),
                       check=True, capture_output=True)

        result = self.gh.rebase_continue()
        self.assertTrue(result["ok"])
        self.assertFalse(result["rebase_in_progress"])

    def test_continue_with_unresolved_conflict_stays_in_conflict(self):
        self._swap_order()

        result = self.gh.rebase_continue()
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("conflict"))
        self.assertTrue(result["rebase_in_progress"])


# ---------------------------------------------------------------------------
# _filter_rebase_groups unit tests
# ---------------------------------------------------------------------------

class RebaseGroupFilterTests(unittest.TestCase):

    def _e(self, label):
        return {"hash": label[:8].replace(" ", "_"), "label": label, "timestamp": "2026-01-01T00:00:00"}

    def _labels(self, entries):
        return [e["label"] for e in entries]

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
        self.assertEqual(result[0]["label"], "rebase")
        self.assertEqual(result[1]["label"], "commit: some commit")
        self.assertEqual(result[2]["label"], "rebase")
        self.assertEqual(result[3]["label"], "commit: initial")

    def test_empty_input_returns_empty(self):
        self.assertEqual(GitHistory._filter_rebase_groups([]), [])


# ---------------------------------------------------------------------------
# Branch History: rebase group collapsing (integration)
# ---------------------------------------------------------------------------

class BranchHistoryRebaseCollapsingTests(StandardRepoTest):

    def test_branch_history_collapses_rebase_group_to_single_finish_entry(self):
        state = self.gh.read_state()
        h = state["commits"][0]["hash"]
        commit_count_before = len(state["commits"])

        result = self.gh.rebase("reword", hashes=[h], new_message="Rewarded top")
        self.assertTrue(result["ok"])

        rebase_entries = [e for e in result["branch_history"] if e["label"] == "rebase"]
        self.assertEqual(len(rebase_entries), 1)

        # Unchanged commits (fast-forwarded during rebase) must still appear in
        # the branch history under their original "commit:" labels.
        commit_labels = [e["label"] for e in result["branch_history"] if e["label"].startswith("commit")]
        self.assertGreaterEqual(len(commit_labels), commit_count_before)


class LoggingTests(StandardRepoTest):

    def setUp(self):
        super().setUp()
        import git_history as _gh_module
        self._log_path_orig = _gh_module._LOG_PATH
        self._log_file = self.tmpdir / "test.log"
        _gh_module._LOG_PATH = self._log_file

    def tearDown(self):
        import git_history as _gh_module
        _gh_module._LOG_PATH = self._log_path_orig
        super().tearDown()

    def _log_lines(self):
        if not self._log_file.exists():
            return []
        return [l for l in self._log_file.read_text().splitlines() if l]

    def test_rebase_appends_log_entry(self):
        h = self.hashes()[0]
        self.gh.rebase("reword", hashes=[h], new_message="New message")
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        ts, branch, hash_ = lines[0].split()
        self.assertEqual(branch, "main")
        self.assertEqual(len(hash_), 40)

    def test_reset_appends_log_entry(self):
        hashes = self.hashes()
        self.gh.reset(hashes[1])
        lines = self._log_lines()
        self.assertEqual(len(lines), 1)
        ts, branch, hash_ = lines[0].split()
        self.assertEqual(hash_, hashes[1])

    def test_multiple_operations_append_multiple_lines(self):
        hashes = self.hashes()
        self.gh.reset(hashes[1])
        self.gh.reset(hashes[0])
        self.assertEqual(len(self._log_lines()), 2)

    def test_failed_operation_does_not_append_log_entry(self):
        self.gh.reset("0" * 40)
        self.assertEqual(self._log_lines(), [])

    def test_log_entry_format(self):
        h = self.hashes()[0]
        self.gh.rebase("reword", hashes=[h], new_message="Test")
        line = self._log_lines()[0]
        parts = line.split()
        self.assertEqual(len(parts), 3)
        # timestamp is ISO 8601
        import datetime
        datetime.datetime.fromisoformat(parts[0])


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
        self.assertIn("branches", state)
        self.assertIn("main", state["branches"])

    def test_branches_list_includes_all_local_branches(self):
        state = self.gh.read_state()
        self.assertIn("feature", state["branches"])

    def test_switch_to_existing_branch(self):
        result = self.gh.switch_branch("feature")
        self.assertTrue(result["ok"])
        self.assertEqual(result["branch"], "feature")

    def test_switch_returns_full_state(self):
        result = self.gh.switch_branch("feature")
        self.assertIn("commits", result)
        self.assertIn("branch_history", result)
        self.assertIn("branches", result)

    def test_switch_to_unknown_branch_returns_error(self):
        result = self.gh.switch_branch("nonexistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_branch")

    def test_switch_empty_branch_returns_error(self):
        result = self.gh.switch_branch("")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_branch")

    def test_switch_when_dirty_is_refused(self):
        self.make_dirty()
        result = self.gh.switch_branch("feature")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dirty_tree")

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
        order = [c["hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]
        gh.rebase("move", order=order)
        result = gh.switch_branch("other")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rebase_in_progress")

    def test_stash_during_rebase_is_refused(self):
        conflict_dir = self.tmpdir / "conflict"
        conflict_dir.mkdir()
        conflict_repo = _build_conflict_repo(conflict_dir)
        gh = GitHistory(str(conflict_repo))
        state = gh.read_state()
        order = [c["hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]
        gh.rebase("move", order=order)
        result = gh.stash()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rebase_in_progress")

    def test_stash_pop_during_rebase_is_refused(self):
        conflict_dir = self.tmpdir / "conflict"
        conflict_dir.mkdir()
        conflict_repo = _build_conflict_repo(conflict_dir)
        gh = GitHistory(str(conflict_repo))
        state = gh.read_state()
        order = [c["hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]
        gh.rebase("move", order=order)
        result = gh.stash_pop()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rebase_in_progress")

    def test_reset_during_rebase_is_refused(self):
        conflict_dir = self.tmpdir / "conflict"
        conflict_dir.mkdir()
        conflict_repo = _build_conflict_repo(conflict_dir)
        gh = GitHistory(str(conflict_repo))
        state = gh.read_state()
        order = [c["hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]
        gh.rebase("move", order=order)
        result = gh.reset(state["commits"][0]["hash"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rebase_in_progress")


if __name__ == "__main__":
    unittest.main()
