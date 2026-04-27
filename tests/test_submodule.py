"""
Tests for submodule-aware reset and rebase-move guards.

Repo layout (newest → oldest):
  "update main"  — changes main.txt; .gitmodules and gitlink unchanged
  "bump lib"     — advances lib gitlink v1→v2; .gitmodules file unchanged
  "add lib"      — creates .gitmodules, adds lib submodule at gitlink v1
  "base"         — initial main.txt; no submodules at all

Key distinctions exercised:
  - "add lib" touches .gitmodules (file created)
  - "bump lib" does NOT touch .gitmodules — only the gitlink (mode 160000 entry)
  - "update main" touches neither
  - "base" has no .gitmodules
"""
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


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _build_submodule_repo(parent: Path):
    """
    Build the four-commit submodule repo.
    Returns (repo_path, lib_v1_hash, lib_v2_hash).
    """
    upstream = parent / "lib-upstream"
    upstream.mkdir()
    init_repo(upstream)
    _commit_raw(upstream, "lib.py", b"# v1\n", "lib v1", "alice", 0)
    v1 = _git(upstream, "git", "rev-parse", "HEAD").stdout.strip()
    _commit_raw(upstream, "lib.py", b"# v2\n", "lib v2", "alice", 1)
    v2 = _git(upstream, "git", "rev-parse", "HEAD").stdout.strip()

    repo = parent / "repo"
    repo.mkdir()
    init_repo(repo)
    _commit_raw(repo, "main.txt", b"base\n", "base", "alice", 2)

    env3 = _commit_env("alice", 3)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always",
         "submodule", "add", "--quiet", str(upstream), "lib"],
        cwd=str(repo), env=env3, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo / "lib"), "checkout", "--quiet", v1],
        capture_output=True, check=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add lib"],
                   cwd=str(repo), env=env3, capture_output=True, check=True)

    env4 = _commit_env("bob", 4)
    subprocess.run(
        ["git", "-C", str(repo / "lib"), "checkout", "--quiet", v2],
        capture_output=True, check=True,
    )
    subprocess.run(["git", "add", "lib"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "bump lib"],
                   cwd=str(repo), env=env4, capture_output=True, check=True)

    _commit_raw(repo, "main.txt", b"main v2\n", "update main", "carol", 5)
    return repo, v1, v2


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class SubmoduleBase(ChallengeBase):

    def setUp(self):
        super().setUp()
        self.repo, self.v1, self.v2 = _build_submodule_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def _order(self, state=None):
        s = state or self.gh.read_state()
        return [c["hash"] for c in s["commits"]]

    def _lib_head(self):
        return _git(self.repo / "lib", "git", "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# Helper method unit tests
# ---------------------------------------------------------------------------

class HelperMethodTests(SubmoduleBase):
    """Direct tests of the four private helpers that power the submodule guards."""

    def test_get_gitmodules_empty_for_commit_without_submodule(self):
        bm = self._by_msg()
        self.assertEqual(self.gh._get_gitmodules(bm["base"]), "")

    def test_get_gitmodules_non_empty_for_commit_with_submodule(self):
        bm = self._by_msg()
        content = self.gh._get_gitmodules(bm["add lib"])
        self.assertIn("[submodule", content)
        self.assertIn("lib", content)

    def test_get_gitmodules_identical_for_bump_lib_and_add_lib(self):
        # bump lib only changes the gitlink, not .gitmodules
        bm = self._by_msg()
        self.assertEqual(
            self.gh._get_gitmodules(bm["add lib"]),
            self.gh._get_gitmodules(bm["bump lib"]),
        )

    def test_get_gitmodules_identical_across_unrelated_commits(self):
        # update main doesn't touch submodules at all
        bm = self._by_msg()
        self.assertEqual(
            self.gh._get_gitmodules(bm["bump lib"]),
            self.gh._get_gitmodules(bm["update main"]),
        )

    def test_gitlinks_at_empty_for_commit_without_submodule(self):
        bm = self._by_msg()
        self.assertEqual(self.gh._gitlinks_at(bm["base"]), {})

    def test_gitlinks_at_v1_at_add_lib(self):
        bm = self._by_msg()
        self.assertEqual(self.gh._gitlinks_at(bm["add lib"]), {"lib": self.v1})

    def test_gitlinks_at_v2_at_bump_lib(self):
        bm = self._by_msg()
        self.assertEqual(self.gh._gitlinks_at(bm["bump lib"]), {"lib": self.v2})

    def test_gitlinks_at_v2_at_update_main(self):
        # update main does not change the gitlink; pointer stays at v2
        bm = self._by_msg()
        self.assertEqual(self.gh._gitlinks_at(bm["update main"]), {"lib": self.v2})

    def test_commit_touches_gitmodules_true_for_add_lib(self):
        bm = self._by_msg()
        self.assertTrue(self.gh._commit_touches_gitmodules(bm["add lib"]))

    def test_commit_touches_gitmodules_false_for_bump_lib(self):
        # Only the gitlink (160000 entry) changed; .gitmodules is untouched
        bm = self._by_msg()
        self.assertFalse(self.gh._commit_touches_gitmodules(bm["bump lib"]))

    def test_commit_touches_gitmodules_false_for_unrelated_commit(self):
        bm = self._by_msg()
        self.assertFalse(self.gh._commit_touches_gitmodules(bm["update main"]))

    def test_any_moved_touches_gitmodules_true_when_add_lib_repositioned(self):
        bm = self._by_msg()
        current   = [bm["update main"], bm["bump lib"], bm["add lib"], bm["base"]]
        reordered = [bm["add lib"],     bm["update main"], bm["bump lib"], bm["base"]]
        self.assertTrue(self.gh._any_moved_commit_touches_gitmodules(current, reordered))

    def test_any_moved_touches_gitmodules_false_when_add_lib_stays_in_place(self):
        # Swap update main ↔ bump lib; add lib stays at index 2
        bm = self._by_msg()
        current   = [bm["update main"], bm["bump lib"], bm["add lib"], bm["base"]]
        reordered = [bm["bump lib"], bm["update main"], bm["add lib"], bm["base"]]
        self.assertFalse(self.gh._any_moved_commit_touches_gitmodules(current, reordered))

    def test_any_moved_touches_gitmodules_false_when_only_gitlink_commit_moves(self):
        # bump lib changes position but only affects the gitlink, not .gitmodules
        bm = self._by_msg()
        current   = [bm["update main"], bm["bump lib"], bm["add lib"], bm["base"]]
        reordered = [bm["bump lib"], bm["update main"], bm["add lib"], bm["base"]]
        self.assertFalse(self.gh._any_moved_commit_touches_gitmodules(current, reordered))

    def test_any_moved_touches_gitmodules_true_when_full_rotation_includes_add_lib(self):
        # All four commits move; add lib is one of them → blocked
        bm = self._by_msg()
        current   = [bm["update main"], bm["bump lib"], bm["add lib"], bm["base"]]
        rotated   = current[1:] + [current[0]]
        self.assertTrue(self.gh._any_moved_commit_touches_gitmodules(current, rotated))


# ---------------------------------------------------------------------------
# Reset: .gitmodules guard
# ---------------------------------------------------------------------------

class ResetGitmodulesGuardTests(SubmoduleBase):

    def test_reset_blocked_when_target_removes_gitmodules(self):
        # HEAD has .gitmodules; target "base" has none
        bm = self._by_msg()
        result = self.gh.reset(bm["base"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gitmodules_differ")

    def test_reset_blocked_when_resetting_forward_past_add_submodule(self):
        # Park HEAD at "base" (no .gitmodules), attempt redo to "add lib"
        bm = self._by_msg()
        _git(self.repo, "git", "reset", "--hard", bm["base"])
        self.gh = GitHistory(str(self.repo))
        result = self.gh.reset(bm["add lib"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gitmodules_differ")

    def test_reset_succeeds_when_gitmodules_identical_and_gitlink_changes(self):
        # HEAD = update main (lib=v2), target = add lib (lib=v1): same .gitmodules
        bm = self._by_msg()
        result = self.gh.reset(bm["add lib"])
        self.assertTrue(result["ok"], f"expected success, got: {result}")

    def test_reset_returns_submodule_update_suggested_when_gitlink_changes(self):
        bm = self._by_msg()
        result = self.gh.reset(bm["add lib"])
        self.assertTrue(result["ok"])
        self.assertTrue(
            result.get("submodule_update_suggested"),
            "expected submodule_update_suggested=True when gitlink pointer changes",
        )

    def test_reset_no_submodule_update_suggested_when_gitlink_unchanged(self):
        # bump lib and update main both point lib at v2 — no pointer difference
        bm = self._by_msg()
        result = self.gh.reset(bm["bump lib"])
        self.assertTrue(result["ok"])
        self.assertFalse(
            result.get("submodule_update_suggested", False),
            "should not suggest update when gitlink pointer does not change",
        )

    def test_reset_no_submodule_update_suggested_for_repo_without_submodules(self):
        # Two commits with no submodules: no suggestion expected
        bm = self._by_msg()
        # Strip back to base then add a second clean commit
        _git(self.repo, "git", "reset", "--hard", bm["base"])
        _commit_raw(self.repo, "other.txt", b"x\n", "other", "bob", 10)
        gh = GitHistory(str(self.repo))
        target = gh.read_state()["commits"][-1]["hash"]
        result = gh.reset(target)
        self.assertTrue(result["ok"])
        self.assertFalse(result.get("submodule_update_suggested", False))

    def test_reset_dirty_tree_still_blocked_when_gitmodules_same(self):
        # .gitmodules identical between target and HEAD, but working tree is dirty
        bm = self._by_msg()
        (self.repo / "main.txt").write_bytes(b"dirty\n")
        result = self.gh.reset(bm["add lib"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "dirty_tree")

    def test_gitmodules_check_takes_precedence_over_dirty_tree_check(self):
        # When both .gitmodules differs AND tree is dirty, gitmodules_differ is returned
        bm = self._by_msg()
        (self.repo / "main.txt").write_bytes(b"dirty\n")
        result = self.gh.reset(bm["base"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gitmodules_differ")


# ---------------------------------------------------------------------------
# submodule_update()
# ---------------------------------------------------------------------------

class SubmoduleUpdateTests(SubmoduleBase):

    def test_submodule_update_after_reset_brings_lib_to_v1(self):
        # Reset from HEAD (lib=v2) to add lib (lib=v1).
        # git reset --hard does not touch submodule dirs, so lib still has v2.
        # submodule_update() must bring it to v1.
        bm = self._by_msg()
        self.gh.reset(bm["add lib"])
        self.assertEqual(self._lib_head(), self.v2,
                         "lib should still be at v2 before calling submodule_update()")
        result = self.gh.submodule_update()
        self.assertTrue(result["ok"], f"submodule_update() failed: {result}")
        self.assertEqual(self._lib_head(), self.v1,
                         "lib should be at v1 after submodule_update()")

    def test_submodule_update_returns_valid_state(self):
        bm = self._by_msg()
        self.gh.reset(bm["add lib"])
        result = self.gh.submodule_update()
        self.assertTrue(result["ok"])
        self.assert_valid_state(result)
        self.assertFalse(result["dirty"])

    def test_submodule_update_when_already_synced_is_a_noop(self):
        # No reset first; lib already matches HEAD pointer (v2). Should still succeed.
        result = self.gh.submodule_update()
        self.assertTrue(result["ok"])
        self.assertEqual(self._lib_head(), self.v2)

    def test_submodule_update_then_reset_back_to_bump_lib_restores_v2(self):
        # Undo to add lib, update to v1, then redo to bump lib — v2 expected.
        bm = self._by_msg()
        bump_lib_hash = bm["bump lib"]

        self.gh.reset(bm["add lib"])
        self.gh.submodule_update()
        self.assertEqual(self._lib_head(), self.v1)

        result = self.gh.reset(bump_lib_hash)
        self.assertTrue(result["ok"])
        self.assertTrue(result.get("submodule_update_suggested"),
                        "redo to bump lib (lib=v2) should suggest update")
        self.gh.submodule_update()
        self.assertEqual(self._lib_head(), self.v2)


# ---------------------------------------------------------------------------
# Rebase move: .gitmodules guard
# ---------------------------------------------------------------------------

class RebaseMoveGitmodulesTests(SubmoduleBase):

    def test_move_blocked_when_add_lib_is_repositioned(self):
        bm = self._by_msg()
        order = self._order()
        # Move add lib to the front
        idx = order.index(bm["add lib"])
        order.insert(0, order.pop(idx))
        result = self.gh.rebase("move", order=order)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gitmodules_in_range")

    def test_move_blocked_when_add_lib_is_moved_down(self):
        # Move add lib toward the bottom; still blocked because it changes position
        bm = self._by_msg()
        order = self._order()
        idx = order.index(bm["add lib"])
        order.append(order.pop(idx))  # move to last position
        result = self.gh.rebase("move", order=order)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gitmodules_in_range")

    def test_move_allowed_when_only_bump_lib_is_repositioned(self):
        # bump lib only changes the gitlink, not .gitmodules — reorder must succeed
        bm = self._by_msg()
        order = self._order()
        um_idx = order.index(bm["update main"])
        bl_idx = order.index(bm["bump lib"])
        order[um_idx], order[bl_idx] = order[bl_idx], order[um_idx]
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"],
                        f"move of gitlink-only commit should be allowed, got: {result}")

    def test_move_allowed_when_add_lib_stays_in_place(self):
        # Swap update main ↔ bump lib; add lib does not change position
        bm = self._by_msg()
        order = self._order()
        um_idx = order.index(bm["update main"])
        bl_idx = order.index(bm["bump lib"])
        order[um_idx], order[bl_idx] = order[bl_idx], order[um_idx]
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"],
                        "move must be allowed when the .gitmodules commit stays in place")

    def test_move_noop_with_gitmodules_in_range_is_still_allowed(self):
        # Sending the unchanged order must short-circuit without touching add lib
        order = self._order()
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"])
        self.assertEqual(self._order(result), order)

    def test_move_blocked_when_full_rotation_includes_add_lib(self):
        # Rotate all four commits: add lib is among the movers
        order = self._order()
        rotated = order[1:] + [order[0]]
        result = self.gh.rebase("move", order=rotated)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gitmodules_in_range")

    def test_move_preserves_gitlink_after_reorder_of_non_gitmodules_commits(self):
        # After a permitted move, the gitlink entry must still exist in HEAD
        bm = self._by_msg()
        order = self._order()
        um_idx = order.index(bm["update main"])
        bl_idx = order.index(bm["bump lib"])
        order[um_idx], order[bl_idx] = order[bl_idx], order[um_idx]
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"])
        r = _git(self.repo, "git", "ls-tree", "HEAD", "lib")
        self.assertIn("160000", r.stdout,
                      "gitlink must still be present in HEAD after reorder")

    def test_repo_stays_clean_after_blocked_move(self):
        # A blocked move must leave the repo in a clean, non-rebasing state
        bm = self._by_msg()
        order = self._order()
        idx = order.index(bm["add lib"])
        order.insert(0, order.pop(idx))
        self.gh.rebase("move", order=order)
        state = self.gh.read_state()
        self.assertFalse(state["rebase_in_progress"])
        self.assertFalse(state["dirty"])


# ---------------------------------------------------------------------------
# Squash / fixup / reword with submodule commits (these must still work)
# ---------------------------------------------------------------------------

class SubmoduleRebaseOperationsTests(SubmoduleBase):
    """Squash, fixup, and reword on commits that touch submodules must still work."""

    def test_squash_add_lib_and_bump_lib(self):
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase(
            "squash",
            hashes=[bm["add lib"], bm["bump lib"]],
        )
        self.assertTrue(result["ok"], f"squash of submodule commits failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        r = _git(self.repo, "git", "ls-tree", "HEAD", "lib")
        self.assertIn("160000", r.stdout,
                      "gitlink missing from HEAD after squash of submodule commits")

    def test_fixup_bump_lib_into_add_lib(self):
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase("fixup", hashes=[bm["bump lib"]])
        self.assertTrue(result["ok"], f"fixup of submodule commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in result["commits"]]
        self.assertNotIn("bump lib", msgs)
        self.assertIn("add lib", msgs)

    def test_reword_add_lib_preserves_tree(self):
        bm = self._by_msg()
        h = bm["add lib"]
        old_tree = _git(self.repo, "git", "rev-parse", h + ":").stdout.strip()

        result = self.gh.rebase("reword", hashes=[h], new_message="introduce lib submodule")
        self.assertTrue(result["ok"], f"reword of submodule commit failed: {result}")

        new_h = self._by_msg(result).get("introduce lib submodule")
        self.assertIsNotNone(new_h, "rewarded commit not found in result")
        new_tree = _git(self.repo, "git", "rev-parse", new_h + ":").stdout.strip()
        self.assertEqual(old_tree, new_tree,
                         "tree changed after reword of submodule commit")

    def test_squash_bump_lib_with_unrelated_update_main(self):
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase(
            "squash",
            hashes=[bm["bump lib"], bm["update main"]],
        )
        self.assertTrue(result["ok"], f"squash of submodule + unrelated commit failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)


# ---------------------------------------------------------------------------
# Repo builder: three regular commits above a .gitmodules commit
# ---------------------------------------------------------------------------

def _build_repo_commits_after_gitmodules(parent: Path):
    """
    Five-commit repo where .gitmodules is at HEAD~3.

    History (newest first):
      "commit gamma"  — creates other.txt; does not touch .gitmodules
      "commit beta"   — creates extra.txt; does not touch .gitmodules
      "commit alpha"  — modifies main.txt; does not touch .gitmodules
      "add lib"       — creates .gitmodules, adds lib submodule at v1
      "base"          — initial commit
    """
    upstream = parent / "lib-upstream-ag"
    upstream.mkdir()
    init_repo(upstream)
    _commit_raw(upstream, "lib.py", b"# v1\n", "lib v1", "alice", 0)
    v1 = _git(upstream, "git", "rev-parse", "HEAD").stdout.strip()

    repo = parent / "repo-ag"
    repo.mkdir()
    init_repo(repo)
    _commit_raw(repo, "main.txt", b"base\n", "base", "alice", 0)

    env1 = _commit_env("bob", 1)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always",
         "submodule", "add", "--quiet", str(upstream), "lib"],
        cwd=str(repo), env=env1, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo / "lib"), "checkout", "--quiet", v1],
        capture_output=True, check=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add lib"],
                   cwd=str(repo), env=env1, capture_output=True, check=True)

    _commit_raw(repo, "main.txt", b"main v2\n", "commit alpha", "carol", 2)
    _commit_raw(repo, "extra.txt", b"extra v1\n", "commit beta", "alice", 3)
    _commit_raw(repo, "other.txt", b"other v1\n", "commit gamma", "bob", 4)
    return repo, v1


# ---------------------------------------------------------------------------
# Rebase operations on commits above .gitmodules
# ---------------------------------------------------------------------------

class RebaseAfterGitmodulesTests(ChallengeBase):
    """
    Verify that move, reword, fixup, and squash on commits that sit above a
    .gitmodules commit all succeed, and that the gitlink remains intact.

    Repo layout (newest first):
      "commit gamma"  — HEAD       (creates other.txt; independent)
      "commit beta"   — HEAD~1     (creates extra.txt; independent)
      "commit alpha"  — HEAD~2     (modifies main.txt; independent)
      "add lib"       — HEAD~3     (.gitmodules commit)
      "base"          — HEAD~4
    """

    def setUp(self):
        super().setUp()
        self.repo, self.v1 = _build_repo_commits_after_gitmodules(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def _order(self, state=None):
        s = state or self.gh.read_state()
        return [c["hash"] for c in s["commits"]]

    def _has_gitlink(self):
        r = _git(self.repo, "git", "ls-tree", "HEAD", "lib")
        return "160000" in r.stdout

    # -- move ----------------------------------------------------------------

    def test_move_two_commits_above_gitmodules_succeeds(self):
        # Swap gamma ↔ beta; add lib stays at index 3
        bm = self._by_msg()
        order = self._order()
        g, b = order.index(bm["commit gamma"]), order.index(bm["commit beta"])
        order[g], order[b] = order[b], order[g]
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"], f"move above .gitmodules failed: {result}")
        self.assert_valid_state(result)

    def test_move_three_commits_above_gitmodules_succeeds(self):
        # Rotate [gamma, beta, alpha] → [beta, alpha, gamma]; add lib and base fixed
        bm = self._by_msg()
        order = self._order()
        al = bm["commit alpha"]
        be = bm["commit beta"]
        ga = bm["commit gamma"]
        al_idx, be_idx, ga_idx = order.index(al), order.index(be), order.index(ga)
        order[ga_idx], order[be_idx], order[al_idx] = be, al, ga
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"], f"3-commit rotate above .gitmodules failed: {result}")
        self.assert_valid_state(result)

    def test_move_above_gitmodules_preserves_gitlink(self):
        bm = self._by_msg()
        order = self._order()
        g, b = order.index(bm["commit gamma"]), order.index(bm["commit beta"])
        order[g], order[b] = order[b], order[g]
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"])
        self.assertTrue(self._has_gitlink(),
                        "gitlink missing from HEAD after move above .gitmodules")

    def test_move_gitmodules_commit_still_blocked_in_this_repo(self):
        bm = self._by_msg()
        order = self._order()
        al_idx = order.index(bm["add lib"])
        order.insert(0, order.pop(al_idx))
        result = self.gh.rebase("move", order=order)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "gitmodules_in_range")

    # -- reword --------------------------------------------------------------

    def test_reword_top_commit_above_gitmodules_succeeds(self):
        bm = self._by_msg()
        result = self.gh.rebase("reword", hashes=[bm["commit gamma"]], new_message="gamma updated")
        self.assertTrue(result["ok"], f"reword gamma failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertIn("gamma updated", msgs)
        self.assertNotIn("commit gamma", msgs)

    def test_reword_commit_adjacent_to_gitmodules_succeeds(self):
        # alpha is immediately above add lib
        bm = self._by_msg()
        result = self.gh.rebase("reword", hashes=[bm["commit alpha"]], new_message="alpha updated")
        self.assertTrue(result["ok"], f"reword alpha (adjacent to .gitmodules) failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertIn("alpha updated", msgs)

    def test_reword_above_gitmodules_repo_stays_clean(self):
        bm = self._by_msg()
        result = self.gh.rebase("reword", hashes=[bm["commit beta"]], new_message="beta updated")
        self.assertTrue(result["ok"])
        self.assertFalse(result["dirty"])
        self.assertFalse(result["rebase_in_progress"])

    # -- fixup ---------------------------------------------------------------

    def test_fixup_top_commit_above_gitmodules_succeeds(self):
        # gamma folds into beta
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase("fixup", hashes=[bm["commit gamma"]])
        self.assertTrue(result["ok"], f"fixup gamma failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in result["commits"]]
        self.assertNotIn("commit gamma", msgs)
        self.assertIn("commit beta", msgs)

    def test_fixup_commit_above_gitmodules_into_predecessor_succeeds(self):
        # beta folds into alpha (alpha is adjacent to add lib)
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase("fixup", hashes=[bm["commit beta"]])
        self.assertTrue(result["ok"], f"fixup beta (folds into alpha adjacent to .gitmodules) failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in result["commits"]]
        self.assertNotIn("commit beta", msgs)
        self.assertIn("commit alpha", msgs)

    def test_fixup_above_gitmodules_preserves_gitlink(self):
        bm = self._by_msg()
        self.gh.rebase("fixup", hashes=[bm["commit gamma"]])
        self.assertTrue(self._has_gitlink(),
                        "gitlink missing from HEAD after fixup above .gitmodules")

    # -- squash --------------------------------------------------------------

    def test_squash_top_two_commits_above_gitmodules_succeeds(self):
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase("squash", hashes=[bm["commit beta"], bm["commit gamma"]])
        self.assertTrue(result["ok"], f"squash beta+gamma failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    def test_squash_commits_adjacent_to_gitmodules_succeeds(self):
        # alpha (adjacent to add lib) squashed with beta; both above .gitmodules
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase("squash", hashes=[bm["commit alpha"], bm["commit beta"]])
        self.assertTrue(result["ok"], f"squash alpha+beta (alpha adjacent to .gitmodules) failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    def test_squash_all_three_commits_above_gitmodules_succeeds(self):
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase(
            "squash",
            hashes=[bm["commit alpha"], bm["commit beta"], bm["commit gamma"]],
        )
        self.assertTrue(result["ok"], f"squash all three above .gitmodules failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 2)

    def test_squash_above_gitmodules_preserves_gitlink(self):
        bm = self._by_msg()
        self.gh.rebase("squash", hashes=[bm["commit beta"], bm["commit gamma"]])
        self.assertTrue(self._has_gitlink(),
                        "gitlink missing from HEAD after squash above .gitmodules")


# ---------------------------------------------------------------------------
# switch_branch: submodule update suggestion
# ---------------------------------------------------------------------------

def _build_two_branch_submodule_repo(parent: Path):
    """
    Two-branch repo where main has lib at v2 and 'side' has lib at v1.

    Returns (repo_path, v1, v2).
    """
    repo, v1, v2 = _build_submodule_repo(parent)
    bm = {c["message"]: c["hash"]
          for c in GitHistory(str(repo)).read_state()["commits"]}
    _git(repo, "git", "branch", "side", bm["add lib"])
    return repo, v1, v2


class SwitchBranchSubmoduleTests(ChallengeBase):

    def setUp(self):
        super().setUp()
        self.repo, self.v1, self.v2 = _build_two_branch_submodule_repo(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _lib_head(self):
        return _git(self.repo / "lib", "git", "rev-parse", "HEAD").stdout.strip()

    def test_switch_suggests_update_when_gitlink_differs(self):
        # main has lib=v2, side has lib=v1 — switch must suggest update
        result = self.gh.switch_branch("side")
        self.assertTrue(result["ok"], f"switch failed: {result}")
        self.assertTrue(
            result.get("submodule_update_suggested"),
            "expected submodule_update_suggested=True when gitlink pointer differs between branches",
        )

    def test_switch_no_suggestion_when_gitlink_unchanged(self):
        # Create a branch at the same commit as main; no gitlink change expected
        _git(self.repo, "git", "branch", "same", "HEAD")
        result = self.gh.switch_branch("same")
        self.assertTrue(result["ok"], f"switch failed: {result}")
        self.assertFalse(
            result.get("submodule_update_suggested", False),
            "should not suggest update when gitlink pointer is identical between branches",
        )

    def test_switch_no_suggestion_when_no_submodules(self):
        # Plain two-commit repo with no submodules
        from make_test_repo import init_repo
        plain = self.tmpdir / "plain"
        plain.mkdir()
        init_repo(plain)
        _commit_raw(plain, "a.txt", b"a\n", "first", "alice", 0)
        _git(plain, "git", "branch", "other")
        _commit_raw(plain, "b.txt", b"b\n", "second", "bob", 1)
        gh = GitHistory(str(plain))
        result = gh.switch_branch("other")
        self.assertTrue(result["ok"], f"switch failed: {result}")
        self.assertFalse(
            result.get("submodule_update_suggested", False),
            "should not suggest update when repo has no submodules",
        )


# ---------------------------------------------------------------------------
# Repo builder: three trivial commits on top of a gitlink-only commit,
# which itself sits above the .gitmodules commit.
#
# This reproduces the exact bug report scenario:
#   HEAD   "commit gamma" — independent (creates c.txt)
#   HEAD~1 "commit beta"  — independent (creates b.txt)
#   HEAD~2 "commit alpha" — independent (creates a.txt)
#   HEAD~3 "bump lib"     — gitlink v1→v2 (NO .gitmodules change)
#   HEAD~4 "add lib"      — creates .gitmodules, adds lib at v1
#   HEAD~5 "base"         — initial commit
# ---------------------------------------------------------------------------

def _build_repo_trivials_above_gitlink(parent: Path):
    """
    Six-commit repo matching the bug-report layout.

    Returns (repo_path, v1, v2).
    """
    upstream = parent / "lib-upstream-tg"
    upstream.mkdir()
    init_repo(upstream)
    _commit_raw(upstream, "lib.py", b"# v1\n", "lib v1", "alice", 0)
    v1 = _git(upstream, "git", "rev-parse", "HEAD").stdout.strip()
    _commit_raw(upstream, "lib.py", b"# v2\n", "lib v2", "alice", 1)
    v2 = _git(upstream, "git", "rev-parse", "HEAD").stdout.strip()

    repo = parent / "repo-tg"
    repo.mkdir()
    init_repo(repo)
    _commit_raw(repo, "main.txt", b"base\n", "base", "alice", 0)

    env1 = _commit_env("bob", 1)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always",
         "submodule", "add", "--quiet", str(upstream), "lib"],
        cwd=str(repo), env=env1, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo / "lib"), "checkout", "--quiet", v1],
        capture_output=True, check=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add lib"],
                   cwd=str(repo), env=env1, capture_output=True, check=True)

    env2 = _commit_env("carol", 2)
    subprocess.run(
        ["git", "-C", str(repo / "lib"), "checkout", "--quiet", v2],
        capture_output=True, check=True,
    )
    subprocess.run(["git", "add", "lib"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "bump lib"],
                   cwd=str(repo), env=env2, capture_output=True, check=True)

    _commit_raw(repo, "a.txt", b"alpha\n", "commit alpha", "alice", 3)
    _commit_raw(repo, "b.txt", b"beta\n",  "commit beta",  "bob",   4)
    _commit_raw(repo, "c.txt", b"gamma\n", "commit gamma", "carol", 5)
    return repo, v1, v2


# ---------------------------------------------------------------------------
# Regression tests for the "fixup on trivial commits above a gitlink" bug.
#
# Before the fix, rebase() always ran git rebase -i HEAD~200 with ALL
# visible commits in the todo, causing git to replay the gitlink commit at
# HEAD~3 (and the .gitmodules commit at HEAD~4) even when the user only
# wanted to fixup the top commit.  With the minimal-range fix, only the
# commits that actually need to change are put in the todo.
# ---------------------------------------------------------------------------

class TrivialCommitsAboveGitlinkTests(ChallengeBase):
    """
    Fixup, reword, and move of the three topmost commits must succeed even
    though a gitlink-only commit sits at HEAD~3 and a .gitmodules commit at
    HEAD~4.  The minimal rebase range ensures those submodule commits are
    never replayed.
    """

    def setUp(self):
        super().setUp()
        self.repo, self.v1, self.v2 = _build_repo_trivials_above_gitlink(self.tmpdir)
        self.gh = GitHistory(str(self.repo))

    def _by_msg(self, state=None):
        s = state or self.gh.read_state()
        return {c["message"]: c["hash"] for c in s["commits"]}

    def _order(self, state=None):
        s = state or self.gh.read_state()
        return [c["hash"] for c in s["commits"]]

    def _has_gitlink(self):
        r = _git(self.repo, "git", "ls-tree", "HEAD", "lib")
        return "160000" in r.stdout

    # -- fixup ---------------------------------------------------------------

    def test_fixup_head_into_predecessor_succeeds(self):
        # "commit gamma" (HEAD) folds into "commit beta" (HEAD~1).
        # The minimal base is "commit alpha" (HEAD~2); bump lib is NOT replayed.
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase("fixup", hashes=[bm["commit gamma"]])
        self.assertTrue(result["ok"], f"fixup HEAD failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in result["commits"]]
        self.assertNotIn("commit gamma", msgs)
        self.assertIn("commit beta", msgs)

    def test_fixup_head_leaves_repo_clean(self):
        bm = self._by_msg()
        result = self.gh.rebase("fixup", hashes=[bm["commit gamma"]])
        self.assertTrue(result["ok"])
        self.assertFalse(result["rebase_in_progress"],
                         "rebase must not be left in progress after fixup")
        self.assertFalse(result["dirty"])

    def test_fixup_head_preserves_gitlink(self):
        bm = self._by_msg()
        result = self.gh.rebase("fixup", hashes=[bm["commit gamma"]])
        self.assertTrue(result["ok"])
        self.assertTrue(self._has_gitlink(),
                        "gitlink must survive fixup of a commit above it")

    def test_fixup_head_minus_one_into_predecessor_succeeds(self):
        # "commit beta" (HEAD~1) folds into "commit alpha" (HEAD~2).
        # The minimal base is "bump lib" (HEAD~3) — a gitlink commit,
        # but it is used only as the rebase BASE (not replayed), so git
        # never re-applies the submodule change.
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase("fixup", hashes=[bm["commit beta"]])
        self.assertTrue(result["ok"], f"fixup HEAD~1 failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in result["commits"]]
        self.assertNotIn("commit beta", msgs)
        self.assertIn("commit alpha", msgs)

    def test_fixup_head_minus_one_leaves_repo_clean(self):
        bm = self._by_msg()
        result = self.gh.rebase("fixup", hashes=[bm["commit beta"]])
        self.assertTrue(result["ok"])
        self.assertFalse(result["rebase_in_progress"])
        self.assertFalse(result["dirty"])

    def test_fixup_top_two_commits_together_succeeds(self):
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase(
            "fixup",
            hashes=[bm["commit gamma"], bm["commit beta"]],
        )
        self.assertTrue(result["ok"], f"multi-fixup failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)

    # -- reword --------------------------------------------------------------

    def test_reword_head_succeeds(self):
        bm = self._by_msg()
        result = self.gh.rebase("reword", hashes=[bm["commit gamma"]],
                                new_message="gamma renamed")
        self.assertTrue(result["ok"], f"reword HEAD failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertIn("gamma renamed", msgs)
        self.assertNotIn("commit gamma", msgs)

    def test_reword_head_leaves_repo_clean(self):
        bm = self._by_msg()
        result = self.gh.rebase("reword", hashes=[bm["commit gamma"]],
                                new_message="renamed")
        self.assertTrue(result["ok"])
        self.assertFalse(result["rebase_in_progress"])
        self.assertFalse(result["dirty"])

    def test_reword_head_minus_one_succeeds(self):
        bm = self._by_msg()
        result = self.gh.rebase("reword", hashes=[bm["commit beta"]],
                                new_message="beta renamed")
        self.assertTrue(result["ok"], f"reword HEAD~1 failed: {result}")
        msgs = [c["message"] for c in result["commits"]]
        self.assertIn("beta renamed", msgs)

    def test_reword_head_preserves_gitlink(self):
        bm = self._by_msg()
        self.gh.rebase("reword", hashes=[bm["commit gamma"]], new_message="x")
        self.assertTrue(self._has_gitlink(),
                        "gitlink must survive reword of a commit above it")

    # -- move ----------------------------------------------------------------

    def test_move_swap_top_two_trivial_commits_succeeds(self):
        bm = self._by_msg()
        order = self._order()
        g = order.index(bm["commit gamma"])
        b = order.index(bm["commit beta"])
        order[g], order[b] = order[b], order[g]
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"],
                        f"swap of top two trivial commits failed: {result}")
        self.assert_valid_state(result)

    def test_move_swap_top_two_leaves_repo_clean(self):
        bm = self._by_msg()
        order = self._order()
        g = order.index(bm["commit gamma"])
        b = order.index(bm["commit beta"])
        order[g], order[b] = order[b], order[g]
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"])
        self.assertFalse(result["rebase_in_progress"])
        self.assertFalse(result["dirty"])

    def test_move_swap_top_two_preserves_gitlink(self):
        bm = self._by_msg()
        order = self._order()
        g = order.index(bm["commit gamma"])
        b = order.index(bm["commit beta"])
        order[g], order[b] = order[b], order[g]
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"])
        self.assertTrue(self._has_gitlink(),
                        "gitlink must survive swap of trivial commits above it")

    def test_move_rotate_all_three_trivial_commits_succeeds(self):
        bm = self._by_msg()
        order = self._order()
        al = bm["commit alpha"]
        be = bm["commit beta"]
        ga = bm["commit gamma"]
        al_i, be_i, ga_i = order.index(al), order.index(be), order.index(ga)
        order[ga_i], order[be_i], order[al_i] = be, al, ga
        result = self.gh.rebase("move", order=order)
        self.assertTrue(result["ok"],
                        f"3-commit rotate above gitlink failed: {result}")
        self.assert_valid_state(result)

    def test_move_gitlink_commit_still_blocked(self):
        # "bump lib" (gitlink-only, HEAD~3) must not be repositioned —
        # but that is enforced via the existing gitmodules guard only if
        # .gitmodules is in the range.  bump lib itself does NOT touch
        # .gitmodules, so the guard does not block it.  This test documents
        # the current (permitted) behaviour for a gitlink-only move.
        bm = self._by_msg()
        order = self._order()
        bl_i = order.index(bm["bump lib"])
        ga_i = order.index(bm["commit gamma"])
        order[bl_i], order[ga_i] = order[ga_i], order[bl_i]
        result = self.gh.rebase("move", order=order)
        # bump lib does not touch .gitmodules, so this move is permitted.
        self.assertTrue(result["ok"],
                        "moving a gitlink-only commit should be allowed")

    # -- squash --------------------------------------------------------------

    def test_squash_top_two_trivial_commits_succeeds(self):
        state = self.gh.read_state()
        bm = self._by_msg(state)
        result = self.gh.rebase(
            "squash",
            hashes=[bm["commit gamma"], bm["commit beta"]],
        )
        self.assertTrue(result["ok"], f"squash top two failed: {result}")
        self.assertEqual(len(result["commits"]), len(state["commits"]) - 1)


if __name__ == "__main__":
    unittest.main()
