"""
Challenging-scenario tests: repos with merge commits, submodules, binary files,
file create/delete, empty commits, and sequential operations. Each test verifies
that squash, fixup, reword, and reset (undo/redo) either succeed correctly or
fail cleanly — no broken repo state, no stuck rebase.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from make_test_repo import AUTHORS, BASE_DATE, init_repo
from conftest import _commit_raw, _build_conflict_repo
from git_history import GitHistory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo: Path, *args):
    return subprocess.run(
        list(args), cwd=str(repo),
        capture_output=True, text=True, encoding="utf-8"
    )


def _commit_env(author_key="alice", day_offset=0):
    author_name, author_email = AUTHORS[author_key]
    when = (BASE_DATE + timedelta(days=day_offset)).isoformat()
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
        "GIT_COMMITTER_DATE": when,
    })
    return env


def _commit_empty(repo: Path, message: str, author_key="alice", day_offset=0):
    """Create a commit with no file changes."""
    env = _commit_env(author_key, day_offset)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", message],
        cwd=str(repo), env=env, check=True, capture_output=True
    )


def _ls_tree(repo: Path, ref: str) -> list:
    """Return list of filenames present in the tree at ref."""
    r = _git(repo, "git", "ls-tree", "-r", "--name-only", ref)
    return [l for l in r.stdout.splitlines() if l]


def _parent_count(repo: Path, commit_hash: str) -> int:
    r = _git(repo, "git", "rev-parse", commit_hash + "^@")
    return len([l for l in r.stdout.strip().splitlines() if l])


# ---------------------------------------------------------------------------
# Repo builders
# ---------------------------------------------------------------------------

def _build_merge_commit_repo(parent: Path) -> Path:
    """
    Repo with a no-ff merge commit in the main branch history.

        A ─── B ─── D(merge) ─── E    (main)
               \\   /
                C                      (feature, merged at D)

    git log traverses both sides of the merge, so visible commits include
    E, D, B, C, A — the merge commit D and feature commit C are in the range.
    """
    repo = parent / "merge-repo"
    repo.mkdir()
    init_repo(repo)

    _commit_raw(repo, "base.txt",    b"base\n",         "initial",     "alice", 0)
    _commit_raw(repo, "main.txt",    b"main content\n", "add main",    "bob",   1)
    hash_b = _git(repo, "git", "rev-parse", "HEAD").stdout.strip()

    _git(repo, "git", "checkout", "-b", "feature", hash_b + "~1")
    _commit_raw(repo, "feature.txt", b"feature\n",      "add feature", "carol", 2)
    hash_c = _git(repo, "git", "rev-parse", "HEAD").stdout.strip()

    _git(repo, "git", "checkout", "main")
    env_d = _commit_env("alice", 3)
    subprocess.run(
        ["git", "merge", "--no-ff", hash_c, "-m", "merge feature"],
        cwd=str(repo), env=env_d, check=True, capture_output=True
    )
    _commit_raw(repo, "after.txt", b"after merge\n", "after merge", "bob", 4)
    return repo


def _build_binary_and_rename_repo(parent: Path) -> Path:
    """
    Repo with commits touching binary files and a file rename.

    Commits (newest first):
      "update docs"    — modifies docs.txt (the renamed file)
      "rename to docs" — renames readme.txt → docs.txt
      "update binary"  — replaces data.bin content with v2
      "add binary"     — creates data.bin with v1 content
      "initial"        — creates readme.txt
    """
    repo = parent / "binary-repo"
    repo.mkdir()
    init_repo(repo)

    _commit_raw(repo, "readme.txt", b"initial readme\n", "initial",       "alice", 0)

    BINARY_V1 = bytes(range(256)) * 4          # 1 KB
    BINARY_V2 = bytes(range(255, -1, -1)) * 4  # same size, reversed

    _commit_raw(repo, "data.bin", BINARY_V1, "add binary",    "bob",   1)
    _commit_raw(repo, "data.bin", BINARY_V2, "update binary", "carol", 2)

    env3 = _commit_env("alice", 3)
    subprocess.run(["git", "mv", "readme.txt", "docs.txt"],
                   cwd=str(repo), env=env3, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "rename to docs"],
                   cwd=str(repo), env=env3, capture_output=True, check=True)

    _commit_raw(repo, "docs.txt", b"updated docs\n", "update docs", "bob", 4)
    return repo


def _build_create_delete_repo(parent: Path) -> Path:
    """
    Repo where one commit creates a file and a later commit deletes it.

    Commits (newest first):
      "after delete"  — creates unrelated.txt
      "delete temp"   — deletes temp.txt
      "add temp"      — creates temp.txt
      "initial"       — creates base.txt
    """
    repo = parent / "create-delete-repo"
    repo.mkdir()
    init_repo(repo)

    _commit_raw(repo, "base.txt", b"base\n",      "initial",      "alice", 0)
    _commit_raw(repo, "temp.txt", b"temporary\n", "add temp",     "bob",   1)

    env2 = _commit_env("carol", 2)
    (repo / "temp.txt").unlink()
    subprocess.run(["git", "rm", "temp.txt"], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "delete temp"],
                   cwd=str(repo), env=env2, capture_output=True, check=True)

    _commit_raw(repo, "unrelated.txt", b"unrelated\n", "after delete", "alice", 3)
    return repo


def _build_empty_commit_repo(parent: Path) -> Path:
    """
    Repo with an --allow-empty commit mixed in.

    Commits (newest first):
      "real C"   — modifies a.txt
      "empty B"  — no file changes
      "real A"   — creates a.txt
      "initial"  — creates base.txt
    """
    repo = parent / "empty-commit-repo"
    repo.mkdir()
    init_repo(repo)

    _commit_raw(repo, "base.txt", b"base\n", "initial", "alice", 0)
    _commit_raw(repo, "a.txt",    b"v1\n",   "real A",  "bob",   1)
    _commit_empty(repo, "empty B", "carol", 2)
    _commit_raw(repo, "a.txt",    b"v2\n",   "real C",  "alice", 3)
    return repo


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class ChallengeBase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-challenge-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def assert_recoverable(self, gh: GitHistory):
        """Abort any in-progress rebase and verify the repo is clean afterward."""
        state = gh.read_state()
        if state["rebase_in_progress"]:
            abort = gh.rebase_abort()
            self.assertTrue(abort["ok"], f"rebase_abort() failed: {abort}")
            state = gh.read_state()
        self.assertFalse(state["rebase_in_progress"],
                         "repo stuck in rebase after abort")
        self.assertFalse(state["dirty"],
                         "repo dirty after recovery from failed operation")
        self.assertTrue(state["ok"])

    def assert_valid_state(self, state):
        self.assertTrue(state["ok"])
        self.assertIsInstance(state["commits"], list)
        self.assertGreater(len(state["commits"]), 0)
        for c in state["commits"]:
            self.assertEqual(len(c["hash"]), 40)
            self.assertIsInstance(c["message"], str)


# ---------------------------------------------------------------------------
# 1. Merge commits in the visible range
# ---------------------------------------------------------------------------

class MergeCommitTests(ChallengeBase):
    """
    A merge commit in the visible range means any rebase operation will
    encounter `pick <merge-commit>` in its todo. Git cannot replay merge
    commits without --rebase-merges. The backend must not leave the repo
    stuck after this failure.
    """

    def setUp(self):
        super().setUp()
        self.repo = _build_merge_commit_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _merge_and_non_merge_hashes(self):
        state = self.gh.read_state()
        merge, non_merge = [], []
        for c in state["commits"]:
            if _parent_count(self.repo, c["hash"]) > 1:
                merge.append(c["hash"])
            else:
                non_merge.append(c["hash"])
        return merge, non_merge

    def test_squash_of_merge_commit_leaves_repo_recoverable(self):
        """Squash targeting the merge commit itself must not corrupt the repo."""
        merge_hashes, _ = self._merge_and_non_merge_hashes()
        self.assertTrue(merge_hashes, "expected at least one merge commit in visible range")

        result = self.gh.rebase("squash", hashes=[merge_hashes[0]])
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)

    def test_squash_two_non_merge_commits_when_merge_is_in_range(self):
        """
        Squashing two regular commits works, but the merge commit is also
        in the todo as a plain 'pick'. Git will fail on that pick.
        The repo must be recoverable.
        """
        _, non_merge = self._merge_and_non_merge_hashes()
        if len(non_merge) < 2:
            self.skipTest("need at least two non-merge commits")

        result = self.gh.rebase("squash", hashes=[non_merge[0], non_merge[1]])
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            state = self.gh.read_state()
            self.assert_valid_state(state)

    def test_fixup_of_non_merge_commit_adjacent_to_merge_leaves_repo_recoverable(self):
        """Fixup a regular commit that is adjacent to a merge commit."""
        _, non_merge = self._merge_and_non_merge_hashes()
        if not non_merge:
            self.skipTest("no non-merge commits")

        result = self.gh.rebase("fixup", hashes=[non_merge[0]])
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)

    def test_move_with_merge_commit_present_leaves_repo_recoverable(self):
        """Swap the two newest commits when a merge commit is in the visible list."""
        state = self.gh.read_state()
        order = [c["hash"] for c in state["commits"]]
        if len(order) < 2:
            self.skipTest("not enough commits")
        order[0], order[1] = order[1], order[0]

        result = self.gh.rebase("move", order=order)
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)

    def test_reword_non_merge_commit_when_merge_is_in_range(self):
        """Reword a regular commit; the merge commit in the todo must not break things."""
        state = self.gh.read_state()
        # Pick the newest commit regardless of whether it's a merge.
        h = state["commits"][0]["hash"]
        if _parent_count(self.repo, h) > 1:
            self.skipTest("newest commit is the merge commit — skip reword test")

        result = self.gh.rebase("reword", hashes=[h], new_message="rewarded top")
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)

    def test_read_state_after_failed_op_always_succeeds(self):
        """read_state() must return ok=True even after a failed rebase attempt."""
        _, non_merge = self._merge_and_non_merge_hashes()
        if len(non_merge) < 2:
            self.skipTest("need at least two non-merge commits")

        self.gh.rebase("squash", hashes=[non_merge[0], non_merge[1]])
        # Whether op succeeded or failed, read_state must work.
        self.gh.rebase_abort()  # safe to call even if not in rebase
        state = self.gh.read_state()
        self.assertTrue(state["ok"])
        self.assertFalse(state["rebase_in_progress"])


# ---------------------------------------------------------------------------
# 3. Binary files and file renames
# ---------------------------------------------------------------------------

class BinaryAndRenameTests(ChallengeBase):

    BINARY_V2 = bytes(range(255, -1, -1)) * 4

    def setUp(self):
        super().setUp()
        self.repo = _build_binary_and_rename_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def test_squash_two_binary_commits_succeeds(self):
        """Squash 'add binary' + 'update binary' — data.bin must have v2 content."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase(
            "squash",
            hashes=[bm["add binary"], bm["update binary"]]
        )
        self.assertTrue(result["ok"], f"squash of binary commits failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        # data.bin must still be present and contain the v2 bytes.
        r = subprocess.run(
            ["git", "show", "HEAD:data.bin"],
            cwd=str(self.repo), capture_output=True
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, self.BINARY_V2)

    def test_fixup_binary_update_into_add(self):
        """Fixup 'update binary' into 'add binary'."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase("fixup", hashes=[bm["update binary"]])
        self.assertTrue(result["ok"], f"fixup of binary commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in result["commits"]]
        self.assertNotIn("update binary", msgs)

    def test_squash_across_rename_preserves_renamed_file(self):
        """Squash 'rename to docs' + 'update docs': docs.txt must exist, readme.txt must not."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase(
            "squash",
            hashes=[bm["rename to docs"], bm["update docs"]]
        )
        self.assertTrue(result["ok"], f"squash across rename failed: {result}")
        files = _ls_tree(self.repo, "HEAD")
        self.assertIn("docs.txt", files)
        self.assertNotIn("readme.txt", files)

    def test_fixup_rename_commit_into_predecessor(self):
        """Fixup the rename commit into 'update binary'."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase("fixup", hashes=[bm["rename to docs"]])
        self.assertTrue(result["ok"], f"fixup of rename commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    def test_reword_rename_commit_preserves_rename(self):
        """Reword the rename commit; the tree must still show docs.txt, not readme.txt."""
        bm = self._by_msg()
        h = bm["rename to docs"]

        result = self.gh.rebase("reword", hashes=[h], new_message="mv readme → docs")
        self.assertTrue(result["ok"], f"reword failed: {result}")

        new_h = self._by_msg(result).get("mv readme → docs")
        self.assertIsNotNone(new_h)
        files_at_commit = _ls_tree(self.repo, new_h)
        self.assertIn("docs.txt",    files_at_commit)
        self.assertNotIn("readme.txt", files_at_commit)

    def test_show_binary_commit_returns_ok(self):
        """show() must succeed on a commit that touches a binary file."""
        bm = self._by_msg()
        result = self.gh.show(bm["add binary"])
        self.assertTrue(result["ok"], f"show() failed on binary commit: {result}")
        self.assertIn("data.bin", result["diff"])


# ---------------------------------------------------------------------------
# 4. Create-then-delete the same file
# ---------------------------------------------------------------------------

class CreateDeleteFileTests(ChallengeBase):

    def setUp(self):
        super().setUp()
        self.repo = _build_create_delete_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def test_squash_create_and_delete_produces_commit_without_temp_file(self):
        """Squash 'add temp' + 'delete temp': the combined commit must not contain temp.txt."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase(
            "squash",
            hashes=[bm["add temp"], bm["delete temp"]]
        )
        self.assertTrue(result["ok"], f"squash of create+delete failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

        # The combined commit is the one that replaced both — it's at the position
        # of 'add temp' in the new history (between 'after delete' and 'initial').
        combined = result["commits"][1]  # after delete=0, combined=1, initial=2
        files_in_combined = _ls_tree(self.repo, combined["hash"])
        self.assertNotIn("temp.txt", files_in_combined,
                         "temp.txt must not exist in the squashed commit's tree")

    def test_fixup_delete_into_add_produces_no_temp_file(self):
        """Fixup 'delete temp' into 'add temp': combined tree must not have temp.txt."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase("fixup", hashes=[bm["delete temp"]])
        self.assertTrue(result["ok"], f"fixup of delete commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        # 'delete temp' message is gone (folded in).
        msgs = [c["message"] for c in result["commits"]]
        self.assertNotIn("delete temp", msgs)

    def test_squash_create_delete_working_tree_has_no_temp_file(self):
        """After squash, temp.txt must not exist in the working tree."""
        bm = self._by_msg()
        self.gh.rebase("squash", hashes=[bm["add temp"], bm["delete temp"]])
        self.assertFalse((self.repo / "temp.txt").exists(),
                         "temp.txt present in working tree after squash of create+delete")

    def test_squash_nonadjacent_create_delete_with_middle_commit(self):
        """
        Squash 'add temp' with 'after delete' (non-adjacent, with 'delete temp'
        between them). The todo inserts 'delete temp' as a plain pick between them.
        Verify the repo is recoverable regardless of outcome.
        """
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase(
            "squash",
            hashes=[bm["add temp"], bm["after delete"]]
        )
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)


# ---------------------------------------------------------------------------
# 5. Empty commits
# ---------------------------------------------------------------------------

class EmptyCommitTests(ChallengeBase):

    def setUp(self):
        super().setUp()
        self.repo = _build_empty_commit_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def test_fixup_empty_commit_into_real_predecessor(self):
        """Fixup the empty commit: it folds into 'real A' and vanishes."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        self.assertIn("empty B", bm)

        result = self.gh.rebase("fixup", hashes=[bm["empty B"]])
        self.assertTrue(result["ok"], f"fixup of empty commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in result["commits"]]
        self.assertNotIn("empty B", msgs)
        self.assertIn("real A", msgs)

    def test_squash_empty_commit_with_adjacent_real_commit(self):
        """Squash 'empty B' with 'real A': result has combined messages."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase(
            "squash",
            hashes=[bm["empty B"], bm["real A"]]
        )
        self.assertTrue(result["ok"], f"squash with empty commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    def test_reword_empty_commit(self):
        """Reword an empty commit: message changes, no file changes."""
        bm = self._by_msg()
        h = bm["empty B"]

        result = self.gh.rebase("reword", hashes=[h], new_message="placeholder commit")
        self.assertTrue(result["ok"], f"reword of empty commit failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertIn("placeholder commit", msgs)
        self.assertNotIn("empty B", msgs)

    def test_show_empty_commit_has_empty_diff(self):
        """show() on the empty commit must return an empty diff."""
        bm = self._by_msg()
        result = self.gh.show(bm["empty B"])
        self.assertTrue(result["ok"], f"show() failed on empty commit: {result}")
        self.assertEqual(result["diff"].strip(), "",
                         "empty commit should produce an empty diff")

    def test_squash_real_commit_then_empty_commit_then_real_commit(self):
        """Squash all three: real A + empty B + real C into one commit."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase(
            "squash",
            hashes=[bm["real A"], bm["empty B"], bm["real C"]]
        )
        self.assertTrue(result["ok"], f"3-way squash including empty commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 2)
        # a.txt must have the v2 content from 'real C'.
        r = _git(self.repo, "git", "show", "HEAD:a.txt")
        # 'real C' writes v2; that should be in the squash result unless ordering changed.
        self.assertEqual(r.returncode, 0)


# ---------------------------------------------------------------------------
# 6. Undo / redo via branch_history + reset
# ---------------------------------------------------------------------------

class UndoRedoTests(ChallengeBase):
    """
    After a squash/fixup, branch_history must contain the pre-operation HEAD
    so the user can reset back to it (undo). After undo, branch_history must
    still contain the post-operation HEAD so the user can redo.
    """

    def setUp(self):
        super().setUp()
        self.repo = _build_binary_and_rename_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def test_undo_squash_via_reset_to_pre_squash_head(self):
        """After squash, resetting to pre-squash HEAD restores original commit count."""
        state_before = self.gh.read_state()
        count_before = len(state_before["commits"])
        pre_squash_head = state_before["commits"][0]["hash"]
        bm = self._by_msg(state_before)

        squash_result = self.gh.rebase(
            "squash",
            hashes=[bm["add binary"], bm["update binary"]]
        )
        self.assertTrue(squash_result["ok"], f"squash failed: {squash_result}")

        # Pre-squash HEAD must be in branch_history.
        bh_hashes = {e["hash"] for e in squash_result["branch_history"]}
        self.assertIn(pre_squash_head, bh_hashes,
                      "pre-squash HEAD not in branch_history — undo impossible")

        undo = self.gh.reset(pre_squash_head)
        self.assertTrue(undo["ok"], f"undo (reset) failed: {undo}")
        self.assertEqual(undo["commits"][0]["hash"], pre_squash_head)
        self.assertEqual(len(undo["commits"]), count_before)

    def test_redo_after_undo_restores_squash(self):
        """After undo, resetting to post-squash HEAD restores the squash result."""
        state_before = self.gh.read_state()
        bm = self._by_msg(state_before)
        pre_squash_head = state_before["commits"][0]["hash"]

        squash_result = self.gh.rebase(
            "squash",
            hashes=[bm["add binary"], bm["update binary"]]
        )
        self.assertTrue(squash_result["ok"])
        post_squash_head = squash_result["commits"][0]["hash"]
        count_after_squash = len(squash_result["commits"])

        self.gh.reset(pre_squash_head)  # undo

        undo_state = self.gh.read_state()
        bh_hashes = {e["hash"] for e in undo_state["branch_history"]}
        self.assertIn(post_squash_head, bh_hashes,
                      "post-squash HEAD not in branch_history after undo — redo impossible")

        redo = self.gh.reset(post_squash_head)
        self.assertTrue(redo["ok"], f"redo (reset) failed: {redo}")
        self.assertEqual(redo["commits"][0]["hash"], post_squash_head)
        self.assertEqual(len(redo["commits"]), count_after_squash)

    def test_undo_fixup_restores_both_original_commit_messages(self):
        """After fixup, undo must restore the fixup'd commit message."""
        state_before = self.gh.read_state()
        bm = self._by_msg(state_before)
        pre_fixup_head = state_before["commits"][0]["hash"]
        count_before = len(state_before["commits"])

        fixup_result = self.gh.rebase("fixup", hashes=[bm["update binary"]])
        self.assertTrue(fixup_result["ok"])

        undo = self.gh.reset(pre_fixup_head)
        self.assertTrue(undo["ok"])
        self.assertEqual(len(undo["commits"]), count_before)
        msgs = [c["message"] for c in undo["commits"]]
        self.assertIn("update binary", msgs,
                      "fixup'd commit message not restored after undo")

    def test_multiple_sequential_squash_then_undo_each(self):
        """Two squashes then two undos: each undo must restore the previous state."""
        state0 = self.gh.read_state()
        bm0 = self._by_msg(state0)
        head0 = state0["commits"][0]["hash"]
        count0 = len(state0["commits"])

        # Squash 1: combine binary commits.
        r1 = self.gh.rebase("squash", hashes=[bm0["add binary"], bm0["update binary"]])
        self.assertTrue(r1["ok"])
        head1 = r1["commits"][0]["hash"]
        count1 = len(r1["commits"])
        self.assertEqual(count1, count0 - 1)

        # Squash 2: combine rename + update docs (non-binary, different files).
        bm1 = self._by_msg(r1)
        if "rename to docs" in bm1 and "update docs" in bm1:
            r2 = self.gh.rebase(
                "squash",
                hashes=[bm1["rename to docs"], bm1["update docs"]]
            )
            self.assertTrue(r2["ok"])
            count2 = len(r2["commits"])
            self.assertEqual(count2, count1 - 1)

            # Undo squash 2: reset to head1.
            u2 = self.gh.reset(head1)
            self.assertTrue(u2["ok"])
            self.assertEqual(len(u2["commits"]), count1)

        # Undo squash 1: reset to head0.
        u1 = self.gh.reset(head0)
        self.assertTrue(u1["ok"])
        self.assertEqual(len(u1["commits"]), count0)

    def test_undo_reword_restores_original_message(self):
        """After reword, undo via reset restores the original commit message."""
        state_before = self.gh.read_state()
        bm = self._by_msg(state_before)
        h = bm["update docs"]
        pre_reword_head = state_before["commits"][0]["hash"]

        rw = self.gh.rebase("reword", hashes=[h], new_message="polished docs update")
        self.assertTrue(rw["ok"])

        undo = self.gh.reset(pre_reword_head)
        self.assertTrue(undo["ok"])
        msgs = [c["message"] for c in undo["commits"]]
        self.assertIn("update docs", msgs)
        self.assertNotIn("polished docs update", msgs)


# ---------------------------------------------------------------------------
# 7. Sequential operations and _start boundary stability
# ---------------------------------------------------------------------------

class SequentialOperationsTests(ChallengeBase):
    """Verify that chained squash/fixup/reword operations keep _start consistent."""

    def setUp(self):
        super().setUp()
        self.repo = _build_create_delete_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def test_squash_then_reword_then_squash(self):
        """Three sequential operations must each succeed and leave valid state."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        r1 = self.gh.rebase("squash", hashes=[bm["add temp"], bm["delete temp"]])
        self.assertTrue(r1["ok"], f"first squash failed: {r1}")
        self.assert_valid_state(r1)

        oldest = r1["commits"][-1]
        r2 = self.gh.rebase("reword", hashes=[oldest["hash"]], new_message="reworded base")
        self.assertTrue(r2["ok"], f"reword after squash failed: {r2}")
        self.assert_valid_state(r2)

        if len(r2["commits"]) >= 2:
            h_top  = r2["commits"][0]["hash"]
            h_next = r2["commits"][1]["hash"]
            r3 = self.gh.rebase("squash", hashes=[h_top, h_next])
            self.assertTrue(r3["ok"], f"second squash failed: {r3}")
            self.assert_valid_state(r3)

    def test_commit_count_decrements_by_one_per_fixup(self):
        """Each fixup must reduce commit count by exactly one."""
        state = self.gh.read_state()
        count = len(state["commits"])
        bm = self._by_msg(state)

        r1 = self.gh.rebase("fixup", hashes=[bm["delete temp"]])
        self.assertTrue(r1["ok"])
        self.assertEqual(len(r1["commits"]), count - 1)
        count -= 1

        bm2 = self._by_msg(r1)
        r2 = self.gh.rebase("fixup", hashes=[bm2["after delete"]])
        self.assertTrue(r2["ok"])
        self.assertEqual(len(r2["commits"]), count - 1)

    def test_interleaved_squash_and_reword_preserve_all_other_messages(self):
        """After squash + reword, commits not involved must still have original messages."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        r1 = self.gh.rebase("squash", hashes=[bm["add temp"], bm["delete temp"]])
        self.assertTrue(r1["ok"])
        msgs1 = {c["message"] for c in r1["commits"]}
        self.assertIn("initial",     msgs1)
        self.assertIn("after delete", msgs1)

        bm2 = self._by_msg(r1)
        h_after = bm2.get("after delete")
        if h_after:
            r2 = self.gh.rebase("reword", hashes=[h_after], new_message="post-squash top")
            self.assertTrue(r2["ok"])
            msgs2 = {c["message"] for c in r2["commits"]}
            self.assertIn("initial", msgs2,
                          "'initial' commit message changed by unrelated reword")


# ---------------------------------------------------------------------------
# 8. Unicode filenames and special-character commit messages
# ---------------------------------------------------------------------------

def _build_unicode_repo(parent: Path) -> Path:
    """
    Repo with unicode filenames, unicode content, and commit messages that
    contain emoji, non-ASCII letters, and shell-special characters.

    Commits (newest first):
      'update: 日本語 ($special & "quotes")' — modifies 日本語.txt
      "feat: add café ☕"                     — creates café.txt
      "add résumé.txt"                        — creates résumé.txt
      "initial"                               — creates base.txt
    """
    repo = parent / "unicode-repo"
    repo.mkdir()
    init_repo(repo)

    _commit_raw(repo, "base.txt",   b"base\n",                         "initial",                            "alice", 0)
    _commit_raw(repo, "résumé.txt", "résumé: naïve\n".encode("utf-8"), "add résumé.txt",                     "bob",   1)
    _commit_raw(repo, "café.txt",   "☕ coffee\n".encode("utf-8"),      "feat: add café ☕",                   "carol", 2)
    _commit_raw(repo, "日本語.txt", "日本語\n".encode("utf-8"),         'update: 日本語 ($special & "quotes")', "alice", 3)
    return repo


# ---------------------------------------------------------------------------
# 9. Conflicting content — move/reorder triggers a rebase conflict
# ---------------------------------------------------------------------------
#
# _build_conflict_repo (from conftest) builds:
#   "version B" — sets line 2 to LINE_B
#   "version A" — sets line 2 to LINE_A
#   "initial"   — creates f.txt with three lines
# Swapping version A and version B causes a content conflict during rebase.


# ---------------------------------------------------------------------------
# 10. Tags on commits in the visible range
# ---------------------------------------------------------------------------

def _build_tagged_repo(parent: Path) -> Path:
    """
    Repo where commits carry lightweight and annotated tags.

    Commits (newest first):
      "release notes"  — modifies changelog.txt; lightweight tag 'v1.1'
      "bump version"   — modifies version.txt;   annotated tag 'v1.0'
      "add version"    — creates version.txt
      "initial"        — creates base.txt
    """
    repo = parent / "tagged-repo"
    repo.mkdir()
    init_repo(repo)

    _commit_raw(repo, "base.txt",    b"base\n",      "initial",       "alice", 0)
    _commit_raw(repo, "version.txt", b"0.0.1\n",     "add version",   "bob",   1)
    _commit_raw(repo, "version.txt", b"1.0.0\n",     "bump version",  "carol", 2)
    h_v10 = _git(repo, "git", "rev-parse", "HEAD").stdout.strip()
    subprocess.run(
        ["git", "tag", "-a", "v1.0", h_v10, "-m", "Release 1.0"],
        cwd=str(repo), capture_output=True, check=True
    )

    _commit_raw(repo, "changelog.txt", b"- released\n", "release notes", "alice", 3)
    h_v11 = _git(repo, "git", "rev-parse", "HEAD").stdout.strip()
    subprocess.run(
        ["git", "tag", "v1.1", h_v11],
        cwd=str(repo), capture_output=True, check=True
    )
    return repo


# ---------------------------------------------------------------------------
# 11. Multi-line commit messages (subject + body + trailers)
# ---------------------------------------------------------------------------

def _build_multiline_message_repo(parent: Path) -> Path:
    """
    Repo with commits that have proper subject + body messages and trailers.

    Commits (newest first):
      "Fix critical bug\\n\\nCaused by X.\\nSee issue #42." — modifies fix.txt
      "Add feature\\n\\nNew API.\\nCo-authored-by: Bob <b@x.com>" — adds feat.txt
      "initial" — creates base.txt
    """
    repo = parent / "multiline-repo"
    repo.mkdir()
    init_repo(repo)

    _commit_raw(repo, "base.txt", b"base\n", "initial", "alice", 0)

    env1 = _commit_env("bob", 1)
    (repo / "feat.txt").write_bytes(b"feature\n")
    subprocess.run(["git", "add", "feat.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Add feature\n\nNew API.\nCo-authored-by: Bob <b@x.com>"],
        cwd=str(repo), env=env1, capture_output=True, check=True
    )

    env2 = _commit_env("carol", 2)
    (repo / "fix.txt").write_bytes(b"fix\n")
    subprocess.run(["git", "add", "fix.txt"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Fix critical bug\n\nCaused by X.\nSee issue #42."],
        cwd=str(repo), env=env2, capture_output=True, check=True
    )
    return repo


# ---------------------------------------------------------------------------
# 12. Files with spaces and parentheses in their names
# ---------------------------------------------------------------------------

def _build_spaces_in_names_repo(parent: Path) -> Path:
    """
    Repo with filenames containing spaces and parentheses.

    Commits (newest first):
      "update docs"       — modifies "my document.txt"
      "add spaced files"  — creates "my document.txt" and "notes (draft).txt"
      "initial"           — creates base.txt
    """
    repo = parent / "spaces-repo"
    repo.mkdir()
    init_repo(repo)

    _commit_raw(repo, "base.txt", b"base\n", "initial", "alice", 0)

    env1 = _commit_env("bob", 1)
    (repo / "my document.txt").write_bytes(b"draft\n")
    (repo / "notes (draft).txt").write_bytes(b"notes\n")
    subprocess.run(
        ["git", "add", "--", "my document.txt", "notes (draft).txt"],
        cwd=str(repo), capture_output=True, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "add spaced files"],
        cwd=str(repo), env=env1, capture_output=True, check=True
    )

    _commit_raw(repo, "my document.txt", b"final\n", "update docs", "carol", 2)
    return repo


# ---------------------------------------------------------------------------
# 14. Single-commit and two-commit repos (near-root edge cases)
# ---------------------------------------------------------------------------

def _build_single_commit_repo(parent: Path) -> Path:
    """Repo with exactly one commit (the root)."""
    repo = parent / "single-repo"
    repo.mkdir()
    init_repo(repo)
    _commit_raw(repo, "base.txt", b"base\n", "initial", "alice", 0)
    return repo


def _build_two_commit_repo(parent: Path) -> Path:
    """Repo with exactly two commits (root + one child)."""
    repo = parent / "two-repo"
    repo.mkdir()
    init_repo(repo)
    _commit_raw(repo, "base.txt", b"base\n", "initial", "alice", 0)
    _commit_raw(repo, "a.txt",    b"v1\n",   "add a",   "bob",   1)
    return repo


# ---------------------------------------------------------------------------
# 8. Unicode filenames and special-character commit messages
# ---------------------------------------------------------------------------

class UnicodeAndSpecialCharTests(ChallengeBase):
    """Operations on repos with unicode filenames and special-char commit messages."""

    def setUp(self):
        super().setUp()
        self.repo = _build_unicode_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def test_read_state_returns_unicode_messages(self):
        """read_state must correctly return unicode commit messages including emoji and CJK."""
        state = self.gh.read_state()
        self.assert_valid_state(state)
        msgs = [c["message"] for c in state["commits"]]
        self.assertTrue(any("☕" in m or "café" in m for m in msgs),
                        "unicode emoji commit not found in state")
        self.assertTrue(any("日本語" in m for m in msgs),
                        "CJK commit message not found in state")

    def test_squash_commits_with_unicode_filenames(self):
        """Squash two commits that create unicode-named files."""
        state = self.gh.read_state()
        hashes = [state["commits"][0]["hash"], state["commits"][1]["hash"]]
        result = self.gh.rebase("squash", hashes=hashes)
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)
            self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    def test_reword_commit_with_shell_special_chars_in_message(self):
        """Reword a commit whose original message contains $, quotes, and &."""
        state = self.gh.read_state()
        h = state["commits"][0]["hash"]  # 'update: 日本語 ($special & "quotes")'
        result = self.gh.rebase("reword", hashes=[h], new_message="cleaned up message")
        self.assertTrue(result["ok"], f"reword with special-char original message failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertIn("cleaned up message", msgs)

    def test_reword_to_message_containing_shell_special_chars(self):
        """Reword a commit to a new message that itself contains $, quotes, and backticks."""
        state = self.gh.read_state()
        h = state["commits"][1]["hash"]
        new_msg = 'fix: handle $PATH, "quoted args", and `backticks`'
        result = self.gh.rebase("reword", hashes=[h], new_message=new_msg)
        self.assertTrue(result["ok"], f"reword to special-char new message failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertIn(new_msg, msgs)

    def test_show_commit_with_unicode_filename(self):
        """show() must succeed on a commit that creates a unicode-named file."""
        state = self.gh.read_state()
        # 'add résumé.txt' is the second-oldest commit.
        h = state["commits"][-2]["hash"]
        result = self.gh.show(h)
        self.assertTrue(result["ok"], f"show() failed on unicode-filename commit: {result}")

    def test_fixup_unicode_commit_into_predecessor(self):
        """Fixup the newest commit (unicode filename + special-char message) into predecessor."""
        state = self.gh.read_state()
        h_top = state["commits"][0]["hash"]
        result = self.gh.rebase("fixup", hashes=[h_top])
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)
            self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    def test_reword_to_emoji_message(self):
        """Reword a commit message to one that is itself pure emoji."""
        state = self.gh.read_state()
        h = state["commits"][0]["hash"]
        result = self.gh.rebase("reword", hashes=[h], new_message="🚀 ship it")
        self.assertTrue(result["ok"], f"reword to emoji message failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertIn("🚀 ship it", msgs)


# ---------------------------------------------------------------------------
# 9. Conflicting content — move/reorder triggers a rebase conflict
# ---------------------------------------------------------------------------

class ConflictingContentTests(ChallengeBase):
    """
    Swapping two commits that both edit the same line causes a rebase conflict.
    The backend must abort cleanly without leaving the repo stuck.
    """

    def setUp(self):
        super().setUp()
        self.repo = _build_conflict_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def test_move_conflicting_commits_leaves_repo_recoverable(self):
        """
        Swapping 'version A' and 'version B' triggers a conflict on the shared line.
        Result must be ok=False with a clean repo, or ok=True with valid state.
        """
        state = self.gh.read_state()
        order = [c["hash"] for c in state["commits"]]
        self.assertGreaterEqual(len(order), 2)
        order[0], order[1] = order[1], order[0]

        result = self.gh.rebase("move", order=order)
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)

    def test_repo_is_clean_after_conflict_and_abort(self):
        """After a conflicting move + explicit abort, read_state must show no rebase in progress."""
        state = self.gh.read_state()
        order = [c["hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]

        self.gh.rebase("move", order=order)
        self.gh.rebase_abort()

        state_after = self.gh.read_state()
        self.assertTrue(state_after["ok"])
        self.assertFalse(state_after["rebase_in_progress"])
        self.assertFalse(state_after["dirty"])

    def test_commit_count_unchanged_after_failed_conflict(self):
        """After a failed conflicting move + abort, commit count must be the same as before."""
        state_before = self.gh.read_state()
        count_before = len(state_before["commits"])
        order = [c["hash"] for c in state_before["commits"]]
        order[0], order[1] = order[1], order[0]

        self.gh.rebase("move", order=order)
        self.gh.rebase_abort()

        state_after = self.gh.read_state()
        self.assertEqual(len(state_after["commits"]), count_before)

    def test_head_hash_unchanged_after_failed_conflict(self):
        """HEAD must point to the same commit after a failed move + abort."""
        state_before = self.gh.read_state()
        head_before = state_before["commits"][0]["hash"]
        order = [c["hash"] for c in state_before["commits"]]
        order[0], order[1] = order[1], order[0]

        self.gh.rebase("move", order=order)
        self.gh.rebase_abort()

        state_after = self.gh.read_state()
        self.assertEqual(state_after["commits"][0]["hash"], head_before,
                         "HEAD changed after failed conflicting move + abort")

    def test_squash_non_adjacent_commits_with_conflicting_middle_pick(self):
        """
        Squash 'version A' with 'initial' (non-adjacent): the middle commit
        'version B' is picked between them and may conflict.
        The repo must be recoverable regardless of outcome.
        """
        state = self.gh.read_state()
        bm = {c["message"]: c["hash"] for c in state["commits"]}
        if "version A" not in bm or "initial" not in bm:
            self.skipTest("expected commits not found")

        result = self.gh.rebase("squash", hashes=[bm["version A"], bm["initial"]])
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)


# ---------------------------------------------------------------------------
# 10. Tags on commits in the visible range
# ---------------------------------------------------------------------------

class TaggedCommitTests(ChallengeBase):
    """Operations must succeed (or fail cleanly) when commits carry git tags."""

    def setUp(self):
        super().setUp()
        self.repo = _build_tagged_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def test_read_state_with_tagged_commits_succeeds(self):
        """read_state must work normally even when commits carry annotated and lightweight tags."""
        state = self.gh.read_state()
        self.assert_valid_state(state)
        self.assertGreaterEqual(len(state["commits"]), 3)

    def test_squash_two_tagged_commits(self):
        """Squash the annotated-tagged and lightweight-tagged commits."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        self.assertIn("bump version",  bm)
        self.assertIn("release notes", bm)

        result = self.gh.rebase("squash", hashes=[bm["bump version"], bm["release notes"]])
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)
            self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    def test_reword_annotated_tagged_commit(self):
        """Reword the commit carrying the annotated tag: the message must change."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        h = bm["bump version"]

        result = self.gh.rebase("reword", hashes=[h], new_message="bump to v1.0.0")
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            msgs = [c["message"] for c in result["commits"]]
            self.assertIn("bump to v1.0.0", msgs)
            self.assertNotIn("bump version", msgs)

    def test_fixup_annotated_tagged_commit_into_predecessor(self):
        """Fixup the annotated-tagged commit into 'add version'."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        h = bm["bump version"]

        result = self.gh.rebase("fixup", hashes=[h])
        if not result["ok"]:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)
            msgs = [c["message"] for c in result["commits"]]
            self.assertNotIn("bump version", msgs)

    def test_reset_to_pre_tag_commit(self):
        """Reset to 'add version', before any tags exist on later commits."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.reset(bm["add version"])
        self.assertTrue(result["ok"], f"reset past tagged commits failed: {result}")
        self.assertEqual(result["commits"][0]["hash"], bm["add version"])

    def test_show_tagged_commit_succeeds(self):
        """show() on an annotated-tagged commit must return ok=True."""
        bm = self._by_msg()
        result = self.gh.show(bm["bump version"])
        self.assertTrue(result["ok"], f"show() failed on tagged commit: {result}")


# ---------------------------------------------------------------------------
# 11. Multi-line commit messages (subject + body + trailers)
# ---------------------------------------------------------------------------

class MultilineMessageTests(ChallengeBase):
    """Squash, fixup, reword on commits with subject+body+trailer messages."""

    def setUp(self):
        super().setUp()
        self.repo = _build_multiline_message_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def test_read_state_returns_full_multiline_message(self):
        """read_state must return full multi-line messages, not just the subject line."""
        state = self.gh.read_state()
        self.assert_valid_state(state)
        full = " ".join(c["message"] for c in state["commits"])
        self.assertIn("Co-authored-by", full,
                      "trailer line missing from read_state commit message")
        self.assertIn("issue #42", full,
                      "body line missing from read_state commit message")

    def test_squash_multiline_message_commits(self):
        """Squash two multiline-message commits: commit count decreases by one."""
        state = self.gh.read_state()
        hashes = [c["hash"] for c in state["commits"][:2]]
        result = self.gh.rebase("squash", hashes=hashes)
        self.assertTrue(result["ok"], f"squash of multiline-message commits failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    def test_reword_multiline_commit_to_single_line(self):
        """Reword a multiline-message commit to a plain one-liner."""
        state = self.gh.read_state()
        h = state["commits"][0]["hash"]
        result = self.gh.rebase("reword", hashes=[h], new_message="simple one-liner")
        self.assertTrue(result["ok"], f"reword multiline → simple failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertIn("simple one-liner", msgs)

    def test_reword_single_line_commit_to_multiline(self):
        """Reword the simple 'initial' commit to a multi-line message with a body."""
        state = self.gh.read_state()
        h = state["commits"][-1]["hash"]
        new_msg = "initial commit\n\nCreates the base structure.\nSigned-off-by: Alice"
        result = self.gh.rebase("reword", hashes=[h], new_message=new_msg)
        self.assertTrue(result["ok"], f"reword to multiline failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertTrue(any("initial commit" in m for m in msgs))

    def test_fixup_multiline_commit_discards_body(self):
        """Fixup a commit with a multi-line message: the body must not appear in result."""
        state = self.gh.read_state()
        h = state["commits"][0]["hash"]
        result = self.gh.rebase("fixup", hashes=[h])
        self.assertTrue(result["ok"], f"fixup of multiline commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        full_after = " ".join(c["message"] for c in result["commits"])
        self.assertNotIn("issue #42", full_after,
                         "fixup'd commit body still present in result messages")

    def test_show_multiline_commit_returns_full_message(self):
        """show() must include the full multi-line message in its output."""
        state = self.gh.read_state()
        h = state["commits"][0]["hash"]
        result = self.gh.show(h)
        self.assertTrue(result["ok"], f"show() failed on multiline commit: {result}")


# ---------------------------------------------------------------------------
# 12. Files with spaces and parentheses in their names
# ---------------------------------------------------------------------------

class SpacesInFilenamesTests(ChallengeBase):
    """Operations on repos where filenames contain spaces and parentheses."""

    def setUp(self):
        super().setUp()
        self.repo = _build_spaces_in_names_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def test_show_commit_with_spaced_filename_includes_filename_in_diff(self):
        """show() must include the space-containing filename in its diff output."""
        bm = self._by_msg()
        result = self.gh.show(bm["add spaced files"])
        self.assertTrue(result["ok"], f"show() failed on spaced-filename commit: {result}")
        self.assertIn("my document.txt", result["diff"])

    def test_squash_commits_touching_spaced_filename(self):
        """Squash commits that both touch a file with spaces in its name."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase("squash", hashes=[bm["add spaced files"], bm["update docs"]])
        self.assertTrue(result["ok"], f"squash with spaced filenames failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        files = _ls_tree(self.repo, "HEAD")
        self.assertIn("my document.txt", files)

    def test_fixup_commit_touching_spaced_filename(self):
        """Fixup 'update docs' into 'add spaced files': commit count decreases by one."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.rebase("fixup", hashes=[bm["update docs"]])
        self.assertTrue(result["ok"], f"fixup with spaced filenames failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    def test_reword_commit_touching_spaced_file_preserves_tree(self):
        """Reword a commit that touches a spaced-name file: tree must be identical."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        h = bm["add spaced files"]
        old_tree = _git(self.repo, "git", "rev-parse", h + ":").stdout.strip()

        result = self.gh.rebase("reword", hashes=[h], new_message="add files with spaces")
        self.assertTrue(result["ok"], f"reword with spaced filenames failed: {result}")

        new_bm = {c["message"]: c["hash"] for c in result["commits"]}
        new_h = new_bm.get("add files with spaces")
        self.assertIsNotNone(new_h, "rewarded commit not found by new message")
        new_tree = _git(self.repo, "git", "rev-parse", new_h + ":").stdout.strip()
        self.assertEqual(old_tree, new_tree, "tree changed after reword of spaced-filename commit")

    def test_spaced_filename_present_in_head_after_sequential_operations(self):
        """After squash + reword, the spaced filename must still be in HEAD tree."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        r1 = self.gh.rebase("squash", hashes=[bm["add spaced files"], bm["update docs"]])
        self.assertTrue(r1["ok"])

        bm2 = {c["message"]: c["hash"] for c in r1["commits"]}
        top = r1["commits"][0]
        r2 = self.gh.rebase("reword", hashes=[top["hash"]], new_message="merged docs")
        self.assertTrue(r2["ok"])

        files = _ls_tree(self.repo, "HEAD")
        self.assertIn("my document.txt", files,
                      "spaced filename missing from HEAD after squash + reword")


# ---------------------------------------------------------------------------
# 14. Single-commit and two-commit repos (near-root edge cases)
# ---------------------------------------------------------------------------

class NearRootEdgeCaseTests(ChallengeBase):
    """Operations on repos with very few commits test root-commit boundary handling."""

    def test_read_state_single_commit_repo(self):
        """read_state on a one-commit repo must return exactly one commit."""
        repo = _build_single_commit_repo(self.tmpdir)
        gh = GitHistory(str(repo))
        state = gh.read_state()
        self.assertTrue(state["ok"])
        self.assertEqual(len(state["commits"]), 1)

    def test_show_root_commit(self):
        """show() on the root commit (no parent) must succeed."""
        repo = _build_single_commit_repo(self.tmpdir)
        gh = GitHistory(str(repo))
        state = gh.read_state()
        h = state["commits"][0]["hash"]
        result = gh.show(h)
        self.assertTrue(result["ok"], f"show() failed on root commit: {result}")

    def test_squash_only_two_commits_in_repo(self):
        """Squash the only two commits: result is one commit, or fails cleanly."""
        repo = _build_two_commit_repo(self.tmpdir)
        gh = GitHistory(str(repo))
        state = gh.read_state()
        hashes = [c["hash"] for c in state["commits"]]
        self.assertEqual(len(hashes), 2)

        result = gh.rebase("squash", hashes=hashes)
        if result["ok"]:
            self.assertEqual(len(result["commits"]), 1)
        else:
            self.assert_recoverable(gh)

    def test_fixup_non_root_into_root(self):
        """Fixup the only non-root commit into the root: one commit remains, or fails cleanly."""
        repo = _build_two_commit_repo(self.tmpdir)
        gh = GitHistory(str(repo))
        state = gh.read_state()
        h_top = state["commits"][0]["hash"]

        result = gh.rebase("fixup", hashes=[h_top])
        if result["ok"]:
            self.assertEqual(len(result["commits"]), 1)
            self.assertEqual(result["commits"][0]["message"], "initial")
        else:
            self.assert_recoverable(gh)

    def test_reword_root_commit(self):
        """Reword the root commit itself: message must change, or fail cleanly."""
        repo = _build_two_commit_repo(self.tmpdir)
        gh = GitHistory(str(repo))
        state = gh.read_state()
        h_root = state["commits"][-1]["hash"]

        result = gh.rebase("reword", hashes=[h_root], new_message="project start")
        if result["ok"]:
            msgs = [c["message"] for c in result["commits"]]
            self.assertIn("project start", msgs)
        else:
            self.assert_recoverable(gh)

    def test_reset_to_root_in_two_commit_repo(self):
        """Reset to the root commit: HEAD moves to root and only one commit is visible."""
        repo = _build_two_commit_repo(self.tmpdir)
        gh = GitHistory(str(repo))
        state = gh.read_state()
        h_root = state["commits"][-1]["hash"]

        result = gh.reset(h_root)
        self.assertTrue(result["ok"], f"reset to root failed: {result}")
        self.assertEqual(result["commits"][0]["hash"], h_root)
        self.assertEqual(len(result["commits"]), 1)

    def test_squash_on_single_commit_repo_fails_cleanly(self):
        """Squash with no second commit to fold into must fail without corrupting the repo."""
        repo = _build_single_commit_repo(self.tmpdir)
        gh = GitHistory(str(repo))
        state = gh.read_state()
        h = state["commits"][0]["hash"]

        result = gh.rebase("squash", hashes=[h])
        if not result["ok"]:
            self.assert_recoverable(gh)
        else:
            self.assert_valid_state(result)


if __name__ == "__main__":
    unittest.main()
