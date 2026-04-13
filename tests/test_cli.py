"""Tests for the git_history.py CLI entry point.

Only the early-exit error paths are tested here; the happy path (server starts,
browser opens) requires interactive verification — see manual_test.md.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_cli(args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "git_history"] + args,
        capture_output=True, text=True, cwd=str(cwd),
        env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT)},
    )


def test_not_a_git_repo():
    with tempfile.TemporaryDirectory() as d:
        r = _run_cli([], d)
        assert r.returncode != 0
        assert "not a git repository" in r.stderr


def test_detached_head():
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        subprocess.run(["git", "init", "-b", "main"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=d, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=d, capture_output=True)
        (d / "f.txt").write_text("x")
        subprocess.run(["git", "add", "f.txt"], cwd=d, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=d, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d,
                              capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "checkout", head], cwd=d, capture_output=True)
        r = _run_cli([], d)
        assert r.returncode != 0
        assert "detached" in r.stderr
