import argparse
import logging
import os
import secrets
import subprocess
import sys
import webbrowser

import flask.cli
from werkzeug.serving import make_server

import git_warp.backend
from git_warp.rest_api import create_app
from git_warp.backend import GitWarp, GitError, GitWarpError, WarpStateError


def _get_history_index(gh):
    """Get undo stack and current HEAD index. Exits on error."""
    try:
        return gh.get_history_state()
    except WarpStateError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def _set_history_index(gh, history, target_idx):
    """Move HEAD to a specific history index. Exits on error."""
    if target_idx < 0 or target_idx >= len(history):
        print(f"error: target index {target_idx} out of range (0–{len(history)-1})", file=sys.stderr)
        sys.exit(1)
    try:
        gh.reset(history[target_idx].commit_hash)
    except GitWarpError as e:
        print(f"error: {e.code}: {e.message}", file=sys.stderr)
        sys.exit(1)
    except GitError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"At history index {target_idx}.")


def main():
    parser = argparse.ArgumentParser(
        description="Rewrite branch history with unlimited undo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=0,
                        help="TCP port for the server (default: auto-assigned)")
    parser.add_argument("--clear-log", action="store_true",
                        help="delete the log file and exit")
    _color_scheme = parser.add_mutually_exclusive_group()
    _color_scheme.add_argument("--dark", action="store_true",
                                help="force dark mode (default: follow OS)")
    _color_scheme.add_argument("--light", action="store_true",
                                help="force light mode (default: follow OS)")
    _undo_redo = parser.add_mutually_exclusive_group()
    _undo_redo.add_argument("--undo", nargs="?", const=1, type=int, metavar="N",
                            help="undo the last N operations (default: 1)")
    _undo_redo.add_argument("--redo", nargs="?", const=1, type=int, metavar="N",
                            help="redo the next N operations (default: 1)")
    args = parser.parse_args()

    if args.clear_log:
        log_path, existed = git_warp.backend.clear_log()
        if existed:
            print(f"Deleted {log_path}")
        else:
            print(f"No log file at {log_path}")
        sys.exit(0)

    # Require git >= 2.26, the first release with rebase --empty=.
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
        parts = r.stdout.strip().split()  # "git version X.Y.Z"
        version_parts = parts[2].split(".")
        major, minor = int(version_parts[0]), int(version_parts[1])
        if (major, minor) < (2, 26):
            raise ValueError("version too old")
    except (FileNotFoundError, IndexError, ValueError):
        # Git not found, unknown version, or version too old.
        print("fatal: git >= 2.26 required", file=sys.stderr)
        sys.exit(1)

    # Run as command line tool for undo/redo
    if args.undo is not None:
        try:
            gh = GitWarp(os.getcwd())
        except WarpStateError as e:
            print(f"fatal: {e}", file=sys.stderr)
            sys.exit(1)
        history, idx = _get_history_index(gh)
        _set_history_index(gh, history, idx + args.undo)
        sys.exit(0)
    if args.redo is not None:
        try:
            gh = GitWarp(os.getcwd())
        except WarpStateError as e:
            print(f"fatal: {e}", file=sys.stderr)
            sys.exit(1)
        history, idx = _get_history_index(gh)
        _set_history_index(gh, history, idx - args.redo)
        sys.exit(0)
    # Run web server UI
    token = secrets.token_urlsafe(24)
    repo_path = os.getcwd()

    try:
        app = create_app(repo_path, token)
    except WarpStateError as e:
        print(f"fatal: {e}", file=sys.stderr)
        sys.exit(1)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    flask.cli.show_server_banner = lambda *_: None

    httpd = make_server("127.0.0.1", args.port, app, threaded=False)
    port = httpd.server_port

    url = f"http://127.0.0.1:{port}/?t={token}"
    if args.dark:
        url += "&dark=1"
    elif args.light:
        url += "&light=1"
    print(f"git warp running at {url}  —  Ctrl+C to quit")
    webbrowser.open(url)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
