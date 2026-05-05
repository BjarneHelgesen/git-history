"""
Unit tests for the git-history REST API.

These tests exercise the Flask endpoints defined in the plan. They verify
HTTP methods, status codes, JSON structure, auth token enforcement, and that
each endpoint correctly delegates to the GitHistory backend.

The Flask app is expected to live in ``git_history.py`` and expose a
``create_app(repo_path, token)`` factory that returns a configured Flask app.
The app stores a ``GitHistory`` instance on ``app.config["GH"]`` and the auth
token on ``app.config["TOKEN"]``.

Endpoints under test:

    GET  /api/state                -> state JSON
    POST /api/stash                -> state | error JSON
    POST /api/stash/pop            -> state | error JSON
    POST /api/rebase               -> state | conflict | error JSON
    POST /api/rebase/continue      -> state | conflict | error JSON
    POST /api/rebase/abort         -> state | error JSON
    POST /api/reset                -> state | error JSON
    GET  /api/show?commit_hash=<commit_hash>  -> {ok, info, diff} | error JSON

Run with:
    python -m unittest tests.test_rest_api
"""
import os
from unittest.mock import patch
import pytest
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_test_repo import COMMITS
from conftest import _commit_raw, _build_conflict_repo, _build_template_repo
from git_history import create_app


TOKEN = "test-token-abc123"


# ---------------------------------------------------------------------------
# Base test class
# ---------------------------------------------------------------------------

class StandardAPITest(unittest.TestCase):
    """Fresh copy of the 21-commit test repo with a Flask test client."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-api-test-"))
        self.repo = self.tmpdir / "repo"
        shutil.copytree(_build_template_repo(), self.repo)
        self.app = create_app(str(self.repo), TOKEN)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def get(self, url, **kwargs):
        return self.client.get(url, headers={"X-Token": TOKEN}, **kwargs)

    def post(self, url, json=None, **kwargs):
        return self.client.post(url, json=json,
                                headers={"X-Token": TOKEN}, **kwargs)

    def make_dirty(self, path="README.md", content=b"# changed\n"):
        (self.repo / path).write_bytes(content)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class AuthTests(StandardAPITest):

    def test_missing_token_returns_403(self):
        resp = self.client.get("/api/state")
        self.assertEqual(resp.status_code, 403)

    def test_wrong_token_returns_403(self):
        resp = self.client.get("/api/state",
                               headers={"X-Token": "wrong"})
        self.assertEqual(resp.status_code, 403)

    def test_correct_token_returns_200(self):
        resp = self.get("/api/state")
        self.assertEqual(resp.status_code, 200)

    def test_auth_applies_to_post_endpoints(self):
        resp = self.client.post("/api/stash")
        self.assertEqual(resp.status_code, 403)

    def test_api_state_with_token_in_query_string(self):
        resp = self.client.get(f"/api/state?t={TOKEN}")
        self.assertEqual(resp.status_code, 200)

    def test_auth_not_required_for_root(self):
        # Static files / index don't require auth.  The root route may
        # return 404 if static files aren't present yet, but it must NOT
        # return 403.
        resp = self.client.get("/")
        self.assertNotEqual(resp.status_code, 403)


# ---------------------------------------------------------------------------
# GET /api/state
# ---------------------------------------------------------------------------

class StateEndpointTests(StandardAPITest):

    def test_returns_json(self):
        resp = self.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type, "application/json")

    def test_state_has_expected_fields(self):
        data = self.get("/api/state").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("branch", data)
        self.assertIn("dirty", data)
        self.assertIn("has_stash", data)
        self.assertIn("rebase_in_progress", data)
        self.assertIn("conflict_files", data)
        self.assertIn("commits", data)
        self.assertIn("branch_history", data)

    def test_state_commit_count(self):
        data = self.get("/api/state").get_json()
        self.assertEqual(len(data["commits"]), len(COMMITS))

    def test_state_commits_newest_first(self):
        data = self.get("/api/state").get_json()
        self.assertEqual(data["commits"][0]["message"], "Add CI workflow")
        self.assertEqual(data["commits"][-1]["message"], "Initial commit")


# ---------------------------------------------------------------------------
# POST /api/stash
# ---------------------------------------------------------------------------

class StashEndpointTests(StandardAPITest):

    def test_stash_clean_returns_nothing_to_stash(self):
        data = self.post("/api/stash").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "nothing_to_stash")

    def test_stash_dirty_succeeds(self):
        self.make_dirty()
        data = self.post("/api/stash").get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["dirty"])
        self.assertTrue(data["has_stash"])

    def test_stash_uses_post(self):
        resp = self.get("/api/stash")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# POST /api/stash/pop
# ---------------------------------------------------------------------------

class StashPopEndpointTests(StandardAPITest):

    def test_pop_no_stash_returns_error(self):
        data = self.post("/api/stash/pop").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "no_stash")

    def test_pop_when_dirty_returns_error(self):
        self.make_dirty()
        self.post("/api/stash")
        self.make_dirty(path="LICENSE", content=b"dirty\n")
        data = self.post("/api/stash/pop").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    def test_pop_restores_dirty_state(self):
        self.make_dirty()
        self.post("/api/stash")
        data = self.post("/api/stash/pop").get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["dirty"])
        self.assertFalse(data["has_stash"])

    def test_stash_pop_uses_post(self):
        resp = self.get("/api/stash/pop")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# POST /api/rebase — move
# ---------------------------------------------------------------------------

class RebaseMoveEndpointTests(StandardAPITest):

    def test_swap_two_newest(self):
        state = self.get("/api/state").get_json()
        commit_hashes = [c["commit_hash"] for c in state["commits"]]
        commit_hashes[0], commit_hashes[1] = commit_hashes[1], commit_hashes[0]

        data = self.post("/api/rebase", json={
            "operation": "move", "order": commit_hashes,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["message"],
                         state["commits"][1]["message"])

    def test_move_unchanged_order_is_noop(self):
        state = self.get("/api/state").get_json()
        commit_hashes = [c["commit_hash"] for c in state["commits"]]

        data = self.post("/api/rebase", json={
            "operation": "move", "order": commit_hashes,
        }).get_json()
        self.assertTrue(data["ok"])
        new_hashes = [c["commit_hash"] for c in data["commits"]]
        self.assertEqual(new_hashes, commit_hashes)

    def test_move_refused_when_dirty(self):
        self.make_dirty()
        state = self.get("/api/state").get_json()
        commit_hashes = [c["commit_hash"] for c in state["commits"]]
        commit_hashes[0], commit_hashes[1] = commit_hashes[1], commit_hashes[0]

        data = self.post("/api/rebase", json={
            "operation": "move", "order": commit_hashes,
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    def test_rebase_uses_post(self):
        resp = self.get("/api/rebase")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# POST /api/rebase — squash
# ---------------------------------------------------------------------------

class RebaseSquashEndpointTests(StandardAPITest):

    def test_squash_two_adjacent(self):
        state = self.get("/api/state").get_json()
        commit_hashes = [state["commits"][0]["commit_hash"], state["commits"][1]["commit_hash"]]

        data = self.post("/api/rebase", json={
            "operation": "squash", "commit_hashes": commit_hashes,
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) - 1)

    def test_squash_refused_when_dirty(self):
        self.make_dirty()
        state = self.get("/api/state").get_json()
        commit_hashes = [state["commits"][0]["commit_hash"], state["commits"][1]["commit_hash"]]

        data = self.post("/api/rebase", json={
            "operation": "squash", "commit_hashes": commit_hashes,
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")


# ---------------------------------------------------------------------------
# POST /api/rebase — fixup
# ---------------------------------------------------------------------------

class RebaseFixupEndpointTests(StandardAPITest):

    def test_fixup_middle_commit(self):
        state = self.get("/api/state").get_json()
        target = state["commits"][3]

        data = self.post("/api/rebase", json={
            "operation": "fixup", "commit_hashes": [target["commit_hash"]],
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["commits"]), len(state["commits"]) - 1)
        msgs = [c["message"] for c in data["commits"]]
        self.assertNotIn(target["message"], msgs)

    def test_fixup_root_refused(self):
        state = self.get("/api/state").get_json()
        root = state["commits"][-1]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "fixup", "commit_commit_hashes":[root],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


# ---------------------------------------------------------------------------
# POST /api/rebase — reword
# ---------------------------------------------------------------------------

class RebaseRewordEndpointTests(StandardAPITest):

    def test_reword_top_commit(self):
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "reword",
            "commit_hashes": [h],
            "new_message": "Brand new message",
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["message"], "Brand new message")

    def test_reword_missing_message_returns_error(self):
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "reword",
            "commit_commit_hashes":[h],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    def test_reword_refused_when_dirty(self):
        self.make_dirty()
        state = self.get("/api/state").get_json()
        h = state["commits"][0]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "reword",
            "commit_hashes":[h],
            "new_message": "x",
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")


# ---------------------------------------------------------------------------
# POST /api/rebase — invalid operation
# ---------------------------------------------------------------------------

class RebaseInvalidTests(StandardAPITest):

    def test_unknown_operation_returns_error(self):
        data = self.post("/api/rebase", json={
            "operation": "unknown",
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    def test_missing_body_returns_error(self):
        data = self.post("/api/rebase").get_json()
        self.assertFalse(data["ok"])

    def test_rebase_move_with_missing_order_field(self):
        data = self.post("/api/rebase", json={
            "operation": "move",
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


# ---------------------------------------------------------------------------
# POST /api/reset
# ---------------------------------------------------------------------------

class ResetEndpointTests(StandardAPITest):

    def test_reset_to_older_commit(self):
        state = self.get("/api/state").get_json()
        target = state["commits"][5]

        data = self.post("/api/reset", json={
            "commit_hash":target["commit_hash"],
        }).get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["commits"][0]["commit_hash"], target["commit_hash"])

    def test_reset_refused_when_dirty(self):
        self.make_dirty()
        h = self.get("/api/state").get_json()["commits"][5]["commit_hash"]

        data = self.post("/api/reset", json={"commit_hash":h}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    def test_reset_uses_post(self):
        resp = self.get("/api/reset")
        self.assertEqual(resp.status_code, 405)

    def test_reset_with_missing_commit_hash(self):
        data = self.post("/api/reset", json={}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_commit")


# ---------------------------------------------------------------------------
# GET /api/show
# ---------------------------------------------------------------------------

class ShowEndpointTests(StandardAPITest):

    def test_show_returns_info_and_diff(self):
        state = self.get("/api/state").get_json()
        head = state["commits"][0]

        data = self.get(f"/api/show?commit_hash={head['commit_hash']}").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("diff --git", data["diff"])

    def test_show_unknown_hash(self):
        data = self.get(f"/api/show?commit_hash={'0' * 40}").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_commit")

    def test_show_missing_hash_param(self):
        data = self.get("/api/show").get_json()
        self.assertFalse(data["ok"])

    def test_show_uses_get(self):
        resp = self.post("/api/show")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# Conflict flow via REST
# ---------------------------------------------------------------------------

class ConflictEndpointTests(unittest.TestCase):
    """Test the full conflict workflow through the API."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-api-conflict-"))
        self.repo = _build_conflict_repo(self.tmpdir)
        self.app = create_app(str(self.repo), TOKEN)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def get(self, url, **kwargs):
        return self.client.get(url, headers={"X-Token": TOKEN}, **kwargs)

    def post(self, url, json=None, **kwargs):
        return self.client.post(url, json=json,
                                headers={"X-Token": TOKEN}, **kwargs)

    def _swap_order(self):
        """Swap the two newest commits to trigger a conflict."""
        state = self.get("/api/state").get_json()
        order = [c["commit_hash"] for c in state["commits"]]
        order[0], order[1] = order[1], order[0]
        return self.post("/api/rebase", json={"operation": "move", "order": order})

    def test_move_produces_conflict(self):
        data = self._swap_order().get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data.get("conflict"))
        self.assertIn("f.txt", data["conflict_files"])
        self.assertTrue(data["rebase_in_progress"])

    def test_abort_clears_conflict(self):
        self._swap_order()
        data = self.post("/api/rebase/abort").get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["rebase_in_progress"])
        self.assertEqual(data["conflict_files"], [])

    def test_continue_with_unresolved_conflict(self):
        self._swap_order()
        data = self.post("/api/rebase/continue").get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data.get("conflict"))
        self.assertTrue(data["rebase_in_progress"])

    def test_conflict_response_includes_full_state(self):
        data = self._swap_order().get_json()
        self.assertTrue(data.get("conflict"))
        # Conflict response must carry the full state fields, not just conflict
        # info, so the frontend can refresh uniformly.
        for field in ("branch", "branches", "commits", "branch_history",
                      "dirty", "has_stash", "submodule_update_suggested"):
            self.assertIn(field, data)
        self.assertTrue(data["commits"])

    def test_continue_after_resolving(self):
        self._swap_order()
        # Resolve to initial content so version_A's patch applies cleanly next.
        (self.repo / "f.txt").write_bytes(b"line1\nline2\nline3\n")
        subprocess.run(["git", "add", "f.txt"], cwd=str(self.repo),
                       check=True, capture_output=True)

        data = self.post("/api/rebase/continue").get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["rebase_in_progress"])

    def test_rebase_continue_uses_post(self):
        resp = self.client.get("/api/rebase/continue",
                               headers={"X-Token": TOKEN})
        self.assertEqual(resp.status_code, 405)

    def test_rebase_abort_uses_post(self):
        resp = self.client.get("/api/rebase/abort",
                               headers={"X-Token": TOKEN})
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# POST /api/quit
# ---------------------------------------------------------------------------

class QuitEndpointTests(StandardAPITest):

    def test_quit_returns_ok(self):
        with patch("git_history.os._exit"):
            data = self.post("/api/quit").get_json()
        self.assertTrue(data["ok"])

    def test_quit_uses_post(self):
        resp = self.get("/api/quit")
        self.assertEqual(resp.status_code, 405)


class ManualPageTests(StandardAPITest):

    def test_manual_returns_200(self):
        resp = self.client.get("/manual")
        self.assertEqual(resp.status_code, 200)

    def test_manual_contains_html(self):
        resp = self.client.get("/manual")
        self.assertIn(b"git-history", resp.data)

    def test_manual_requires_no_token(self):
        resp = self.client.get("/manual")
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# POST /api/rebase — consecutive commit validation
# ---------------------------------------------------------------------------

class RebaseConsecutiveEndpointTests(StandardAPITest):

    def test_squash_non_adjacent_commits_returns_invalid_request(self):
        state = self.get("/api/state").get_json()
        h0 = state["commits"][0]["commit_hash"]
        h2 = state["commits"][2]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "squash",
            "commit_commit_hashes":[h0, h2],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    def test_fixup_non_adjacent_commits_returns_invalid_request(self):
        state = self.get("/api/state").get_json()
        h0 = state["commits"][0]["commit_hash"]
        h2 = state["commits"][2]["commit_hash"]

        data = self.post("/api/rebase", json={
            "operation": "fixup",
            "commit_commit_hashes":[h0, h2],
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


# ---------------------------------------------------------------------------
# POST /api/submodule/update
# ---------------------------------------------------------------------------

class SubmoduleUpdateEndpointTests(StandardAPITest):

    def test_submodule_update_uses_post(self):
        resp = self.get("/api/submodule/update")
        self.assertEqual(resp.status_code, 405)

    def test_submodule_update_requires_auth(self):
        resp = self.client.post("/api/submodule/update")
        self.assertEqual(resp.status_code, 403)

    def test_submodule_update_returns_state(self):
        data = self.post("/api/submodule/update").get_json()
        self.assertTrue(data["ok"])
        self.assertIn("commits", data)
        self.assertIn("branch", data)
        self.assertIn("dirty", data)


# ---------------------------------------------------------------------------
# GET /log
# ---------------------------------------------------------------------------

class LogEndpointTests(StandardAPITest):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="git-history-api-test-"))
        self.repo = self.tmpdir / "repo"
        shutil.copytree(_build_template_repo(), self.repo)
        self._log_file = self.tmpdir / "test.log"
        self.app = create_app(str(self.repo), TOKEN, log_path=self._log_file)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        super().tearDown()

    def test_log_returns_200_when_file_does_not_exist(self):
        resp = self.get("/log")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"")

    def test_log_returns_plain_text_content_type(self):
        resp = self.get("/log")
        self.assertIn("text/plain", resp.content_type)

    def test_log_requires_auth(self):
        resp = self.client.get("/log")
        self.assertEqual(resp.status_code, 403)

    def test_log_returns_entries_after_reset(self):
        state = self.get("/api/state").get_json()
        self.post("/api/reset", json={"commit_hash":state["commits"][3]["commit_hash"]})

        resp = self.get("/log")
        lines = [l for l in resp.data.decode("utf-8").splitlines() if l]
        self.assertEqual(len(lines), 1)
        parts = lines[0].split()
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[1], "main")
        self.assertEqual(len(parts[2]), 40)

    def test_log_accumulates_entries_across_operations(self):
        state = self.get("/api/state").get_json()
        self.post("/api/reset", json={"commit_hash":state["commits"][2]["commit_hash"]})

        state2 = self.get("/api/state").get_json()
        self.post("/api/rebase", json={
            "operation": "reword",
            "commit_hashes": [state2["commits"][0]["commit_hash"]],
            "new_message": "log test reword",
        })

        lines = [l for l in self.get("/log").data.decode("utf-8").splitlines() if l]
        self.assertEqual(len(lines), 2)


# ---------------------------------------------------------------------------
# Auth token edge cases
# ---------------------------------------------------------------------------

class AuthTokenEdgeCasesTests(StandardAPITest):

    def test_empty_token_returns_403(self):
        resp = self.client.get("/api/state", headers={"X-Token": ""})
        self.assertEqual(resp.status_code, 403)

    def test_token_with_spaces_returns_403(self):
        resp = self.client.get("/api/state",
                               headers={"X-Token": "test-token abc123"})
        self.assertEqual(resp.status_code, 403)

    def test_token_case_sensitive(self):
        resp = self.client.get("/api/state",
                               headers={"X-Token": TOKEN.upper()})
        self.assertEqual(resp.status_code, 403)

    def test_token_with_special_characters_wrong(self):
        resp = self.client.get("/api/state",
                               headers={"X-Token": TOKEN + "!"})
        self.assertEqual(resp.status_code, 403)

    def test_token_in_query_string_vs_header(self):
        # Query string token should work
        resp = self.client.get(f"/api/state?t={TOKEN}")
        self.assertEqual(resp.status_code, 200)

    def test_wrong_query_token_returns_403(self):
        resp = self.client.get("/api/state?t=wrong")
        self.assertEqual(resp.status_code, 403)

    def test_malformed_json_with_valid_token(self):
        resp = self.client.post("/api/rebase",
                                data="{invalid json}",
                                headers={"X-Token": TOKEN,
                                        "Content-Type": "application/json"})
        # Should fail on JSON parsing, not auth
        self.assertNotEqual(resp.status_code, 403)

    def test_missing_operation_field_returns_error(self):
        data = self.post("/api/rebase", json={}).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


# ---------------------------------------------------------------------------
# Stash conflict scenarios
# ---------------------------------------------------------------------------

class StashConflictScenarioTests(StandardAPITest):

    def test_stash_pop_with_uncommitted_changes_after_stash(self):
        # Make dirty changes and stash them
        self.make_dirty()
        self.post("/api/stash")

        # Make different dirty changes
        self.make_dirty(path="LICENSE", content=b"different content\n")

        # Pop should fail due to dirty tree
        data = self.post("/api/stash/pop").get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "dirty_tree")

    def test_stash_then_reset_then_pop(self):
        state = self.get("/api/state").get_json()
        self.make_dirty()
        self.post("/api/stash")

        # Reset to an older commit (changes state)
        self.post("/api/reset", json={"commit_hash": state["commits"][2]["commit_hash"]})

        # Pop should still work on the stashed content
        data = self.post("/api/stash/pop").get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["dirty"])


# ---------------------------------------------------------------------------
# HTTP method validation
# ---------------------------------------------------------------------------

class HTTPMethodValidationTests(StandardAPITest):

    def test_state_rejects_post(self):
        resp = self.post("/api/state")
        self.assertEqual(resp.status_code, 405)

    def test_stash_rejects_get(self):
        resp = self.get("/api/stash")
        self.assertEqual(resp.status_code, 405)

    def test_show_rejects_post(self):
        resp = self.post("/api/show")
        self.assertEqual(resp.status_code, 405)

    def test_rebase_rejects_get(self):
        resp = self.get("/api/rebase")
        self.assertEqual(resp.status_code, 405)


# ---------------------------------------------------------------------------
# Error response structure
# ---------------------------------------------------------------------------

class ErrorResponseStructureTests(StandardAPITest):

    def test_error_response_has_ok_false(self):
        data = self.post("/api/stash").get_json()
        self.assertFalse(data["ok"])

    def test_error_response_has_error_field(self):
        data = self.post("/api/stash").get_json()
        self.assertIn("error", data)

    def test_invalid_operation_error_response(self):
        data = self.post("/api/rebase", json={
            "operation": "nonexistent"
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")

    def test_missing_required_field_error_response(self):
        state = self.get("/api/state").get_json()
        data = self.post("/api/rebase", json={
            "operation": "move"
            # Missing "order" field
        }).get_json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
