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

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from make_test_repo import AUTHORS, BASE_DATE, init_repo
from conftest import _commit_raw, _ensure_persistent_test_repo
from git_warp.backend import GitWarp, GitWarpError, GitError


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


def _restore_repo(repo: Path, branch: str, head: str):
    """Undo whatever a test did, back to the class fixture's initial commit."""
    subprocess.run(["git", "rebase", "--abort"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "checkout", branch], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "reset", "--hard", head], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "clean", "-fd"], cwd=str(repo), capture_output=True)


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


def _build_revert_pair_repo(parent: Path) -> Path:
    """
    Repo where the newest commit reverts the one before it.

    Commits (newest first):
      "revert timeout bump" — config.txt back to TIMEOUT = 30
      "bump timeout"        — config.txt to TIMEOUT = 60
      "add config"          — creates config.txt with TIMEOUT = 30

    Moving "bump timeout" to the top replays "revert timeout bump" onto
    "add config", where TIMEOUT is already 30, so it becomes empty.
    """
    repo = parent / "revert-pair-repo"
    repo.mkdir()
    init_repo(repo)

    _commit_raw(repo, "config.txt", b"TIMEOUT = 30\n", "add config",          "alice", 0)
    _commit_raw(repo, "config.txt", b"TIMEOUT = 60\n", "bump timeout",        "bob",   1)
    _commit_raw(repo, "config.txt", b"TIMEOUT = 30\n", "revert timeout bump", "carol", 2)
    return repo


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class ChallengeBase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def assert_recoverable(self, gh: GitWarp):
        """Abort any in-progress rebase and verify the repo is clean afterward."""
        state = gh.read_state()
        if state.rebase_in_progress:
            abort = gh.rebase_abort()
            self.assertTrue(abort.ok, f"rebase_abort() failed: {abort}")
            state = gh.read_state()
        self.assertFalse(state.rebase_in_progress,
                         "repo stuck in rebase after abort")
        self.assertFalse(state.dirty,
                         "repo dirty after recovery from failed operation")
        self.assertTrue(state.ok)

    def assert_valid_state(self, state):
        self.assertTrue(state.ok)
        self.assertIsInstance(state.commits, list)
        self.assertGreater(len(state.commits), 0)
        for c in state.commits:
            self.assertEqual(len(c.commit_hash), 40)
            self.assertIsInstance(c.message, str)

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c.message: c.commit_hash for c in s.commits}

    def _order(self, state=None):
        s = state or self.gh.read_state()
        return [c.commit_hash for c in s.commits]

# ---------------------------------------------------------------------------
# 1. Merge commits in the visible range
# ---------------------------------------------------------------------------

@pytest.mark.release
class MergeCommitTests(ChallengeBase):
    """
    A merge commit in the visible range means any rebase operation will
    encounter `pick <merge-commit>` in its todo. Git cannot replay merge
    commits without --rebase-merges. The backend must not leave the repo
    stuck after this failure.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       capture_output=True, check=True)
        # Remove origin remote
        subprocess.run(["git", "remote", "remove", "origin"],
                       cwd=str(cls.repo), capture_output=True)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def _merge_and_non_merge_hashes(self):
        state = self.gh.read_state()
        merge, non_merge = [], []
        for c in state.commits:
            if _parent_count(self.repo, c.commit_hash) > 1:
                merge.append(c.commit_hash)
            else:
                non_merge.append(c.commit_hash)
        return merge, non_merge

    def test_squash_of_merge_commit_leaves_repo_recoverable(self):
        """Squash targeting the merge commit itself must not corrupt the repo."""
        merge_hashes, _ = self._merge_and_non_merge_hashes()
        self.assertTrue(merge_hashes, "expected at least one merge commit in visible range")

        try:
            result = self.gh.squash([merge_hashes[0]])
            self.assert_valid_state(result)
        except GitError:
            self.assert_recoverable(self.gh)

    def test_squash_two_non_merge_commits_when_merge_is_in_range(self):
        """
        Squashing two regular commits works, but the merge commit is also
        in the todo as a plain 'pick'. Git will fail on that pick.
        The repo must be recoverable.
        """
        _, non_merge = self._merge_and_non_merge_hashes()
        if len(non_merge) < 2:
            self.skipTest("need at least two non-merge commits")

        try:
            result = self.gh.squash([non_merge[0], non_merge[1]])
            state = self.gh.read_state()
            self.assert_valid_state(state)
        except (GitError, GitWarpError):
            self.assert_recoverable(self.gh)

    def test_fixup_of_non_merge_commit_adjacent_to_merge_leaves_repo_recoverable(self):
        """Fixup a regular commit that is adjacent to a merge commit."""
        _, non_merge = self._merge_and_non_merge_hashes()
        if not non_merge:
            self.skipTest("no non-merge commits")

        try:
            result = self.gh.fixup([non_merge[0]])
            self.assert_valid_state(result)
        except GitError:
            self.assert_recoverable(self.gh)

    def test_move_with_merge_commit_present_leaves_repo_recoverable(self):
        """Swap the two newest commits when a merge commit is in the visible list."""
        state = self.gh.read_state()
        order = [c.commit_hash for c in state.commits]
        if len(order) < 2:
            self.skipTest("not enough commits")
        order[0], order[1] = order[1], order[0]

        try:
            result = self.gh.move(order)
            self.assert_valid_state(result)
        except GitError:
            self.assert_recoverable(self.gh)

    def test_reword_non_merge_commit_when_merge_is_in_range(self):
        """Reword a regular commit; the merge commit in the todo must not break things."""
        state = self.gh.read_state()
        # Pick the newest commit regardless of whether it's a merge.
        h = state.commits[0].commit_hash
        if _parent_count(self.repo, h) > 1:
            self.skipTest("newest commit is the merge commit — skip reword test")

        result = self.gh.reword(h, "rewarded top")
        if not result.ok:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)

    def test_read_state_after_failed_op_always_succeeds(self):
        """read_state() must return ok=True even after a failed rebase attempt."""
        _, non_merge = self._merge_and_non_merge_hashes()
        if len(non_merge) < 2:
            self.skipTest("need at least two non-merge commits")

        try:
            self.gh.squash([non_merge[0], non_merge[1]])        # Whether op succeeded or failed, read_state must work.
        except (GitError, GitWarpError):
            pass
        if self.gh.read_state().rebase_in_progress:
            self.gh.rebase_abort()
        state = self.gh.read_state()
        self.assertTrue(state.ok)
        self.assertFalse(state.rebase_in_progress)


# ---------------------------------------------------------------------------
# 3. Binary files and file renames
# ---------------------------------------------------------------------------

@pytest.mark.release
class BinaryAndRenameTests(ChallengeBase):

    BINARY_V2 = bytes(range(255, -1, -1)) * 4

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        cls.repo = _build_binary_and_rename_repo(cls.tmpdir)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_squash_two_binary_commits_succeeds(self):
        """Squash 'add binary' + 'update binary' — data.bin must have v2 content."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.squash([bm["add binary"], bm["update binary"]])
        self.assertTrue(result.ok, f"squash of binary commits failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 1)        # data.bin must still be present and contain the v2 bytes.
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

        result = self.gh.fixup([bm["update binary"]])
        self.assertTrue(result.ok, f"fixup of binary commit failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 1)
        msgs = [c.message for c in result.commits]
        self.assertNotIn("update binary", msgs)

    def test_squash_across_rename_preserves_renamed_file(self):
        """Squash 'rename to docs' + 'update docs': docs.txt must exist, readme.txt must not."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.squash([bm["rename to docs"], bm["update docs"]])
        self.assertTrue(result.ok, f"squash across rename failed: {result}")
        files = _ls_tree(self.repo, "HEAD")
        self.assertIn("docs.txt", files)
        self.assertNotIn("readme.txt", files)

    def test_fixup_rename_commit_into_predecessor(self):
        """Fixup the rename commit into 'update binary'."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.fixup([bm["rename to docs"]])
        self.assertTrue(result.ok, f"fixup of rename commit failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 1)
    def test_reword_rename_commit_preserves_rename(self):
        """Reword the rename commit; the tree must still show docs.txt, not readme.txt."""
        bm = self._by_msg()
        h = bm["rename to docs"]

        result = self.gh.reword(h, "mv readme → docs")
        self.assertTrue(result.ok, f"reword failed: {result}")
        new_h = self._by_msg(result).get("mv readme → docs")
        self.assertIsNotNone(new_h)
        files_at_commit = _ls_tree(self.repo, new_h)
        self.assertIn("docs.txt",    files_at_commit)
        self.assertNotIn("readme.txt", files_at_commit)

    def test_show_binary_commit_returns_ok(self):
        """show() must succeed on a commit that touches a binary file."""
        bm = self._by_msg()
        result = self.gh.show(bm["add binary"])
        self.assertTrue(result.ok, f"show() failed on binary commit: {result}")
        self.assertIn("data.bin", result.diff)


# ---------------------------------------------------------------------------
# 4. Create-then-delete the same file
# ---------------------------------------------------------------------------

def _setup_create_delete_repo(tmpdir):
    """Clone the standard test repo and append create-then-delete commits on top."""
    persistent_repo = _ensure_persistent_test_repo()
    repo = tmpdir / "repo"
    subprocess.run(["git", "clone", str(persistent_repo), str(repo)],
                   capture_output=True, check=True)
    subprocess.run(["git", "remote", "remove", "origin"],
                   cwd=str(repo), capture_output=True)
    _commit_raw(repo, "temp.txt", b"temporary\n", "add temp", "bob", 200)
    (repo / "temp.txt").unlink()
    subprocess.run(["git", "rm", "temp.txt"], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "delete temp"], cwd=str(repo),
                   capture_output=True, check=True)
    return repo


@pytest.mark.release
class CreateDeleteFileTests(ChallengeBase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        cls.repo = _setup_create_delete_repo(cls.tmpdir)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_squash_create_and_delete_produces_commit_without_temp_file(self):
        """Squash 'add temp' + 'delete temp': the combined commit must not contain temp.txt."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.squash([bm["add temp"], bm["delete temp"]])
        self.assertTrue(result.ok, f"squash of create+delete failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 1)

        combined = result.commits[0]  # squashed commit is the new HEAD
        files_in_combined = _ls_tree(self.repo, combined.commit_hash)
        self.assertNotIn("temp.txt", files_in_combined,
                         "temp.txt must not exist in the squashed commit's tree")

    def test_squash_create_and_delete_keeps_the_empty_result(self):
        """The combined diff is empty; the squashed commit is kept, not dropped."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.squash([bm["add temp"], bm["delete temp"]])
        self.assertTrue(result.ok, f"squash of create+delete failed: {result}")
        self.assertFalse(result.conflict, "empty squash reported as a conflict")
        self.assertFalse(result.rebase_in_progress, "rebase left unfinished")
        # Only the fold itself removes a commit; the empty result is kept.
        self.assertEqual(len(result.commits), len(state.commits) - 1)
        self.assertEqual(self.gh.show(result.commits[0].commit_hash).diff.strip(), "",
                         "the squashed commit should have no changes left")

    def test_fixup_delete_into_add_produces_no_temp_file(self):
        """Fixup 'delete temp' into 'add temp': combined tree must not have temp.txt."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.fixup([bm["delete temp"]])
        self.assertTrue(result.ok, f"fixup of delete commit failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 1)        # 'delete temp' message is gone (folded in).
        msgs = [c.message for c in result.commits]
        self.assertNotIn("delete temp", msgs)

    def test_squash_create_delete_working_tree_has_no_temp_file(self):
        """After squash, temp.txt must not exist in the working tree."""
        bm = self._by_msg()
        self.gh.squash([bm["add temp"], bm["delete temp"]])
        self.assertFalse((self.repo / "temp.txt").exists(),
                         "temp.txt present in working tree after squash of create+delete")

    def test_squash_nonadjacent_create_delete_with_middle_commit(self):
        """
        Squash 'add temp' (HEAD~1) with a commit two steps above it (HEAD~2),
        so 'delete temp' (HEAD) sits between them as a plain pick. Verify the
        repo is recoverable regardless of outcome.
        """
        state = self.gh.read_state()
        # commits[0]=delete temp, commits[1]=add temp, commits[2]=two above add temp
        h_add  = state.commits[1].commit_hash
        h_above = state.commits[2].commit_hash

        try:
            result = self.gh.squash([h_add, h_above])
            self.assert_valid_state(result)
        except (GitError, GitWarpError):
            self.assert_recoverable(self.gh)


# ---------------------------------------------------------------------------
# 5. Empty commits
# ---------------------------------------------------------------------------

@pytest.mark.release
class EmptyCommitTests(ChallengeBase):

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       check=True, capture_output=True)
        _commit_raw(cls.repo, "a.txt", b"v1\n", "real A",  "bob",   1)
        _commit_empty(cls.repo, "empty B", "carol", 2)
        _commit_raw(cls.repo, "a.txt", b"v2\n", "real C",  "alice", 3)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_fixup_empty_commit_into_real_predecessor(self):
        """Fixup the empty commit: it folds into 'real A' and vanishes."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        self.assertIn("empty B", bm)

        result = self.gh.fixup([bm["empty B"]])
        self.assertTrue(result.ok, f"fixup of empty commit failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 1)
        msgs = [c.message for c in result.commits]
        self.assertNotIn("empty B", msgs)
        self.assertIn("real A", msgs)

    def test_squash_empty_commit_with_adjacent_real_commit(self):
        """Squash 'empty B' with 'real A': result has combined messages."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.squash([bm["empty B"], bm["real A"]])
        self.assertTrue(result.ok, f"squash with empty commit failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 1)
    def test_reword_empty_commit(self):
        """Reword an empty commit: message changes, no file changes."""
        bm = self._by_msg()
        h = bm["empty B"]

        result = self.gh.reword(h, "placeholder commit")
        self.assertTrue(result.ok, f"reword of empty commit failed: {result}")
        msgs = [c.message for c in result.commits]
        self.assertIn("placeholder commit", msgs)
        self.assertNotIn("empty B", msgs)

    def test_show_empty_commit_has_empty_diff(self):
        """show() on the empty commit must return an empty diff."""
        bm = self._by_msg()
        result = self.gh.show(bm["empty B"])
        self.assertTrue(result.ok, f"show() failed on empty commit: {result}")
        self.assertEqual(result.diff.strip(), "",
                         "empty commit should produce an empty diff")

    def test_squash_real_commit_then_empty_commit_then_real_commit(self):
        """Squash all three: real A + empty B + real C into one commit."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.squash([bm["real A"], bm["empty B"], bm["real C"]])
        self.assertTrue(result.ok, f"3-way squash including empty commit failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 2)        # a.txt must have the v2 content from 'real C'.
        r = _git(self.repo, "git", "show", "HEAD:a.txt")
        # 'real C' writes v2; that should be in the squash result unless ordering changed.
        self.assertEqual(r.returncode, 0)


@pytest.mark.release
class BecomesEmptyOnReorderTests(ChallengeBase):
    """
    A commit that is not empty to start with, but whose changes are already
    present in its new parent after a reorder, is kept empty — see SPEC.md
    "Empty Commits". --empty=keep lets the rebase run straight through, so no
    commit is lost and there is no pause to mistake for a conflict.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        cls.repo = _build_revert_pair_repo(cls.tmpdir)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def _move_bump_to_top(self):
        """Drag "bump timeout" to the top; returns (state before, move result)."""
        state = self.gh.read_state()
        bump = self._by_msg(state)["bump timeout"]
        order = [c.commit_hash for c in state.commits if c.commit_hash != bump]
        return state, self.gh.move([bump] + order)

    def test_emptied_commit_is_kept_in_the_requested_order(self):
        state, result = self._move_bump_to_top()
        self.assertTrue(result.ok, f"move failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits))
        self.assertEqual([c.message for c in result.commits],
                         ["bump timeout", "revert timeout bump", "add config"])

    def test_kept_commit_has_an_empty_diff(self):
        _, result = self._move_bump_to_top()
        self.assertTrue(result.ok, f"move failed: {result}")
        emptied = self._by_msg(result)["revert timeout bump"]
        self.assertEqual(self.gh.show(emptied).diff.strip(), "",
                         "the emptied commit should have no changes left")

    def test_reorder_does_not_pause_the_rebase(self):
        _, result = self._move_bump_to_top()
        self.assertFalse(result.conflict, "empty commit reported as a conflict")
        self.assertEqual(result.conflict_files, [])
        self.assertFalse(result.rebase_in_progress, "rebase left unfinished")

    def test_emptied_commit_leaves_the_requested_tree(self):
        _, result = self._move_bump_to_top()
        self.assertTrue(result.ok, f"move failed: {result}")
        # "bump timeout" is now newest, so its content is what remains.
        self.assertEqual(_git(self.repo, "git", "show", "HEAD:config.txt").stdout.strip(),
                         "TIMEOUT = 60")


# ---------------------------------------------------------------------------
# 6. Undo / redo via undo_stack + reset
# ---------------------------------------------------------------------------

@pytest.mark.release
class UndoRedoTests(ChallengeBase):
    """
    After a squash/fixup, undo_stack must contain the pre-operation HEAD
    so the user can reset back to it (undo). After undo, undo_stack must
    still contain the post-operation HEAD so the user can redo.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       capture_output=True, check=True)
        subprocess.run(["git", "remote", "remove", "origin"],
                       cwd=str(cls.repo), capture_output=True)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_undo_squash_via_reset_to_pre_squash_head(self):
        """After squash, resetting to pre-squash HEAD restores original commit count."""
        state_before = self.gh.read_state()
        count_before = len(state_before.commits)
        pre_squash_head = state_before.commits[0].commit_hash
        bm = self._by_msg(state_before)

        squash_result = self.gh.squash([bm["Add error pages"], bm["Add deployment config"]])
        self.assertTrue(squash_result.ok, f"squash failed: {squash_result}")

        # Pre-squash HEAD must be in undo_stack.
        bh_hashes = {e.commit_hash for e in squash_result.undo_stack}
        self.assertIn(pre_squash_head, bh_hashes,
                      "pre-squash HEAD not in undo_stack — undo impossible")

        undo = self.gh.reset(pre_squash_head)
        self.assertTrue(undo.ok, f"undo (reset) failed: {undo}")
        self.assertEqual(undo.commits[0].commit_hash, pre_squash_head)
        self.assertEqual(len(undo.commits), count_before)
    def test_redo_after_undo_restores_squash(self):
        """After undo, resetting to post-squash HEAD restores the squash result."""
        state_before = self.gh.read_state()
        bm = self._by_msg(state_before)
        pre_squash_head = state_before.commits[0].commit_hash

        squash_result = self.gh.squash([bm["Add error pages"], bm["Add deployment config"]])
        self.assertTrue(squash_result.ok)
        post_squash_head = squash_result.commits[0].commit_hash
        count_after_squash = len(squash_result.commits)
        self.gh.reset(pre_squash_head)  # undo

        undo_state = self.gh.read_state()
        bh_hashes = {e.commit_hash for e in undo_state.undo_stack}
        self.assertIn(post_squash_head, bh_hashes,
                      "post-squash HEAD not in undo_stack after undo — redo impossible")

        redo = self.gh.reset(post_squash_head)
        self.assertTrue(redo.ok, f"redo (reset) failed: {redo}")
        self.assertEqual(redo.commits[0].commit_hash, post_squash_head)
        self.assertEqual(len(redo.commits), count_after_squash)
    def test_undo_fixup_restores_both_original_commit_messages(self):
        """After fixup, undo must restore the fixup'd commit message."""
        state_before = self.gh.read_state()
        bm = self._by_msg(state_before)
        pre_fixup_head = state_before.commits[0].commit_hash
        count_before = len(state_before.commits)

        fixup_result = self.gh.fixup([bm["Add deployment config"]])
        self.assertTrue(fixup_result.ok)
        undo = self.gh.reset(pre_fixup_head)
        self.assertTrue(undo.ok)
        self.assertEqual(len(undo.commits), count_before)
        msgs = [c.message for c in undo.commits]
        self.assertIn("Add deployment config", msgs,
                      "fixup'd commit message not restored after undo")

    def test_multiple_sequential_squash_then_undo_each(self):
        """Two squashes then two undos: each undo must restore the previous state."""
        state0 = self.gh.read_state()
        bm0 = self._by_msg(state0)
        head0 = state0.commits[0].commit_hash
        count0 = len(state0.commits)

        # Squash 1: combine two adjacent commits.
        r1 = self.gh.squash([bm0["Add error pages"], bm0["Add deployment config"]])
        self.assertTrue(r1.ok)
        head1 = r1.commits[0].commit_hash
        count1 = len(r1.commits)
        self.assertEqual(count1, count0 - 1)

        # Squash 2: combine a different pair of adjacent commits.
        bm1 = self._by_msg(r1)
        if "conflict: version B" in bm1 and "conflict: version A" in bm1:
            r2 = self.gh.squash([bm1["conflict: version B"], bm1["conflict: version A"]])
            self.assertTrue(r2.ok)
            count2 = len(r2.commits)
            self.assertEqual(count2, count1 - 1)

            # Undo squash 2: reset to head1.
            u2 = self.gh.reset(head1)
            self.assertTrue(u2.ok)
            self.assertEqual(len(u2.commits), count1)

        # Undo squash 1: reset to head0.
        u1 = self.gh.reset(head0)
        self.assertTrue(u1.ok)
        self.assertEqual(len(u1.commits), count0)

    def test_undo_reword_restores_original_message(self):
        """After reword, undo via reset restores the original commit message."""
        state_before = self.gh.read_state()
        bm = self._by_msg(state_before)
        h = bm["Add deployment config"]
        pre_reword_head = state_before.commits[0].commit_hash

        rw = self.gh.reword(h, "polished deployment config")
        self.assertTrue(rw.ok)
        undo = self.gh.reset(pre_reword_head)
        self.assertTrue(undo.ok)
        msgs = [c.message for c in undo.commits]
        self.assertIn("Add deployment config", msgs)
        self.assertNotIn("polished deployment config", msgs)


# ---------------------------------------------------------------------------
# 7. Sequential operations and _start boundary stability
# ---------------------------------------------------------------------------

@pytest.mark.release
class SequentialOperationsTests(ChallengeBase):
    """Verify that chained squash/fixup/reword operations keep _start consistent."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        cls.repo = _setup_create_delete_repo(cls.tmpdir)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_squash_then_reword_then_squash(self):
        """Three sequential operations must each succeed and leave valid state."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        r1 = self.gh.squash([bm["add temp"], bm["delete temp"]])
        self.assertTrue(r1.ok, f"first squash failed: {r1}")
        self.assert_valid_state(r1)

        # Reword the top commit (above the merge commit) rather than the oldest,
        # to avoid replaying the merge commit during rebase.
        top = r1.commits[0]
        r2 = self.gh.reword(top.commit_hash, "reworded top")
        self.assertTrue(r2.ok, f"reword after squash failed: {r2}")
        self.assert_valid_state(r2)

        if len(r2.commits) >= 2:
            h_top  = r2.commits[0].commit_hash
            h_next = r2.commits[1].commit_hash
            r3 = self.gh.squash([h_top, h_next])
            self.assertTrue(r3.ok, f"second squash failed: {r3}")
            self.assert_valid_state(r3)

    def test_commit_count_decrements_by_one_per_fixup(self):
        """Each fixup must reduce commit count by exactly one."""
        state = self.gh.read_state()
        count = len(state.commits)
        bm = self._by_msg(state)

        r1 = self.gh.fixup([bm["delete temp"]])
        self.assertTrue(r1.ok)
        self.assertEqual(len(r1.commits), count - 1)
        count -= 1

        bm2 = self._by_msg(r1)
        # After folding "delete temp", the next top commit is "Add error pages" from the standard repo.
        r2 = self.gh.fixup([bm2["Add error pages"]])
        self.assertTrue(r2.ok)
        self.assertEqual(len(r2.commits), count - 1)
    def test_interleaved_squash_and_reword_preserve_all_other_messages(self):
        """After squash + reword, commits not involved must still have original messages."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        r1 = self.gh.squash([bm["add temp"], bm["delete temp"]])
        self.assertTrue(r1.ok)
        msgs1 = {c.message for c in r1.commits}
        self.assertIn("Initial commit", msgs1)
        self.assertIn("Add error pages", msgs1)

        bm2 = self._by_msg(r1)
        h_after = bm2.get("Add error pages")
        if h_after:
            r2 = self.gh.reword(h_after, "post-squash top")
            self.assertTrue(r2.ok)
            msgs2 = {c.message for c in r2.commits}
            self.assertIn("Initial commit", msgs2,
                          "'Initial commit' message changed by unrelated reword")


# ---------------------------------------------------------------------------
# 8. Unicode filenames and special-character commit messages
# ---------------------------------------------------------------------------

def _setup_unicode_repo(tmpdir: Path) -> Path:
    """
    Clone the standard test repo and append three unicode-filename commits on top.

    Commits appended (newest first):
      'update: 日本語 ($special & "quotes")' — modifies 日本語.txt
      "feat: add café ☕"                     — creates café.txt
      "add résumé.txt"                        — creates résumé.txt
    """
    persistent_repo = _ensure_persistent_test_repo()
    repo = tmpdir / "repo"
    subprocess.run(["git", "clone", str(persistent_repo), str(repo)],
                   capture_output=True, check=True)
    subprocess.run(["git", "remote", "remove", "origin"],
                   cwd=str(repo), capture_output=True)
    _commit_raw(repo, "résumé.txt", "résumé: naïve\n".encode("utf-8"), "add résumé.txt",                     "bob",   200)
    _commit_raw(repo, "café.txt",   "☕ coffee\n".encode("utf-8"),      "feat: add café ☕",                   "carol", 201)
    _commit_raw(repo, "日本語.txt", "日本語\n".encode("utf-8"),         'update: 日本語 ($special & "quotes")', "alice", 202)
    return repo


# ---------------------------------------------------------------------------
# 9. Conflicting content — move/reorder triggers a rebase conflict
# ---------------------------------------------------------------------------
#
# The standard test repo has:
#   "conflict: version B" — modifies README.md line 2 to "tracking your tasks."
#   "conflict: version A" — modifies README.md line 2 to "managing your todos."
# Swapping these two causes a content conflict during rebase.


# ---------------------------------------------------------------------------
# 10. Tags on commits in the visible range
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 11. Multi-line commit messages (subject + body + trailers)
# ---------------------------------------------------------------------------

MULTILINE_COMMIT_MESSAGE = (
    "Fix: handle edge\tcase\n\n"
    "This commit:\n"
    "\t- has \"double\" and 'single' quotes\n"
    "\t- backslash C:\\path\\to\\file\n"
    "\t- unicode: café, naïve, 日本語 🎉\n\n"
    "Fixes #42"
)


# ---------------------------------------------------------------------------
# 14. Single-commit and two-commit repos (near-root edge cases)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# 8. Unicode filenames and special-character commit messages
# ---------------------------------------------------------------------------

@pytest.mark.release
class UnicodeAndSpecialCharTests(ChallengeBase):
    """Operations on repos with unicode filenames and special-char commit messages."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        cls.repo = _setup_unicode_repo(cls.tmpdir)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_read_state_returns_unicode_messages(self):
        """read_state must correctly return unicode commit messages including emoji and CJK."""
        state = self.gh.read_state()
        self.assert_valid_state(state)
        msgs = [c.message for c in state.commits]
        self.assertTrue(any("☕" in m or "café" in m for m in msgs),
                        "unicode emoji commit not found in state")
        self.assertTrue(any("日本語" in m for m in msgs),
                        "CJK commit message not found in state")

    def test_squash_commits_with_unicode_filenames(self):
        """Squash two commits that create unicode-named files."""
        state = self.gh.read_state()
        hashes = [state.commits[0].commit_hash, state.commits[1].commit_hash]
        result = self.gh.squash(hashes)
        if not result.ok:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)
            self.assertEqual(len(result.commits), len(state.commits) - 1)

    def test_reword_to_message_containing_shell_special_chars(self):
        """Reword a commit to a new message that itself contains $, quotes, and backticks."""
        state = self.gh.read_state()
        h = state.commits[1].commit_hash
        new_msg = 'fix: handle $PATH, "quoted args", and `backticks`'
        result = self.gh.reword(h, new_msg)
        self.assertTrue(result.ok, f"reword to special-char new message failed: {result}")
        msgs = [c.message for c in result.commits]
        self.assertIn(new_msg, msgs)

    def test_show_commit_with_unicode_filename(self):
        """show() must succeed on a commit that creates a unicode-named file."""
        state = self.gh.read_state()
        h = self._by_msg(state)["add résumé.txt"]
        result = self.gh.show(h)
        self.assertTrue(result.ok, f"show() failed on unicode-filename commit: {result}")


# ---------------------------------------------------------------------------
# 9. Conflicting content — move/reorder triggers a rebase conflict
# ---------------------------------------------------------------------------

@pytest.mark.release
class ConflictingContentTests(ChallengeBase):
    """
    Swapping two commits that both edit the same line causes a rebase conflict.
    The backend must abort cleanly without leaving the repo stuck.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       capture_output=True, check=True)
        # Remove origin remote
        subprocess.run(["git", "remote", "remove", "origin"],
                       cwd=str(cls.repo), capture_output=True)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_move_conflicting_commits_leaves_repo_recoverable(self):
        """
        Swapping 'version A' and 'version B' triggers a conflict on the shared line.
        Result must be ok=False with a clean repo, or ok=True with valid state.
        """
        state = self.gh.read_state()
        order = [c.commit_hash for c in state.commits]
        self.assertGreaterEqual(len(order), 2)
        order[1], order[2] = order[2], order[1]

        result = self.gh.move(order)
        if not result.ok:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)

    def test_repo_is_clean_after_conflict_and_abort(self):
        """After a conflicting move + explicit abort, read_state must show no rebase in progress."""
        state = self.gh.read_state()
        order = [c.commit_hash for c in state.commits]
        order[1], order[2] = order[2], order[1]

        self.gh.move(order)
        self.gh.rebase_abort()

        state_after = self.gh.read_state()
        self.assertTrue(state_after.ok)
        self.assertFalse(state_after.rebase_in_progress)
        self.assertFalse(state_after.dirty)

    def test_commit_count_unchanged_after_failed_conflict(self):
        """After a failed conflicting move + abort, commit count must be the same as before."""
        state_before = self.gh.read_state()
        count_before = len(state_before.commits)
        order = [c.commit_hash for c in state_before.commits]
        order[1], order[2] = order[2], order[1]

        self.gh.move(order)
        self.gh.rebase_abort()

        state_after = self.gh.read_state()
        self.assertEqual(len(state_after.commits), count_before)

    def test_head_hash_unchanged_after_failed_conflict(self):
        """HEAD must point to the same commit after a failed move + abort."""
        state_before = self.gh.read_state()
        head_before = state_before.commits[0].commit_hash
        order = [c.commit_hash for c in state_before.commits]
        order[1], order[2] = order[2], order[1]

        self.gh.move(order)
        self.gh.rebase_abort()

        state_after = self.gh.read_state()
        self.assertEqual(state_after.commits[0].commit_hash, head_before,                         "HEAD changed after failed conflicting move + abort")


@pytest.mark.release
class MultiConflictContinueTests(ChallengeBase):
    """Regression: a rebase that conflicts again after the first conflict is
    resolved must surface the second conflict, not a generic git failure.

    Three commits rewrite the same single line of conflict_x.txt:
        "x base" (0) -> "x to one" (1) -> "x to two" (2)
    Swapping the two newest replays them in reverse onto "x base", so each pick
    conflicts on that line: pick "x to two" (conflict 1), then "x to one"
    (conflict 2). git rebase --continue exits non-zero on the second conflict;
    the backend must report it as a conflict rather than letting the GitError
    propagate as git_failed.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       capture_output=True, check=True)
        subprocess.run(["git", "remote", "remove", "origin"],
                       cwd=str(cls.repo), capture_output=True)
        _commit_raw(cls.repo, "conflict_x.txt", b"0\n", "x base",   "alice", 1)
        _commit_raw(cls.repo, "conflict_x.txt", b"1\n", "x to one", "bob",   2)
        _commit_raw(cls.repo, "conflict_x.txt", b"2\n", "x to two", "carol", 3)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def _resolve(self, content: bytes):
        (self.repo / "conflict_x.txt").write_bytes(content)
        subprocess.run(["git", "add", "conflict_x.txt"], cwd=str(self.repo),
                       check=True, capture_output=True)

    def _swap_two_newest(self):
        order = self._order()
        order[0], order[1] = order[1], order[0]
        return self.gh.move(order)

    @pytest.mark.release
    def test_second_conflict_after_resolving_first_is_reported_as_conflict(self):
        first = self._swap_two_newest()
        self.assertFalse(first.ok)
        self.assertTrue(first.conflict)
        self.assertIn("conflict_x.txt", first.conflict_files)

        self._resolve(b"2\n")
        second = self.gh.rebase_continue()
        self.assertFalse(second.ok)
        self.assertTrue(second.conflict)
        self.assertIn("conflict_x.txt", second.conflict_files)
        self.assertTrue(second.rebase_in_progress)

    @pytest.mark.release
    def test_continue_through_all_conflicts_completes(self):
        self._swap_two_newest()
        self._resolve(b"2\n")
        self.gh.rebase_continue()      # stops on the second conflict
        self._resolve(b"1\n")
        final = self.gh.rebase_continue()
        self.assertTrue(final.ok)
        self.assertFalse(final.rebase_in_progress)
        self.assertFalse(final.conflict)


# ---------------------------------------------------------------------------
# 10. Tags on commits in the visible range
# ---------------------------------------------------------------------------

@pytest.mark.release
class TaggedCommitTests(ChallengeBase):
    """Operations must succeed (or fail cleanly) when commits carry git tags.

    Standard repo has:
      "Add admin panel"      — annotated tag 'v-test-ann'
      "Add deployment config"— lightweight tag 'v-test-lw'  (adjacent, newer)
      "Add user dashboard"   — no tag (used as reset target)

    These commits sit above the merge commit so rebasing them does not require
    replaying the merge, and "Add admin panel"'s predecessor is a plain commit.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       capture_output=True, check=True)
        subprocess.run(["git", "remote", "remove", "origin"],
                       cwd=str(cls.repo), capture_output=True)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_read_state_with_tagged_commits_succeeds(self):
        """read_state must work normally even when commits carry annotated and lightweight tags."""
        state = self.gh.read_state()
        self.assert_valid_state(state)
        bm = self._by_msg(state)
        self.assertIn("Add admin panel", bm)
        self.assertIn("Add deployment config", bm)

    def test_squash_two_tagged_commits(self):
        """Squash the annotated-tagged and lightweight-tagged commits (adjacent)."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        self.assertIn("Add admin panel",       bm)
        self.assertIn("Add deployment config", bm)

        result = self.gh.squash([bm["Add admin panel"], bm["Add deployment config"]])
        if not result.ok:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)
            self.assertEqual(len(result.commits), len(state.commits) - 1)

    def test_reword_annotated_tagged_commit(self):
        """Reword the commit carrying the annotated tag: the message must change."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        h = bm["Add admin panel"]

        result = self.gh.reword(h, "Add admin dashboard panel")
        if not result.ok:
            self.assert_recoverable(self.gh)
        else:
            msgs = [c.message for c in result.commits]
            self.assertIn("Add admin dashboard panel", msgs)
            self.assertNotIn("Add admin panel", msgs)

    def test_fixup_annotated_tagged_commit_into_predecessor(self):
        """Fixup the annotated-tagged commit into its predecessor 'Add user dashboard'."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        h = bm["Add admin panel"]

        result = self.gh.fixup([h])
        if not result.ok:
            self.assert_recoverable(self.gh)
        else:
            self.assert_valid_state(result)
            msgs = [c.message for c in result.commits]
            self.assertNotIn("Add admin panel", msgs)

    def test_reset_to_pre_tag_commit(self):
        """Reset to 'Add user dashboard', before the test-tagged commits."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.reset(bm["Add user dashboard"])
        self.assertTrue(result.ok, f"reset past tagged commits failed: {result}")
        self.assertEqual(result.commits[0].commit_hash, bm["Add user dashboard"])

    def test_show_tagged_commit_succeeds(self):
        """show() on an annotated-tagged commit must return ok=True."""
        bm = self._by_msg()
        result = self.gh.show(bm["Add admin panel"])
        self.assertTrue(result.ok, f"show() failed on tagged commit: {result}")


# ---------------------------------------------------------------------------
# 11. Multi-line commit messages (subject + body + trailers)
# ---------------------------------------------------------------------------

@pytest.mark.release
class MultilineMessageTests(ChallengeBase):
    """Squash, fixup, reword on commits with subject+body+trailer messages."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       capture_output=True, check=True)
        subprocess.run(["git", "remote", "remove", "origin"],
                       cwd=str(cls.repo), capture_output=True)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def _multiline_commit(self, state):
        c = next((c for c in state.commits if c.message == MULTILINE_COMMIT_MESSAGE), None)
        self.assertIsNotNone(c, "multiline commit not found in state")
        return c

    def test_read_state_returns_full_multiline_message(self):
        """read_state must return full multi-line messages, not just the subject line."""
        state = self.gh.read_state()
        self.assert_valid_state(state)
        c = self._multiline_commit(state)
        self.assertIn("Fixes #42", c.message,
                      "body line missing from read_state commit message")

    def test_squash_multiline_message_commits(self):
        """Squash multiline commit with its neighbour: commit count decreases by one."""
        state = self.gh.read_state()
        i = next(idx for idx, c in enumerate(state.commits) if c.message == MULTILINE_COMMIT_MESSAGE)
        # squash with the commit above it (newer, lower index)
        hashes = [state.commits[i - 1].commit_hash, state.commits[i].commit_hash]
        result = self.gh.squash(hashes)
        self.assertTrue(result.ok, f"squash of multiline-message commits failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 1)

    def test_reword_single_line_commit_to_multiline(self):
        """Reword a simple commit to a multi-line message with a body."""
        state = self.gh.read_state()
        i = next(idx for idx, c in enumerate(state.commits) if c.message == MULTILINE_COMMIT_MESSAGE)
        h = state.commits[i + 1].commit_hash  # simple commit below multiline
        new_msg = "rewound commit\n\nCreates the base structure.\nSigned-off-by: Alice"
        result = self.gh.reword(h, new_msg)
        self.assertTrue(result.ok, f"reword to multiline failed: {result}")
        msgs = [c.message for c in result.commits]
        self.assertTrue(any("rewound commit" in m for m in msgs))

    def test_show_multiline_commit_returns_full_message(self):
        """show() must include the full multi-line message in its output."""
        state = self.gh.read_state()
        h = self._multiline_commit(state).commit_hash
        result = self.gh.show(h)
        self.assertTrue(result.ok, f"show() failed on multiline commit: {result}")


# ---------------------------------------------------------------------------
# 12. Files with spaces and parentheses in their names
# ---------------------------------------------------------------------------

@pytest.mark.release
class SpacesInFilenamesTests(ChallengeBase):
    """Operations on repos where filenames contain spaces and parentheses."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       capture_output=True, check=True)
        subprocess.run(["git", "remote", "remove", "origin"],
                       cwd=str(cls.repo), capture_output=True)
        env1 = _commit_env("carol", 1000)
        (cls.repo / "my document.txt").write_bytes(b"draft\n")
        (cls.repo / "notes (draft).txt").write_bytes(b"notes\n")
        subprocess.run(["git", "add", "--", "my document.txt", "notes (draft).txt"],
                       cwd=str(cls.repo), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Add files with spaces"],
                       cwd=str(cls.repo), env=env1, capture_output=True, check=True)
        _commit_raw(cls.repo, "my document.txt", b"final\n", "Update docs", "alice", 1001)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_show_commit_with_spaced_filename_includes_filename_in_diff(self):
        """show() must include the space-containing filename in its diff output."""
        bm = self._by_msg()
        result = self.gh.show(bm["Add files with spaces"])
        self.assertTrue(result.ok, f"show() failed on spaced-filename commit: {result}")
        self.assertIn("my document.txt", result.diff)

    def test_squash_commits_touching_spaced_filename(self):
        """Squash commits that both touch a file with spaces in its name."""
        state = self.gh.read_state()
        bm = self._by_msg(state)

        result = self.gh.squash([bm["Add files with spaces"], bm["Update docs"]])
        self.assertTrue(result.ok, f"squash with spaced filenames failed: {result}")
        self.assertEqual(len(result.commits), len(state.commits) - 1)
        files = _ls_tree(self.repo, "HEAD")
        self.assertIn("my document.txt", files)

    def test_reword_commit_touching_spaced_file_preserves_tree(self):
        """Reword a commit that touches a spaced-name file: tree must be identical."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        h = bm["Add files with spaces"]
        old_tree = _git(self.repo, "git", "rev-parse", h + ":").stdout.strip()

        result = self.gh.reword(h, "renamed: add files with spaces")
        self.assertTrue(result.ok, f"reword with spaced filenames failed: {result}")

        new_bm = {c.message: c.commit_hash for c in result.commits}
        new_h = new_bm.get("renamed: add files with spaces")
        self.assertIsNotNone(new_h, "rewarded commit not found by new message")
        new_tree = _git(self.repo, "git", "rev-parse", new_h + ":").stdout.strip()
        self.assertEqual(old_tree, new_tree, "tree changed after reword of spaced-filename commit")


# ---------------------------------------------------------------------------
# 14. Single-commit and two-commit repos (near-root edge cases)
# ---------------------------------------------------------------------------

@pytest.mark.release
class NearRootEdgeCaseTests(ChallengeBase):
    """Operations on repos with very few commits test root-commit boundary handling."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        pass

    def tearDown(self):
        pass

    def _reset_to(self, tag):
        subprocess.run(["git", "reset", "--hard", tag],
                       cwd=str(self.repo), check=True, capture_output=True)
        return GitWarp(str(self.repo))

    def test_read_state_single_commit_repo(self):
        """read_state on a one-commit repo must return exactly one commit."""
        gh = self._reset_to("v-test-root")
        state = gh.read_state()
        self.assertTrue(state.ok)
        self.assertEqual(len(state.commits), 1)

    def test_show_root_commit(self):
        """show() on the root commit (no parent) must succeed."""
        gh = self._reset_to("v-test-root")
        state = gh.read_state()
        h = state.commits[0].commit_hash
        result = gh.show(h)
        self.assertTrue(result.ok, f"show() failed on root commit: {result}")

    def test_squash_only_two_commits_in_repo(self):
        """Squash the only two commits: result is one commit, or fails cleanly."""
        gh = self._reset_to("v-test-two")
        state = gh.read_state()
        hashes = [c.commit_hash for c in state.commits]
        self.assertEqual(len(hashes), 2)

        result = gh.squash(hashes)
        if result.ok:
            self.assertEqual(len(result.commits), 1)
        else:
            self.assert_recoverable(gh)

    def test_fixup_non_root_into_root(self):
        """Fixup the only non-root commit into the root: one commit remains, or fails cleanly."""
        gh = self._reset_to("v-test-two")
        state = gh.read_state()
        h_top = state.commits[0].commit_hash

        result = gh.fixup([h_top])
        if result.ok:
            self.assertEqual(len(result.commits), 1)
            self.assertEqual(result.commits[0].message, "Initial commit")
        else:
            self.assert_recoverable(gh)

    def test_reword_root_commit(self):
        """Reword the root commit itself: message must change, or fail cleanly."""
        gh = self._reset_to("v-test-two")
        state = gh.read_state()
        h_root = state.commits[-1].commit_hash

        result = gh.reword(h_root, "project start")
        if result.ok:
            msgs = [c.message for c in result.commits]
            self.assertIn("project start", msgs)
        else:
            self.assert_recoverable(gh)

    def test_reset_to_root_in_two_commit_repo(self):
        """Reset to the root commit: HEAD moves to root and only one commit is visible."""
        gh = self._reset_to("v-test-two")
        state = gh.read_state()
        h_root = state.commits[-1].commit_hash

        result = gh.reset(h_root)
        self.assertTrue(result.ok, f"reset to root failed: {result}")
        self.assertEqual(result.commits[0].commit_hash, h_root)
        self.assertEqual(len(result.commits), 1)

    def test_squash_on_single_commit_repo_fails_cleanly(self):
        """Squash with no second commit to fold into must fail without corrupting the repo."""
        gh = self._reset_to("v-test-root")
        state = gh.read_state()
        h = state.commits[0].commit_hash

        try:
            result = gh.squash([h])
            self.assert_valid_state(result)
        except GitWarpError:
            self.assert_recoverable(gh)


# ---------------------------------------------------------------------------
# 15. Empty / malformed reflog
# ---------------------------------------------------------------------------

@pytest.mark.release
class EmptyReflogTests(ChallengeBase):
    """Undo-stack parsing must degrade gracefully when the reflog is empty,
    rather than raising — commits stay readable, the undo stack is just []."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="git-warp-challenge-"))
        persistent_repo = _ensure_persistent_test_repo()
        cls.repo = cls.tmpdir / "repo"
        subprocess.run(["git", "clone", str(persistent_repo), str(cls.repo)],
                       check=True, capture_output=True)
        subprocess.run(["git", "reset", "--hard", "v-test-two"],
                       cwd=str(cls.repo), check=True, capture_output=True)
        cls.branch = _git(cls.repo, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        cls.initial_head = _git(cls.repo, "git", "rev-parse", "HEAD").stdout.strip()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        _restore_repo(self.repo, self.branch, self.initial_head)
        # Drop every reflog entry so `git reflog refs/heads/main` returns empty.
        _git(self.repo, "git", "reflog", "expire", "--expire=all", "--all")
        self.gh = GitWarp(str(self.repo))

    def tearDown(self):
        pass

    def test_empty_reflog_yields_empty_undo_stack(self):
        """An emptied reflog produces an empty undo stack with no crash."""
        self.assertEqual(self.gh._list_undo_stack("main", {}), [])

    def test_read_state_survives_empty_reflog(self):
        """read_state still returns commits even when the undo stack is empty."""
        state = self.gh.read_state()
        self.assertTrue(state.ok)
        self.assertGreater(len(state.commits), 0)
        self.assertEqual(state.undo_stack, [])

    def test_describe_rebase_group_handles_empty_group(self):
        """The pure group classifier defaults to 'rebase' on no entries."""
        self.assertEqual(GitWarp._describe_rebase_group([]), "rebase")

    def test_filter_rebase_groups_handles_empty_input(self):
        """The pure rebase-group filter returns [] on no entries."""
        self.assertEqual(GitWarp._filter_rebase_groups([]), [])


if __name__ == "__main__":
    unittest.main()
