"""
Backend and REST API for git-history.

The GitHistory class wraps a git repository and exposes JSON-serializable
operations. The create_app() factory builds a Flask app that delegates each
/api/* endpoint to a GitHistory instance.
"""
import hmac
import logging
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


_EDITOR_PY = str(Path(__file__).resolve().parent.parent / "editor.py")


class GitHistory:
    def __init__(self, repo_path, start="HEAD~200"):
        self.repo = Path(repo_path)
        self._start = self._resolve_commit(start)

    # ------------------------------------------------------------------
    # low-level helpers
    # ------------------------------------------------------------------

    def _run(self, args, env=None):
        return subprocess.run(
            args, cwd=str(self.repo), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def _resolve_commit(self, ref):
        if ref is None:
            return None
        r = self._run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
        if r.returncode == 0:
            return r.stdout.strip()
        return None

    def _current_branch(self):
        r = self._run(["git", "symbolic-ref", "--short", "HEAD"])
        return r.stdout.strip() if r.returncode == 0 else ""

    def _is_dirty(self):
        r = self._run(["git", "status", "--porcelain", "--untracked-files=no"])
        return bool(r.stdout.strip())

    def _has_stash(self):
        r = self._run(["git", "stash", "list"])
        return bool(r.stdout.strip())

    def _in_rebase(self):
        git_dir = self.repo / ".git"
        return (git_dir / "rebase-merge").exists() or \
               (git_dir / "rebase-apply").exists()

    def _conflict_files(self):
        r = self._run(["git", "diff", "--name-only", "--diff-filter=U"])
        return [ln for ln in r.stdout.splitlines() if ln]

    def _conflict_response(self):
        return {
            "ok": False,
            "conflict": True,
            "conflict_files": self._conflict_files(),
            "rebase_in_progress": True,
        }

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    def read_state(self):
        return {
            "ok": True,
            "branch": self._current_branch(),
            "dirty": self._is_dirty(),
            "has_stash": self._has_stash(),
            "rebase_in_progress": self._in_rebase(),
            "conflict_files": self._conflict_files(),
            "commits": self._list_commits(),
            "branch_history": self._list_branch_history(),
        }

    def refresh(self):
        return self.read_state()

    def _list_commits(self):
        fmt = "%H%x1f%h%x1f%an%x1f%ai%x1f%B%x1f%D%x00"
        args = ["git", "log", "--abbrev=7", f"--format={fmt}"]
        if self._start:
            args.append(f"{self._start}..HEAD")
        else:
            args.append("HEAD")
        r = self._run(args)
        if r.returncode != 0:
            return []

        head_r = self._run(["git", "rev-parse", "--verify", "HEAD"])
        head_hash = head_r.stdout.strip() if head_r.returncode == 0 else ""

        commits = []
        for record in r.stdout.split("\x00"):
            record = record.lstrip("\n")
            if not record:
                continue
            parts = record.split("\x1f")
            if len(parts) < 6:
                continue
            h, sh, author, date, body, refs = parts[:6]
            branches, tags = self._parse_refs(refs)
            commits.append({
                "hash": h,
                "short_hash": sh[:7],
                "message": body.rstrip("\n"),
                "author": author,
                "date": date,
                "branches": branches,
                "tags": tags,
                "is_head": h == head_hash,
            })
        return commits

    @staticmethod
    def _parse_refs(refs):
        branches, tags = [], []
        for part in (p.strip() for p in refs.split(",")):
            if not part:
                continue
            if part.startswith("HEAD -> "):
                branches.append(part[len("HEAD -> "):])
            elif part == "HEAD":
                continue
            elif part.startswith("tag: "):
                tags.append(part[len("tag: "):])
            else:
                branches.append(part)
        return branches, tags

    @staticmethod
    def _filter_rebase_groups(entries):
        """Keep only rebase (finish) from rebase groups; filter all other rebase steps."""
        result = []
        for e in entries:
            if e["label"].startswith("rebase (finish)"):
                result.append({**e, "label": "rebase"})
            elif not e["label"].startswith("rebase"):
                result.append(e)
        return result

    def _list_branch_history(self):
        branch = self._current_branch()
        if not branch:
            return []
        r = self._run(["git", "reflog", f"refs/heads/{branch}", "--format=%H%x1f%gs%x1f%ci"])
        if r.returncode != 0:
            return []
        raw = []
        for line in r.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\x1f")
            if len(parts) < 3:
                continue
            h, label, ts = parts
            raw.append({"hash": h, "label": label, "timestamp": ts})
        # Deduplicate by hash keeping the oldest entry (last in newest-first list).
        # This ensures reset/rebase intermediates are discarded in favour of the
        # original commit label, keeping the displayed list stable after undo/redo.
        filtered = list(self._filter_rebase_groups(raw))
        last_idx = {e["hash"]: i for i, e in enumerate(filtered)}
        entries = [e for i, e in enumerate(filtered) if last_idx[e["hash"]] == i]
        return entries

    # ------------------------------------------------------------------
    # stash
    # ------------------------------------------------------------------

    def stash(self):
        if not self._is_dirty():
            return {"ok": False, "error": "nothing_to_stash"}
        r = self._run(["git", "stash", "push"])
        if r.returncode != 0:
            return {"ok": False, "error": "git_failed"}
        return self.read_state()

    def stash_pop(self):
        if not self._has_stash():
            return {"ok": False, "error": "no_stash"}
        if self._is_dirty():
            return {"ok": False, "error": "dirty_tree"}
        r = self._run(["git", "stash", "pop"])
        if r.returncode != 0:
            return {"ok": False, "error": "git_failed"}
        return self.read_state()

    # ------------------------------------------------------------------
    # reset
    # ------------------------------------------------------------------

    def reset(self, hash):
        if self._is_dirty():
            return {"ok": False, "error": "dirty_tree"}
        r = self._run(["git", "reset", "--hard", hash])
        if r.returncode != 0:
            return {"ok": False, "error": "git_failed"}
        return self.read_state()

    # ------------------------------------------------------------------
    # show
    # ------------------------------------------------------------------

    def show(self, hash):
        fmt = "%H%x00%h%x00%an%x00%ai%x00%s%x00%D"
        log_r = self._run(["git", "log", "--abbrev=7", f"--format={fmt}", "-1", hash])
        if log_r.returncode != 0:
            return {"ok": False, "error": "git_failed"}
        parts = log_r.stdout.split("\x00")
        if len(parts) < 6:
            return {"ok": False, "error": "git_failed"}
        h, sh, author, date, subject, refs = parts[:6]
        branches, tags = self._parse_refs(refs)
        commit = {
            "hash": h,
            "short_hash": sh[:7],
            "message": subject,
            "author": author,
            "date": date,
            "branches": branches,
            "tags": tags,
        }
        diff_r = self._run(["git", "show", "--format=", hash])
        if diff_r.returncode != 0:
            return {"ok": False, "error": "git_failed"}
        info = f"{commit['short_hash']} {commit['message']}"
        return {"ok": True, "commit": commit, "info": info, "diff": diff_r.stdout}

    # ------------------------------------------------------------------
    # rebase
    # ------------------------------------------------------------------

    def rebase(self, operation, hashes=None, order=None, new_message=None):
        if self._is_dirty():
            return {"ok": False, "error": "dirty_tree"}
        if self._in_rebase():
            return {"ok": False, "error": "invalid_request"}

        visible = [c["hash"] for c in self._list_commits()]
        if not visible:
            return {"ok": False, "error": "invalid_request"}

        # Validate and build a mapping from hash -> todo command (default pick).
        if operation == "move":
            if order is None or sorted(order) != sorted(visible):
                return {"ok": False, "error": "invalid_request"}
            if order == visible:
                return self.read_state()
            todo_hashes = list(reversed(order))
            mark = {}
        elif operation in ("squash", "fixup"):
            if not hashes or any(h not in visible for h in hashes):
                return {"ok": False, "error": "invalid_request"}
            todo_hashes = list(reversed(visible))
            if len(hashes) == 1:
                mark = {hashes[0]: operation}
            else:
                # The oldest in the group stays as pick; the rest fold into it.
                oldest_in_group = max(hashes, key=visible.index)
                mark = {h: operation for h in hashes if h != oldest_in_group}
        elif operation == "reword":
            if (not hashes or len(hashes) != 1 or new_message is None
                    or hashes[0] not in visible):
                return {"ok": False, "error": "invalid_request"}
            todo_hashes = list(reversed(visible))
            mark = {}
        else:
            return {"ok": False, "error": "invalid_request"}

        # Squash/fixup of the oldest visible commit needs something to fold
        # into, so extend the rebase base by one and put the old base at the
        # top of the todo as a plain pick.
        oldest_visible = visible[-1]
        extend = (operation in ("squash", "fixup")
                  and oldest_visible in (hashes or []))
        base = self._start
        if extend:
            if base is None:
                # Root commit is the squash target (nothing to fold into).
                if oldest_visible in mark:
                    return {"ok": False, "error": "invalid_request"}
            else:
                parent = self._resolve_commit(f"{base}^")
                if parent is None:
                    return {"ok": False, "error": "invalid_request"}
                todo_hashes = [base] + todo_hashes
                base = parent

        msg_path = None
        if operation == "reword":
            msg_path = self._write_tempfile(new_message + "\n")
            todo_lines = []
            for h in todo_hashes:
                todo_lines.append(f"pick {h}")
                if h == hashes[0]:
                    todo_lines.append(
                        f"exec git commit --amend --allow-empty -F {shlex.quote(msg_path)}"
                    )
        else:
            todo_lines = [f"{mark.get(h, 'pick')} {h}" for h in todo_hashes]

        todo_path = self._write_tempfile("\n".join(todo_lines) + "\n")

        try:
            env = self._rebase_env(todo_path=todo_path, msg_path=msg_path)
            cmd = ["git", "rebase", "-i", "--keep-empty", "--empty=keep"]
            if base is None:
                cmd.append("--root")
            else:
                cmd.append(base)
            r = self._run(cmd, env=env)
        finally:
            for p in (todo_path, msg_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

        if r.returncode != 0 and not self._in_rebase():
            return {"ok": False, "error": "git_failed"}

        # Squashing commits whose combined diff is empty (e.g. a file that is
        # created then deleted) leaves git paused mid-rebase despite
        # --empty=keep. One --continue drives it to completion.
        if self._in_rebase():
            if self._conflict_files():
                return self._conflict_response()
            ce = os.environ.copy()
            ce["GIT_EDITOR"] = "true"
            ce["GIT_SEQUENCE_EDITOR"] = "true"
            self._run(["git", "rebase", "--continue"], env=ce)
            if self._in_rebase():
                return self._conflict_response()

        if extend and self._start is not None:
            # Old _start and the oldest visible commit were folded together;
            # advance _start to the resulting commit so the visible range is
            # stable across subsequent operations.
            new_visible = len(visible) - sum(1 for h in hashes if h in visible)
            new_start = self._resolve_commit(f"HEAD~{new_visible}")
            if new_start is None:
                return {"ok": False, "error": "start_update_failed"}
            self._start = new_start

        return self.read_state()

    def rebase_continue(self):
        if not self._in_rebase():
            return {"ok": False, "error": "invalid_request"}
        if self._conflict_files():
            return self._conflict_response()
        env = os.environ.copy()
        env["GIT_EDITOR"] = "true"
        env["GIT_SEQUENCE_EDITOR"] = "true"
        r = self._run(["git", "rebase", "--continue"], env=env)
        if self._in_rebase():
            return self._conflict_response()
        if r.returncode != 0:
            return {"ok": False, "error": "git_failed"}
        return self.read_state()

    def rebase_abort(self):
        if self._in_rebase():
            r = self._run(["git", "rebase", "--abort"])
            if r.returncode != 0:
                return {"ok": False, "error": "git_failed"}
        return self.read_state()

    # ------------------------------------------------------------------
    # rebase helpers
    # ------------------------------------------------------------------

    def _rebase_env(self, todo_path=None, msg_path=None):
        env = os.environ.copy()
        editor_cmd = "{} {}".format(
            shlex.quote(sys.executable), shlex.quote(_EDITOR_PY),
        )
        env["GIT_SEQUENCE_EDITOR"] = editor_cmd
        env["GIT_EDITOR"] = editor_cmd
        if todo_path:
            env["GIT_HISTORY_TODO"] = todo_path
        if msg_path:
            env["GIT_HISTORY_MSG"] = msg_path
        return env

    @staticmethod
    def _write_tempfile(content):
        fd, path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        return path


# ------------------------------------------------------------------
# Flask REST API
# ------------------------------------------------------------------

def create_app(repo_path, token):
    from flask import Flask, request, jsonify, send_from_directory, abort

    _static = str(Path(__file__).parent / "static")
    app = Flask(__name__, static_folder=_static, static_url_path="/static")
    app.config["GH"] = GitHistory(repo_path)
    app.config["TOKEN"] = token

    @app.before_request
    def auth():
        if request.path.startswith("/api/"):
            if not hmac.compare_digest(
                    request.headers.get("X-Token", ""), app.config["TOKEN"]):
                abort(403)

    @app.route("/")
    def index():
        return send_from_directory(_static, "index.html")

    @app.route("/manual")
    def manual():
        return send_from_directory(_static, "manual.html")

    @app.route("/api/state")
    def api_state():
        return jsonify(app.config["GH"].read_state())

    @app.route("/api/refresh", methods=["POST"])
    def api_refresh():
        return jsonify(app.config["GH"].read_state())

    @app.route("/api/stash", methods=["POST"])
    def api_stash():
        return jsonify(app.config["GH"].stash())

    @app.route("/api/stash/pop", methods=["POST"])
    def api_stash_pop():
        return jsonify(app.config["GH"].stash_pop())

    @app.route("/api/rebase", methods=["POST"])
    def api_rebase():
        body = request.get_json(silent=True) or {}
        operation = body.get("operation")
        if not operation:
            return jsonify({"ok": False, "error": "invalid_request"})
        return jsonify(app.config["GH"].rebase(
            operation=operation,
            hashes=body.get("hashes"),
            order=body.get("order"),
            new_message=body.get("new_message")))

    @app.route("/api/rebase/continue", methods=["POST"])
    def api_rebase_continue():
        return jsonify(app.config["GH"].rebase_continue())

    @app.route("/api/rebase/abort", methods=["POST"])
    def api_rebase_abort():
        return jsonify(app.config["GH"].rebase_abort())

    @app.route("/api/reset", methods=["POST"])
    def api_reset():
        body = request.get_json(silent=True) or {}
        return jsonify(app.config["GH"].reset(body.get("hash", "")))

    @app.route("/api/show")
    def api_show():
        return jsonify(app.config["GH"].show(request.args.get("hash", "")))

    @app.route("/api/quit", methods=["POST"])
    def api_quit():
        response = jsonify({"ok": True})
        response.call_on_close(lambda: os._exit(0))
        return response

    return app


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main():
    import argparse
    import secrets
    import socket
    import webbrowser

    parser = argparse.ArgumentParser(
        description="Interactive git history rewriter — reorder, squash, fixup, and reword commits in the browser. Includes a full undo and redo stack.",
        epilog=(
            "Install:  pip install git+https://github.com/BjarneHelgesen/git-history\n"
            "Full manual available at /manual once the server is running."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("start", nargs="?", default="HEAD~200",
                        help="oldest commit to show (default: HEAD~200)")
    parser.add_argument("--port", type=int, default=0,
                        help="TCP port for the server (default: auto-assigned)")
    args = parser.parse_args()

    # Must be inside a git repo.
    r = subprocess.run(["git", "rev-parse", "--git-dir"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("fatal: not a git repository", file=sys.stderr)
        sys.exit(1)

    # Reject detached HEAD.
    r = subprocess.run(["git", "symbolic-ref", "--quiet", "HEAD"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("fatal: HEAD is detached; checkout a branch first",
              file=sys.stderr)
        sys.exit(1)

    # Pick a port.
    port = args.port
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

    token = secrets.token_urlsafe(24)
    repo_path = os.getcwd()

    app = create_app(repo_path, token)
    # Override the default start if the user passed one.
    app.config["GH"] = GitHistory(repo_path, start=args.start)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    import flask.cli
    flask.cli.show_server_banner = lambda *_: None

    url = f"http://127.0.0.1:{port}/?t={token}"
    print(f"git-history running at {url}  —  Ctrl+C to quit")
    webbrowser.open(url)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
