#!/usr/bin/env python3
"""
Create a fresh git repo populated with commits suitable for testing git-history.

Also creates a companion library repo (<name>-lib) used as a git submodule.

The repo represents building a simple todo web-app. Each commit touches a
unique file path so every rebase operation (move, squash, fixup, reword)
succeeds without merge conflicts in automated tests.

The commits are intentionally arranged to give manual testers clear targets:

  UI row  3  "Advance lib/ submodule pointer v1.0→v1.1" → pointer-only; no guard
  UI row  8  "fixup! Addd authentication form"        → practise Fixup (↵)
  UI row  9  "Addd authentication form"               → practise Reword
  UI rows 10–14  "Step N/5" commits, deliberately     → practise Drag-and-drop:
                 out of order (3, 5, 2, 4, 1)           drag into order 5→4→3→2→1
  UI row 16  "Add branch tracking for lib/ submodule"  → .gitmodules edit (guard fires)
  UI row 17  "Wire in shared-utils as a git submodule" → submodule add (guard fires)

Usage:
    python make_test_repo.py [path] [--force]

Defaults to creating ./test-repo (and ./test-lib). Pass --force to delete and
recreate them. The generated repos are fully deterministic (fixed authors,
dates, content), so commit hashes are stable across runs.
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path


# Each entry: (message, author_key, [(relpath, content), ...], tag_or_None)
#
# Special file sentinels (handled in make_commit):
#   ("__submodule_add__",    "path")  — clone lib repo as submodule at hash1
#   ("__submodule_update__", "path")  — advance submodule pointer to hash2
#
# Commits are listed oldest → newest (bottom → top in the UI).
COMMITS = [
    # ── foundation ───────────────────────────────────────────────────────────
    ("Initial commit",
     "alice",
     [("README.md",
       "# Todo App\n\nA simple web application for managing tasks.\n")],
     None),

    ("Add LICENSE",
     "alice",
     [("LICENSE",
       "MIT License\n\nCopyright (c) 2026 Todo App Authors\n")],
     None),

    ("Add .gitignore",
     "alice",
     [(".gitignore",
       "*.pyc\n__pycache__/\n.venv/\n")],
     None),

    ("Add HTTP server module",
     "alice",
     [("src/server.py",
       "from flask import Flask\napp = Flask(__name__)\n")],
     "v0.1.0"),

    ("Add configuration module",
     "bob",
     [("src/config.py",
       "DEBUG = False\nSECRET_KEY = 'change-me'\nDATABASE_URL = 'sqlite:///todos.db'\n")],
     None),

    ("Add user model",
     "alice",
     [("src/models/__init__.py", ""),
      ("src/models/user.py",
       "class User:\n"
       "    def __init__(self, id, name, email):\n"
       "        self.id = id\n"
       "        self.name = name\n"
       "        self.email = email\n")],
     "v0.2.0"),

    ("Add session middleware",
     "carol",
     [("src/middleware/__init__.py", ""),
      ("src/middleware/session.py",
       "def require_session(request):\n"
       "    return request.cookies.get('session_id')\n")],
     None),

    # ── submodule: pin to v1.0 of the shared library ─────────────────────────
    ("Wire in shared-utils as a git submodule (lib/ pinned at v1.0 — utils only)",
     "alice",
     [("__submodule_add__", "lib")],
     None),

    # ── .gitmodules edit: adds branch tracking; moving this commit is blocked ─
    ("Add branch tracking for lib/ submodule",
     "carol",
     [("__gitmodules_edit__", "lib")],
     None),

    ("Add integration tests",
     "alice",
     [("tests/__init__.py", ""),
      ("tests/test_integration.py",
       "def test_app_starts():\n    assert True\n\n"
       "def test_homepage_returns_200():\n    assert True\n")],
     "v1.0.0"),

    # ── pages: Step 1 is oldest, Step 5 is newest ────────────────────────────
    # They are committed in the scrambled order 1, 4, 2, 5, 3 so the UI shows
    # them as 3, 5, 2, 4, 1 (newest first). Goal: drag to 5, 4, 3, 2, 1.
    ("Step 1/5: Create homepage",
     "bob",
     [("pages/home.py",
       "def homepage():\n    return '<h1>Welcome to Todo App</h1>'\n")],
     None),

    ("Step 4/5: Add contact page",
     "carol",
     [("pages/contact.py",
       "def contact():\n    return '<h1>Contact us</h1>'\n")],
     None),

    ("Step 2/5: Add about page",
     "alice",
     [("pages/about.py",
       "def about():\n    return '<h1>About Todo App</h1>'\n")],
     None),

    ("Step 5/5: Add settings page",
     "bob",
     [("pages/settings.py",
       "def settings():\n    return '<h1>User Settings</h1>'\n")],
     None),

    ("Step 3/5: Add search page",
     "carol",
     [("pages/search.py",
       "def search(query=''):\n    return f'<h1>Search: {query}</h1>'\n")],
     None),

    # ── reword target: fix the typo "Addd" → "Add" ───────────────────────────
    ("Addd authentication form",
     "alice",
     [("pages/auth.py",
       "def login_form():\n    return '<form>...</form>'\n")],
     None),

    # ── fixup target: click ↵ on this row to fold it into the commit above ───
    ("fixup! Addd authentication form",
     "bob",
     [("pages/auth_styles.py",
       "LOGIN_CSS = 'form { max-width: 400px; margin: auto; }'\n")],
     None),

    # ── background commits ────────────────────────────────────────────────────
    ("Add user dashboard",
     "carol",
     [("pages/dashboard.py",
       "def dashboard(user):\n    return f'<h1>Welcome, {user.name}</h1>'\n")],
     None),

    ("Add admin panel",
     "alice",
     [("pages/admin.py",
       "def admin_panel():\n    return '<h1>Admin</h1>'\n")],
     None),

    ("Add deployment config",
     "alice",
     [("scripts/deploy.sh",
       "#!/bin/sh\nset -e\necho 'deploying todo app'\n")],
     None),

    ("Add error pages",
     "bob",
     [("pages/errors.py",
       "def not_found():\n    return '<h1>404 Not Found</h1>', 404\n\n"
       "def server_error():\n    return '<h1>500 Internal Error</h1>', 500\n")],
     None),

    # ── submodule: advance to v1.1 of the shared library ─────────────────────
    ("Advance lib/ submodule pointer v1.0 → v1.1 (picks up date-formatting helpers)",
     "bob",
     [("__submodule_update__", "lib")],
     None),

    ("Add Makefile",
     "carol",
     [("Makefile",
       ".PHONY: test\ntest:\n\tpython -m pytest tests/ -v\n")],
     None),

    # ── HEAD ─────────────────────────────────────────────────────────────────
    ("Add CI workflow",
     "bob",
     [(".github/workflows/ci.yml",
       "name: CI\n"
       "on: [push, pull_request]\n"
       "jobs:\n"
       "  test:\n"
       "    runs-on: ubuntu-latest\n"
       "    steps:\n"
       "      - uses: actions/checkout@v4\n"
       "      - run: echo running tests\n")],
     None),
]

AUTHORS = {
    "alice": ("Alice Andersen", "alice@example.com"),
    "bob":   ("Bob Brown",      "bob@example.com"),
    "carol": ("Carol Carter",   "carol@example.com"),
}

# Fixed base date so commit hashes are reproducible across runs.
BASE_DATE = datetime(2026, 2, 1, 9, 0, 0)
DAYS_BETWEEN_COMMITS = 2


def run(cmd, cwd, env=None):
    result = subprocess.run(
        cmd, cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(f"FAILED: {' '.join(cmd)}\n")
        if result.stderr:
            sys.stderr.write(result.stderr)
        sys.exit(1)
    return result


def _git_env(author_name, author_email, when):
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"]     = author_name
    env["GIT_AUTHOR_EMAIL"]    = author_email
    env["GIT_AUTHOR_DATE"]     = when
    env["GIT_COMMITTER_NAME"]  = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email
    env["GIT_COMMITTER_DATE"]  = when
    return env


def _init(repo):
    run(["git", "init", "-b", "main"],                   repo)
    run(["git", "config", "user.email",       "test@example.com"], repo)
    run(["git", "config", "user.name",        "Test User"],        repo)
    run(["git", "config", "commit.gpgsign",   "false"],            repo)
    run(["git", "config", "core.autocrlf",    "false"],            repo)
    run(["git", "config", "core.fileMode",    "false"],            repo)


def init_repo(repo):
    _init(repo)
    # Allow local (file://) submodule URLs.
    run(["git", "config", "protocol.file.allow", "always"], repo)


def create_lib_repo(lib):
    """Create a two-commit library repo; return (hash1, hash2)."""
    lib.mkdir(parents=True)
    _init(lib)

    def _commit(relpath, content, msg, author_key, day_offset):
        full = lib / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content.encode("utf-8"))
        run(["git", "add", "--", relpath], lib)
        name, email = AUTHORS[author_key]
        when = (BASE_DATE + timedelta(days=day_offset)).isoformat()
        run(["git", "commit", "-m", msg], lib, env=_git_env(name, email, when))
        return run(["git", "rev-parse", "HEAD"], lib).stdout.strip()

    hash1 = _commit(
        "utils.py",
        "def greet(name):\n    return f'Hello, {name}'\n",
        "Initial: add utils", "alice", 0,
    )
    hash2 = _commit(
        "helpers.py",
        "def format_date(d):\n    return d.strftime('%Y-%m-%d')\n",
        "Add date helpers", "bob", 2,
    )
    return hash1, hash2


def write_file(repo, relpath, content):
    full = repo / relpath
    full.parent.mkdir(parents=True, exist_ok=True)
    # Bytes write avoids any platform line-ending translation.
    full.write_bytes(content.encode("utf-8"))


def make_commit(repo, index, message, author_key, files, tag, *, sub):
    for relpath, content in files:
        if relpath == "__submodule_add__":
            path = content.strip()
            run(["git", "-c", "protocol.file.allow=always",
                 "submodule", "add", sub["url"], path], repo)
            # git submodule add checks out HEAD (hash2); pin to hash1 instead.
            run(["git", "checkout", sub["hash1"]], repo / path)
            run(["git", "add", "--", path], repo)
        elif relpath == "__submodule_update__":
            path = content.strip()
            run(["git", "checkout", sub["hash2"]], repo / path)
            run(["git", "add", "--", path], repo)
        elif relpath == "__gitmodules_edit__":
            gitmodules = repo / ".gitmodules"
            text = gitmodules.read_text(encoding="utf-8")
            gitmodules.write_text(text + "\tbranch = main\n", encoding="utf-8")
            run(["git", "add", "--", ".gitmodules"], repo)
        else:
            write_file(repo, relpath, content)
            run(["git", "add", "--", relpath], repo)

    author_name, author_email = AUTHORS[author_key]
    when = (BASE_DATE + timedelta(days=index * DAYS_BETWEEN_COMMITS)).isoformat()
    run(["git", "commit", "-m", message], repo,
        env=_git_env(author_name, author_email, when))
    if tag:
        run(["git", "tag", tag], repo)


def _remove(path):
    for p in path.rglob("*"):
        try:
            p.chmod(p.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass
    shutil.rmtree(path)


def main():
    parser = argparse.ArgumentParser(
        description="Create a fresh git repo for testing git-history.")
    parser.add_argument(
        "path", nargs="?", default="test-repo",
        help="Directory to create the repo in (default: test-repo)")
    parser.add_argument(
        "--force", action="store_true",
        help="Delete the target directories if they already exist")
    args = parser.parse_args()

    repo = Path(args.path).resolve()
    lib  = repo.parent / (repo.name + "-lib")

    for target in (repo, lib):
        if target.exists():
            if not args.force:
                sys.stderr.write(
                    f"refusing to overwrite existing path: {target}\n"
                    "pass --force to delete and recreate it\n")
                sys.exit(1)
            _remove(target)

    lib_hash1, lib_hash2 = create_lib_repo(lib)
    sub = {"url": str(lib), "hash1": lib_hash1, "hash2": lib_hash2}

    repo.mkdir(parents=True)
    init_repo(repo)
    for i, (message, author_key, files, tag) in enumerate(COMMITS):
        make_commit(repo, i, message, author_key, files, tag, sub=sub)

    print(f"Created repo at {repo} with {len(COMMITS)} commits.")
    print(f"Library repo at {lib}.")


if __name__ == "__main__":
    main()
