"""Shared test helpers for repo construction."""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from make_test_repo import AUTHORS, BASE_DATE, COMMITS, create_lib_repo, init_repo, make_commit


def _ensure_persistent_test_repo() -> Path:
    """Create the standard test repo in the root if it doesn't exist.

    Safe for concurrent xdist workers: a directory-based lock serializes
    creation, and each repo is built in a temp dir then atomically renamed into
    place so no caller ever observes a half-built ``.git``.
    """
    repo_root = REPO_ROOT
    repo = repo_root / ".test-repo"
    lib = repo_root / ".test-repo-lib"

    if (repo / ".git").exists():
        return repo

    lockdir = repo_root / ".test-repo.lock"
    while True:
        try:
            lockdir.mkdir()  # atomic across processes
            break
        except FileExistsError:
            if (repo / ".git").exists():  # another worker finished
                return repo
            time.sleep(0.1)

    try:
        if (repo / ".git").exists():  # re-check now that we hold the lock
            return repo

        if (lib / ".git").exists():
            lib_hash1, lib_hash2 = create_lib_repo(lib)  # reuse existing lib
        else:
            lib_tmp = repo_root / f".test-repo-lib.{os.getpid()}.tmp"
            lib_hash1, lib_hash2 = create_lib_repo(lib_tmp)
            os.replace(lib_tmp, lib)

        sub = {"url": str(lib), "hash1": lib_hash1, "hash2": lib_hash2}
        repo_tmp = repo_root / f".test-repo.{os.getpid()}.tmp"
        repo_tmp.mkdir()
        init_repo(repo_tmp)
        for i, (msg, author_key, files, tag) in enumerate(COMMITS):
            make_commit(repo_tmp, i, msg, author_key, files, tag, sub=sub)
        os.replace(repo_tmp, repo)
    finally:
        lockdir.rmdir()

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


@pytest.fixture(scope="session", autouse=True)
def _patch_werkzeug_version():
    """Workaround for Flask test client compatibility with newer werkzeug.

    Newer werkzeug versions removed __version__; Flask's test client needs it.
    """
    import werkzeug
    if not hasattr(werkzeug, "__version__"):
        from importlib.metadata import version
        werkzeug.__version__ = version("werkzeug")
