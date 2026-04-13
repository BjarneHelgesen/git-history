import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from make_test_repo import init_repo
from conftest import _commit_raw
from git_history import GitHistory
from test_challenging import ChallengeBase, _git, _commit_env


def _build_submodule_repo(parent: Path) -> Path:
    """
    Repo with commits that update a submodule pointer.

    Commits (newest first):
      "update main"        — touches main.txt
      "update sub to v2"   — updates lib submodule gitlink
      "add submodule"      — adds lib submodule pinned at v1
      "initial"            — creates main.txt
    """
    upstream = parent / "sub-upstream"
    upstream.mkdir()
    init_repo(upstream)
    _commit_raw(upstream, "lib.py", b"# v1\n", "lib v1", "alice", 0)
    sub_v1 = _git(upstream, "git", "rev-parse", "HEAD").stdout.strip()
    _commit_raw(upstream, "lib.py", b"# v2\n", "lib v2", "alice", 1)
    sub_v2 = _git(upstream, "git", "rev-parse", "HEAD").stdout.strip()

    repo = parent / "repo"
    repo.mkdir()
    init_repo(repo)
    _commit_raw(repo, "main.txt", b"main\n", "initial", "alice", 2)

    env3 = _commit_env("alice", 3)
    subprocess.run(
        ["git", "submodule", "add", "--quiet", str(upstream), "lib"],
        cwd=str(repo), env=env3, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "-C", str(repo / "lib"), "checkout", "--quiet", sub_v1],
        capture_output=True, check=True
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add submodule"],
        cwd=str(repo), env=env3, capture_output=True, check=True
    )

    env4 = _commit_env("bob", 4)
    subprocess.run(
        ["git", "-C", str(repo / "lib"), "checkout", "--quiet", sub_v2],
        capture_output=True, check=True
    )
    subprocess.run(["git", "add", "lib"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "update sub to v2"],
        cwd=str(repo), env=env4, capture_output=True, check=True
    )

    _commit_raw(repo, "main.txt", b"main v2\n", "update main", "carol", 5)
    return repo


class SubmoduleRepoTests(ChallengeBase):
    """Squash, fixup, and reword on repos that contain submodule pointer changes."""

    def setUp(self):
        super().setUp()
        self.repo = _build_submodule_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def test_squash_add_submodule_and_update_submodule(self):
        """Squash the two submodule-touching commits into one."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        self.assertIn("add submodule",   bm)
        self.assertIn("update sub to v2", bm)

        result = self.gh.rebase(
            "squash",
            hashes=[bm["add submodule"], bm["update sub to v2"]]
        )
        self.assertTrue(result["ok"], f"squash of submodule commits failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        # Submodule gitlink must still exist in HEAD tree.
        r = _git(self.repo, "git", "ls-tree", "HEAD", "lib")
        self.assertIn("commit", r.stdout,
                      "submodule gitlink missing from HEAD after squash")

    def test_fixup_submodule_update_into_add_submodule(self):
        """Fixup 'update sub to v2' into its predecessor."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        self.assertIn("update sub to v2", bm)

        result = self.gh.rebase("fixup", hashes=[bm["update sub to v2"]])
        self.assertTrue(result["ok"], f"fixup of submodule commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in result["commits"]]
        self.assertNotIn("update sub to v2", msgs)

    def test_reword_commit_with_submodule_change_preserves_tree(self):
        """Reword must not alter the tree of the rewarded commit."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        h = bm["add submodule"]
        old_tree = _git(self.repo, "git", "rev-parse", h + ":").stdout.strip()

        result = self.gh.rebase("reword", hashes=[h], new_message="introduce lib submodule")
        self.assertTrue(result["ok"], f"reword failed: {result}")

        new_h = self._by_msg(result).get("introduce lib submodule")
        self.assertIsNotNone(new_h, "rewarded commit not found in result")
        new_tree = _git(self.repo, "git", "rev-parse", new_h + ":").stdout.strip()
        self.assertEqual(old_tree, new_tree,
                         "tree changed after reword of submodule commit")

    def test_reset_to_before_submodule_was_added(self):
        """Reset to 'initial' must remove .gitmodules from the working tree."""
        bm = self._by_msg()
        result = self.gh.reset(bm["initial"])
        self.assertTrue(result["ok"], f"reset to pre-submodule commit failed: {result}")
        self.assertEqual(result["commits"][0]["hash"], bm["initial"])
        r = _git(self.repo, "git", "ls-tree", "HEAD", ".gitmodules")
        self.assertEqual(r.stdout.strip(), "",
                         ".gitmodules still present after reset to pre-submodule commit")

    def test_squash_submodule_commit_with_unrelated_commit(self):
        """Squash 'update sub to v2' with the unrelated 'update main' commit."""
        state = self.gh.read_state()
        bm = self._by_msg(state)
        self.assertIn("update sub to v2", bm)
        self.assertIn("update main", bm)

        result = self.gh.rebase(
            "squash",
            hashes=[bm["update sub to v2"], bm["update main"]]
        )
        self.assertTrue(result["ok"], f"squash failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)


if __name__ == "__main__":
    unittest.main()
