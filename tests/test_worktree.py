"""
Unit tests for git worktree support.

Tests the GitHistory class when used with git worktrees (created via `git worktree add`).
This exposes bugs in the backend's handling of worktree-specific file structure.

In a worktree:
- .git is a FILE (not a directory) containing "gitdir: /path/to/real/.git/worktrees/name"
- The rebase state directories are in the real git dir, not in the worktree root
- Various path checks that assume .git is a directory will fail

Run with:
    python -m pytest tests/test_worktree.py -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from git_history import GitHistory


class WorktreeTest(unittest.TestCase):
    """Base class for worktree tests."""

    def setUp(self):
        """Create a main repo and a worktree."""
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-worktree-"))
        self.main_repo = self.tmpdir / "main"
        self.worktree_path = self.tmpdir / "worktree"

        # Create main repo with a few commits
        self._init_repo(self.main_repo)
        self._make_commit(self.main_repo, "file1.txt", b"content1", "Initial commit")
        self._make_commit(self.main_repo, "file1.txt", b"content2", "Second commit")
        self._make_commit(self.main_repo, "file1.txt", b"content3", "Third commit")

        self.main_branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(self.main_repo),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # Create a worktree (detached HEAD at current commit)
        subprocess.run(
            ["git", "worktree", "add", str(self.worktree_path), "HEAD"],
            cwd=str(self.main_repo),
            check=True,
            capture_output=True,
        )

        self.gh = GitHistory(str(self.worktree_path))

    def tearDown(self):
        """Clean up worktree and temp dirs."""
        # Remove the worktree
        subprocess.run(
            ["git", "worktree", "remove", str(self.worktree_path)],
            cwd=str(self.main_repo),
            check=True,
            capture_output=True,
        )
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _init_repo(self, repo_path):
        """Initialize a git repo."""
        repo_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
        )

    def _make_commit(self, repo_path, filename, content, message):
        """Create a commit in a repo."""
        (repo_path / filename).write_bytes(content)
        subprocess.run(
            ["git", "add", filename],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
        )


class WorktreeStructureTests(WorktreeTest):
    """Test that worktree file structure is recognized correctly."""

    def test_worktree_has_git_file_not_directory(self):
        """Verify that .git is a file in a worktree, not a directory."""
        git_path = self.worktree_path / ".git"
        self.assertTrue(git_path.exists(), ".git should exist in worktree")
        self.assertTrue(git_path.is_file(), ".git should be a file in worktree")
        self.assertFalse(git_path.is_dir(), ".git should NOT be a directory in worktree")

        # Verify it contains gitdir reference
        git_file_content = git_path.read_text()
        self.assertIn("gitdir:", git_file_content)

    def test_read_state_in_worktree(self):
        """Test that read_state() works in a worktree."""
        state = self.gh.read_state()
        self.assertTrue(state["ok"])
        self.assertEqual(len(state["commits"]), 3)


class WorktreeRebaseTests(WorktreeTest):
    """Test rebase detection in worktrees (exposes the main bug)."""

    def test_in_rebase_detects_rebase_in_progress(self):
        """Test that _in_rebase() correctly detects a rebase in progress in a worktree.

        This test EXPOSES THE BUG: The current implementation checks if .git/rebase-merge
        exists, but in a worktree .git is a file, not a directory. This check will
        always return False even when a rebase is actually in progress.
        """
        # Start a rebase that will have conflicts
        # First, modify file2 on the main branch
        self._make_commit(self.main_repo, "file2.txt", b"main branch", "Main branch change")

        # Then in the worktree, make a conflicting change
        self._make_commit(self.worktree_path, "file2.txt", b"worktree change", "Worktree change")

        # Now try to rebase the worktree onto main branch
        # This should create a rebase state
        rebase_result = subprocess.run(
            ["git", "rebase", self.main_branch],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
        )

        # The rebase should fail due to conflicts, putting us in rebase state
        if rebase_result.returncode != 0 and "conflict" in rebase_result.stdout.lower() + rebase_result.stderr.lower():
            # We're in a rebase conflict state
            # Now test if _in_rebase() detects it
            in_rebase = self.gh._in_rebase()
            self.assertTrue(in_rebase,
                          "_in_rebase() should return True when a rebase is in progress in a worktree")

            # Clean up the rebase
            subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=str(self.worktree_path),
                check=True,
                capture_output=True,
            )

    def test_state_reports_rebase_in_progress_correctly(self):
        """Test that read_state() reports rebase_in_progress correctly in a worktree.

        This also exposes the bug since read_state() relies on _in_rebase().
        """
        # Create a scenario where rebase will conflict
        # Add a change to main that will conflict with worktree
        (self.main_repo / "conflict.txt").write_bytes(b"main content\n")
        subprocess.run(
            ["git", "add", "conflict.txt"],
            cwd=str(self.main_repo),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add conflict.txt in main"],
            cwd=str(self.main_repo),
            check=True,
            capture_output=True,
        )

        # In worktree, add conflicting change
        (self.worktree_path / "conflict.txt").write_bytes(b"worktree content\n")
        subprocess.run(
            ["git", "add", "conflict.txt"],
            cwd=str(self.worktree_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Add conflict.txt in worktree"],
            cwd=str(self.worktree_path),
            check=True,
            capture_output=True,
        )

        rebase_result = subprocess.run(
            ["git", "rebase", self.main_branch],
            cwd=str(self.worktree_path),
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(rebase_result.returncode, 0, "Rebase should have failed due to conflicts")

        state = self.gh.read_state()
        self.assertTrue(state["ok"])
        self.assertTrue(state["rebase_in_progress"],
                      "read_state() should report rebase_in_progress=True when a rebase is in progress in a worktree")

        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(self.worktree_path),
            check=True,
            capture_output=True,
        )


class WorktreeOperationTests(WorktreeTest):
    """Test that git operations work correctly in worktrees."""

    def test_rebase_operation_in_worktree(self):
        """Test that rebase operations work in a worktree."""
        state = self.gh.read_state()
        commits = state["commits"]

        # Try to reorder commits (simplified rebase)
        # For now, just verify that we can attempt it without crashing
        if len(commits) >= 2:
            hashes = [c["hash"] for c in commits]
            # Try to reorder the last two commits
            result = self.gh.rebase("move", hashes=[hashes[1]], order=[0, 1])
            # We expect this might fail due to conflicts or other issues,
            # but it shouldn't crash with path errors
            self.assertIn("ok", result)

    def test_reset_in_worktree(self):
        """Test that reset works in a worktree."""
        state = self.gh.read_state()
        commits = state["commits"]

        if len(commits) >= 2:
            # Reset to the second newest commit
            target_hash = commits[1]["hash"]
            result = self.gh.reset(target_hash)
            self.assertTrue(result["ok"])

            # Verify we're at that commit
            new_state = self.gh.read_state()
            self.assertEqual(new_state["commits"][0]["hash"], target_hash)

    def test_show_in_worktree(self):
        """Test that show works in a worktree."""
        state = self.gh.read_state()
        commit_hash = state["commits"][0]["hash"]

        result = self.gh.show(commit_hash)
        self.assertTrue(result["ok"])
        self.assertIn("info", result)
        self.assertIn("diff", result)


if __name__ == "__main__":
    unittest.main()
