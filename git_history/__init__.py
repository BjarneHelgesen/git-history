"""
Backend and REST API for git-history.

The GitHistory class wraps a git repository and exposes JSON-serializable
operations. The create_app() factory builds a Flask app that delegates each
/api/* endpoint to a GitHistory instance.
"""
import datetime
import hmac
import logging
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

_LOG_PATH = Path.home() / ".git-history.log"


_EDITOR_PY = str(Path(__file__).parent / "editor.py")


class GitHistory:
    def __init__(self, repo_path):
        self.repo = Path(repo_path)
        self._start = self._resolve_commit("HEAD~200")

    # ------------------------------------------------------------------
    # low-level helpers
    # ------------------------------------------------------------------

    def _run(self, args, env=None):
        return subprocess.run(
            args, cwd=str(self.repo), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def _git_err(self, r):
        return {"ok": False, "error": "git_failed", "message": r.stderr.strip()}

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

    def _list_local_branches(self):
        r = self._run(["git", "branch", "--format=%(refname:short)"])
        return [b.strip() for b in r.stdout.splitlines() if b.strip()]

    def _is_dirty(self):
        r = self._run(["git", "status", "--porcelain", "--untracked-files=no"])
        return bool(r.stdout.strip())

    def _has_stash(self):
        r = self._run(["git", "stash", "list"])
        return bool(r.stdout.strip())

    def _append_log(self):
        branch = self._current_branch()
        head = self._resolve_commit("HEAD")
        if branch and head:
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{ts} {branch} {head}\n")

    def _in_rebase(self):
        r = self._run(["git", "rev-parse", "--git-dir"])
        if r.returncode != 0:
            return False
        git_dir = Path(r.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = (self.repo / git_dir).resolve()
        return (git_dir / "rebase-merge").exists() or \
               (git_dir / "rebase-apply").exists()

    def _conflict_files(self):
        r = self._run(["git", "diff", "--name-only", "--diff-filter=U"])
        return [ln for ln in r.stdout.splitlines() if ln]

    def _get_gitmodules(self, hash):
        r = self._run(["git", "show", f"{hash}:.gitmodules"])
        return r.stdout if r.returncode == 0 else ""

    def _gitlinks_at(self, hash):
        r = self._run(["git", "ls-tree", "-r", hash])
        result = {}
        for line in r.stdout.splitlines():
            if not line.startswith("160000 "):
                continue
            tab = line.find("\t")
            if tab != -1:
                parts = line[:tab].split()
                if len(parts) >= 3:
                    result[line[tab + 1:]] = parts[2]
        return result

    def _commit_touches_gitmodules(self, hash):
        r = self._run(["git", "diff-tree", "--no-commit-id", "-r", "--name-only", hash])
        return ".gitmodules" in r.stdout.splitlines()

    def _any_moved_commit_touches_gitmodules(self, current_order, new_order):
        moved = [h for h, h2 in zip(current_order, new_order) if h != h2]
        return any(self._commit_touches_gitmodules(h) for h in moved)

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
            "branches": self._list_local_branches(),
            "dirty": self._is_dirty(),
            "has_stash": self._has_stash(),
            "rebase_in_progress": self._in_rebase(),
            "conflict_files": self._conflict_files(),
            "commits": self._list_commits(),
            "branch_history": self._list_branch_history(),
        }


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
        seen = set()
        entries = []
        for e in reversed(filtered):
            if e["hash"] not in seen:
                seen.add(e["hash"])
                entries.append(e)
        entries.reverse()
        return entries

    # ------------------------------------------------------------------
    # stash
    # ------------------------------------------------------------------

    def stash(self):
        if self._in_rebase():
            return {"ok": False, "error": "rebase_in_progress"}
        if not self._is_dirty():
            return {"ok": False, "error": "nothing_to_stash"}
        r = self._run(["git", "stash", "push"])
        if r.returncode != 0:
            return self._git_err(r)
        return self.read_state()

    def stash_pop(self):
        if self._in_rebase():
            return {"ok": False, "error": "rebase_in_progress"}
        if not self._has_stash():
            return {"ok": False, "error": "no_stash"}
        if self._is_dirty():
            return {"ok": False, "error": "dirty_tree"}
        r = self._run(["git", "stash", "pop"])
        if r.returncode != 0:
            return self._git_err(r)
        return self.read_state()

    # ------------------------------------------------------------------
    # reset
    # ------------------------------------------------------------------

    def reset(self, hash):
        if self._in_rebase():
            return {"ok": False, "error": "rebase_in_progress"}
        resolved_hash = self._resolve_commit(hash)
        if resolved_hash is None:
            return {"ok": False, "error": "invalid_commit"}
        head_hash = self._resolve_commit("HEAD")
        if head_hash and self._get_gitmodules(head_hash) != self._get_gitmodules(resolved_hash):
            return {"ok": False, "error": "gitmodules_differ"}
        if self._is_dirty():
            return {"ok": False, "error": "dirty_tree"}
        before_links = self._gitlinks_at(head_hash) if head_hash else {}
        r = self._run(["git", "reset", "--hard", resolved_hash])
        if r.returncode != 0:
            return self._git_err(r)
        self._append_log()
        after_links = self._gitlinks_at(resolved_hash)
        state = self.read_state()
        if before_links != after_links:
            state["submodule_update_suggested"] = True
        return state

    def submodule_update(self):
        r = self._run(["git", "submodule", "update", "--init"])
        if r.returncode != 0:
            return self._git_err(r)
        return self.read_state()

    def switch_branch(self, branch):
        if self._in_rebase():
            return {"ok": False, "error": "rebase_in_progress"}
        if self._is_dirty():
            return {"ok": False, "error": "dirty_tree"}
        if branch not in self._list_local_branches():
            return {"ok": False, "error": "invalid_branch"}
        before_links = self._gitlinks_at(self._resolve_commit("HEAD"))
        r = self._run(["git", "switch", branch])
        if r.returncode != 0:
            return self._git_err(r)
        self._start = self._resolve_commit("HEAD~200")
        state = self.read_state()
        if self._gitlinks_at(self._resolve_commit("HEAD")) != before_links:
            state["submodule_update_suggested"] = True
        return state

    # ------------------------------------------------------------------
    # show
    # ------------------------------------------------------------------

    def show(self, hash):
        fmt = "%H%x00%h%x00%an%x00%ai%x00%s%x00%D"
        log_r = self._run(["git", "log", "--abbrev=7", f"--format={fmt}", "-1", hash])
        if log_r.returncode != 0:
            return self._git_err(log_r)
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
            return self._git_err(diff_r)
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
        # Each branch computes a minimal rebase range: only the commits that
        # actually need to change are included in the todo.  This prevents
        # git from replaying unrelated submodule commits when the user only
        # wants to touch the top one or two commits.
        if operation == "move":
            if order is None or sorted(order) != sorted(visible):
                return {"ok": False, "error": "invalid_request"}
            if order == visible:
                return self.read_state()
            if self._any_moved_commit_touches_gitmodules(visible, order):
                return {"ok": False, "error": "gitmodules_in_range"}
            # Minimal range: only go back as far as the oldest position that
            # actually changes.  Commits below that position are untouched.
            changed_idx = [i for i, (h, h2) in enumerate(zip(visible, order)) if h != h2]
            oldest_changed = max(changed_idx)
            range_set = set(visible[:oldest_changed + 1])
            order_slice = [h for h in order if h in range_set]
            todo_hashes = list(reversed(order_slice))
            if oldest_changed + 1 < len(visible):
                base = visible[oldest_changed + 1]
            else:
                base = self._start
            mark = {}
        elif operation in ("squash", "fixup"):
            if not hashes or any(h not in visible for h in hashes):
                return {"ok": False, "error": "invalid_request"}
            # Validate that hashes are consecutive (adjacent) in the visible history.
            indices = sorted(visible.index(h) for h in hashes)
            if indices != list(range(indices[0], indices[-1] + 1)):
                return {"ok": False, "error": "invalid_request"}
            if len(hashes) == 1:
                mark = {hashes[0]: operation}
            else:
                # The oldest in the group stays as pick; the rest fold into it.
                oldest_in_group = max(hashes, key=visible.index)
                mark = {h: operation for h in hashes if h != oldest_in_group}
            # Minimal range: include the fold target (oldest_idx + 1) and
            # everything above it up to HEAD.  visible[:oldest_idx+2] clamps
            # naturally to all-of-visible when oldest_idx is near the end.
            oldest_idx = indices[-1]
            todo_hashes = list(reversed(visible[:oldest_idx + 2]))
            if oldest_idx + 2 < len(visible):
                base = visible[oldest_idx + 2]
            else:
                base = self._start
        elif operation == "reword":
            if (not hashes or len(hashes) != 1 or new_message is None
                    or hashes[0] not in visible):
                return {"ok": False, "error": "invalid_request"}
            # Minimal range: replay only the reworded commit and those above it.
            idx = visible.index(hashes[0])
            todo_hashes = list(reversed(visible[:idx + 1]))
            if idx + 1 < len(visible):
                base = visible[idx + 1]
            else:
                base = self._start
            mark = {}
        else:
            return {"ok": False, "error": "invalid_request"}

        # Squash/fixup of the oldest visible commit needs something to fold
        # into, so extend the rebase base by one and put the old base at the
        # top of the todo as a plain pick.
        oldest_visible = visible[-1]
        extend = (operation in ("squash", "fixup")
                  and oldest_visible in (hashes or []))
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
            return self._git_err(r)

        # Squashing commits whose combined diff is empty (e.g. a file that is
        # created then deleted) leaves git paused mid-rebase despite
        # --empty=keep. One --continue drives it to completion.
        if self._in_rebase():
            err = self._drive_continue()
            if err is not None:
                return err

        if extend and self._start is not None:
            # Old _start and the oldest visible commit were folded together;
            # advance _start to the resulting commit so the visible range is
            # stable across subsequent operations.
            new_visible = len(visible) - sum(1 for h in hashes if h in visible)
            new_start = self._resolve_commit(f"HEAD~{new_visible}")
            if new_start is None:
                return {"ok": False, "error": "start_update_failed"}
            self._start = new_start

        self._append_log()
        before_links = self._gitlinks_at(visible[0])
        after_links = self._gitlinks_at(self._resolve_commit("HEAD"))
        state = self.read_state()
        if before_links != after_links:
            state["submodule_update_suggested"] = True
        return state

    def rebase_continue(self):
        if not self._in_rebase():
            return {"ok": False, "error": "invalid_request"}
        err = self._drive_continue()
        if err is not None:
            return err
        self._append_log()
        orig_head = self._resolve_commit("ORIG_HEAD")
        before_links = self._gitlinks_at(orig_head) if orig_head else {}
        after_links = self._gitlinks_at(self._resolve_commit("HEAD"))
        state = self.read_state()
        if before_links != after_links:
            state["submodule_update_suggested"] = True
        return state

    def rebase_abort(self):
        if self._in_rebase():
            r = self._run(["git", "rebase", "--abort"])
            if r.returncode != 0:
                return self._git_err(r)
        return self.read_state()

    # ------------------------------------------------------------------
    # rebase helpers
    # ------------------------------------------------------------------

    def _drive_continue(self):
        if self._conflict_files():
            return self._conflict_response()
        env = self._rebase_env()
        r = self._run(["git", "rebase", "--continue"], env=env)
        if self._in_rebase():
            return self._conflict_response()
        if r.returncode != 0:
            return self._git_err(r)
        return None

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
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
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

    @app.route("/api/submodule/update", methods=["POST"])
    def api_submodule_update():
        return jsonify(app.config["GH"].submodule_update())

    @app.route("/api/switch", methods=["POST"])
    def api_switch():
        body = request.get_json(silent=True) or {}
        return jsonify(app.config["GH"].switch_branch(body.get("branch", "")))

    @app.route("/api/show")
    def api_show():
        return jsonify(app.config["GH"].show(request.args.get("hash", "")))

    @app.route("/log")
    def log_view():
        from flask import Response
        try:
            content = _LOG_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        return Response(content, mimetype="text/plain")

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
    parser.add_argument("--port", type=int, default=0,
                        help="TCP port for the server (default: auto-assigned)")
    parser.add_argument("--clear-log", action="store_true",
                        help="delete the log file and exit")
    args = parser.parse_args()

    if args.clear_log:
        if _LOG_PATH.exists():
            _LOG_PATH.unlink()
            print(f"Deleted {_LOG_PATH}")
        else:
            print(f"No log file at {_LOG_PATH}")
        sys.exit(0)

    # Require git >= 2.26.
    r = subprocess.run(["git", "--version"], capture_output=True, text=True)
    try:
        parts = r.stdout.strip().split()  # "git version X.Y.Z"
        version_parts = parts[2].split(".")
        major, minor = int(version_parts[0]), int(version_parts[1])
        assert  (major, minor) >= (2, 26)
    except:
        print(f"fatal: git >= 2.26 required", file=sys.stderr) # Git not found, unknown version or version too old.
        sys.exit(1)

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

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    import flask.cli
    flask.cli.show_server_banner = lambda *_: None

    url = f"http://127.0.0.1:{port}/?t={token}"
    print(f"git-history running at {url}  —  Ctrl+C to quit")
    webbrowser.open(url)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=False)


if __name__ == "__main__":
    main()
