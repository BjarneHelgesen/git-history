"""Tests for the make_test_repo.py CLI."""
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKE_SCRIPT = REPO_ROOT / "make_test_repo.py"


def _run(args, cwd):
    return subprocess.run(
        [sys.executable, str(MAKE_SCRIPT)] + args,
        capture_output=True, text=True, cwd=str(cwd),
    )


def _commit_count(repo):
    r = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                       cwd=repo, capture_output=True, text=True)
    return int(r.stdout.strip())


def test_creates_repo_with_21_commits():
    with tempfile.TemporaryDirectory() as d:
        target = str(Path(d) / "repo")
        assert _run([target], d).returncode == 0
        assert _commit_count(target) == 21


def test_fails_if_target_exists():
    with tempfile.TemporaryDirectory() as d:
        target = str(Path(d) / "repo")
        _run([target], d)
        r = _run([target], d)
        assert r.returncode != 0


def test_force_recreates_existing():
    with tempfile.TemporaryDirectory() as d:
        target = str(Path(d) / "repo")
        _run([target], d)
        r = _run([target, "--force"], d)
        assert r.returncode == 0
        assert _commit_count(target) == 21
