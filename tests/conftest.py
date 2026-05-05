"""Shared test helpers for repo construction."""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from make_test_repo import AUTHORS, BASE_DATE, COMMITS, create_lib_repo, init_repo, make_commit


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


def _commit_raw(repo, relpath, data, message, author_key, day_offset):
    from datetime import timedelta
    (repo / relpath).write_bytes(data)
    subprocess.run(["git", "add", "--", relpath], cwd=str(repo),
                   check=True, capture_output=True)
    author_name, author_email = AUTHORS[author_key]
    when = (BASE_DATE + timedelta(days=day_offset)).isoformat()
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"]     = author_name
    env["GIT_AUTHOR_EMAIL"]    = author_email
    env["GIT_AUTHOR_DATE"]     = when
    env["GIT_COMMITTER_NAME"]  = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email
    env["GIT_COMMITTER_DATE"]  = when
    subprocess.run(["git", "commit", "-m", message], cwd=str(repo),
                   env=env, check=True, capture_output=True)


def _build_conflict_repo(parent: Path) -> Path:
    repo = parent / "conflict-repo"
    repo.mkdir()
    init_repo(repo)
    _commit_raw(repo, "f.txt", b"line1\nline2\nline3\n",   "initial",   "alice", 0)
    _commit_raw(repo, "f.txt", b"line1\nLINE_A\nline3\n",  "version A", "bob",   1)
    _commit_raw(repo, "f.txt", b"line1\nLINE_B\nline3\n",  "version B", "carol", 2)
    return repo
