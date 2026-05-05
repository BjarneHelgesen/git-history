"""
Backend and REST API for git-history.

The GitHistory class wraps a git repository and exposes JSON-serializable
operations. The create_app() factory builds a Flask app that delegates each
/api/* endpoint to a GitHistory instance.
"""
import argparse
import datetime
from dataclasses import dataclass, asdict
import hmac
import logging
import os
import secrets
import shlex
import socket
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

import flask
import flask.cli
from flask import Flask, request, jsonify, send_from_directory, abort, Response

_LOG_PATH = Path.home() / ".git-history.log"

# Limit visible commits to this many to avoid overwhelming the UI and slow performance on large repos.
_HISTORY_DEPTH = 200

_EDITOR_PY = str(Path(__file__).parent / "editor.py")


@dataclass
class Commit:
    commit_hash: str
    short_hash: str
    message: str
    author: str
    date: str
    branches: list
    tags: list
    is_head: bool = False
    pushed: bool = False


@dataclass
class BranchHistoryEntry:
    commit_hash: str
    label: str
    timestamp: str


@dataclass
class StateResponse:
    branch: str
    branches: list
    dirty: bool
    has_stash: bool
    rebase_in_progress: bool
    conflict_files: list
    commits: list
    branch_history: list
    ok: bool = True
    submodule_update_suggested: bool = False
    conflict: bool = False


@dataclass
class ErrorResponse:
    error: str
    message: str = ""
    ok: bool = False


@dataclass
class ShowResponse:
    commit: Commit
    diff: str
    ok: bool = True


@dataclass
class _RebaseInstructions:
    todo_lines: list
    base: str = None       # None means --root
    msg_path: str = None   # temp file to unlink; reword only
    extends_base: bool = False
    hashes: list = None    # hashes involved in the operation (for fold operations)


class GitHistory:
    def __init__(self, repo_path, log_path=_LOG_PATH):
        self.repo = Path(repo_path)
        self._log_path = Path(log_path)
        self._start = self._resolve_commit(f"HEAD~{_HISTORY_DEPTH}")

    # ------------------------------------------------------------------
    # low-level helpers
    # ------------------------------------------------------------------

    def _run(self, args, env=None):
        return subprocess.run(
            args, cwd=str(self.repo), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )

    def _git_err(self, r):
        return ErrorResponse(error="git_failed", message=r.stderr.strip())

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

    def _is_dirty_excluding_submodules(self):
        r = self._run(["git", "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"])
        return bool(r.stdout.strip())

    def _has_stash(self):
        r = self._run(["git", "stash", "list"])
        return bool(r.stdout.strip())

    def _append_log(self):
        branch = self._current_branch()
        head = self._resolve_commit("HEAD")
        if branch and head:
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(f"{ts} {branch} {head}\n")
            except OSError:
                pass

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

    def _get_gitmodules(self, commit_hash):
        r = self._run(["git", "show", f"{commit_hash}:.gitmodules"])
        return r.stdout if r.returncode == 0 else ""

    def _gitlinks_at(self, commit_hash):
        r = self._run(["git", "ls-tree", "-r", commit_hash])
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

    def _commit_touches_gitmodules(self, commit_hash):
        r = self._run(["git", "diff-tree", "--no-commit-id", "-r", "--name-only", commit_hash])
        return ".gitmodules" in r.stdout.splitlines()

    def _any_moved_commit_touches_gitmodules(self, current_order, new_order):
        moved = [h for h, h2 in zip(current_order, new_order) if h != h2]
        return any(self._commit_touches_gitmodules(h) for h in moved)

    def _conflict_response(self):
        state = self.read_state()
        state.ok = False
        state.conflict = True
        return state

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    def read_state(self, submodule_update_suggested=False):
        branch = self._current_branch()
        commits = self._list_commits()
        pushed = self._get_pushed_hashes(commits, branch)
        for c in commits:
            c.pushed = c.commit_hash in pushed
        return StateResponse(
            branch=branch,
            branches=self._list_local_branches(),
            dirty=self._is_dirty(),
            has_stash=self._has_stash(),
            rebase_in_progress=self._in_rebase(),
            conflict_files=self._conflict_files(),
            commits=commits,
            branch_history=self._list_branch_history(),
            submodule_update_suggested=submodule_update_suggested,
        )


    def _get_pushed_hashes(self, commits, branch):
        if not branch:
            return set()
        r_upstream = self._run(["git", "rev-parse", "--symbolic-full-name", "@{upstream}"])
        if r_upstream.returncode != 0:
            return set()
        remote_ref = r_upstream.stdout.strip()
        r = self._run(["git", "log", "--format=%H", f"{remote_ref}..HEAD"])
        if r.returncode != 0:
            return set()
        unpushed = set(r.stdout.splitlines())
        return {c.commit_hash for c in commits} - unpushed

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
            commits.append(Commit(
                commit_hash=h,
                short_hash=sh[:7],
                message=body.rstrip("\n"),
                author=author,
                date=date,
                branches=branches,
                tags=tags,
                is_head=h == head_hash,
            ))
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
            if e.label.startswith("rebase (finish)"):
                result.append(BranchHistoryEntry(commit_hash=e.commit_hash, label="rebase", timestamp=e.timestamp))
            elif not e.label.startswith("rebase"):
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
            raw.append(BranchHistoryEntry(commit_hash=h, label=label, timestamp=ts))
        # Deduplicate by hash keeping the oldest entry (last in newest-first list).
        # This ensures reset/rebase intermediates are discarded in favour of the
        # original commit label, keeping the displayed list stable after undo/redo.
        filtered = list(self._filter_rebase_groups(raw))
        seen = set()
        oldest_first = []
        for e in reversed(filtered):
            if e.commit_hash not in seen:
                seen.add(e.commit_hash)
                oldest_first.append(e)
        return list(reversed(oldest_first))

    # ------------------------------------------------------------------
    # stash
    # ------------------------------------------------------------------

    def stash(self):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        if not self._is_dirty():
            return ErrorResponse(error="nothing_to_stash")
        r = self._run(["git", "stash", "push"])
        if r.returncode != 0:
            return self._git_err(r)
        return self.read_state()

    def stash_pop(self):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        if not self._has_stash():
            return ErrorResponse(error="no_stash")
        if self._is_dirty():
            return ErrorResponse(error="dirty_tree")
        r = self._run(["git", "stash", "pop"])
        if r.returncode != 0:
            return self._git_err(r)
        return self.read_state()

    # ------------------------------------------------------------------
    # reset
    # ------------------------------------------------------------------

    def reset(self, commit_hash):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        resolved_hash = self._resolve_commit(commit_hash)
        if resolved_hash is None:
            return ErrorResponse(error="invalid_commit")
        head_hash = self._resolve_commit("HEAD")
        if head_hash and self._get_gitmodules(head_hash) != self._get_gitmodules(resolved_hash):
            return ErrorResponse(error="gitmodules_differ")
        if self._is_dirty():
            return ErrorResponse(error="dirty_tree")
        before_links = self._gitlinks_at(head_hash) if head_hash else {}
        r = self._run(["git", "reset", "--hard", resolved_hash])
        if r.returncode != 0:
            return self._git_err(r)
        self._append_log()
        after_links = self._gitlinks_at(resolved_hash)
        return self.read_state(submodule_update_suggested=before_links != after_links)

    def submodule_update(self):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        r = self._run(["git", "submodule", "update", "--init"])
        if r.returncode != 0:
            return self._git_err(r)
        return self.read_state()

    def switch_branch(self, branch):
        if self._in_rebase():
            return ErrorResponse(error="rebase_in_progress")
        if self._is_dirty():
            return ErrorResponse(error="dirty_tree")
        if branch not in self._list_local_branches():
            return ErrorResponse(error="invalid_branch")
        before_links = self._gitlinks_at(self._resolve_commit("HEAD"))
        r = self._run(["git", "switch", branch])
        if r.returncode != 0:
            return self._git_err(r)
        self._start = self._resolve_commit(f"HEAD~{_HISTORY_DEPTH}")
        return self.read_state(submodule_update_suggested=self._gitlinks_at(self._resolve_commit("HEAD")) != before_links)

    # ------------------------------------------------------------------
    # show
    # ------------------------------------------------------------------

    def show(self, commit_hash):
        resolved = self._resolve_commit(commit_hash)
        if resolved is None:
            return ErrorResponse(error="invalid_commit")
        fmt = "%H%x00%h%x00%an%x00%ai%x00%B%x00%D"
        log_r = self._run(["git", "log", "--abbrev=7", f"--format={fmt}", "-1", resolved])
        if log_r.returncode != 0:
            return self._git_err(log_r)
        parts = log_r.stdout.split("\x00")
        if len(parts) < 6:
            return ErrorResponse(error="git_failed")
        h, sh, author, date, body, refs = parts[:6]
        branches, tags = self._parse_refs(refs)
        commit = Commit(
            commit_hash=h,
            short_hash=sh[:7],
            message=body.rstrip("\n"),
            author=author,
            date=date,
            branches=branches,
            tags=tags,
        )
        diff_r = self._run(["git", "show", "--format=", resolved])
        if diff_r.returncode != 0:
            return self._git_err(diff_r)
        return ShowResponse(commit=commit, diff=diff_r.stdout)

    def read_log(self) -> str:
        try:
            return self._log_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    # ------------------------------------------------------------------
    # rebase
    # ------------------------------------------------------------------

    def move(self, order):
        instr = self._move_instructions(order)
        if isinstance(instr, ErrorResponse):
            return instr
        if not instr.todo_lines:
            return self.read_state()
        return self._rebase(instr)

    def squash(self, hashes):
        instr = self._squash_instructions(hashes)
        return self._rebase(instr)

    def fixup(self, hashes):
        instr = self._fixup_instructions(hashes)
        return self._rebase(instr)

    def reword(self, commit_hash, message):
        instr = self._reword_instructions(commit_hash, message)
        return self._rebase(instr)

    def _rebase(self, instr):
        if isinstance(instr, ErrorResponse):
            return instr
        if self._is_dirty():
            return ErrorResponse(error="dirty_tree")
        if self._in_rebase():
            return ErrorResponse(error="invalid_request")

        visible = [c.commit_hash for c in self._list_commits()]
        if not visible:
            return ErrorResponse(error="invalid_request")

        todo_path = None
        try:
            todo_path = self._write_tempfile("\n".join(instr.todo_lines) + "\n")
            env = self._rebase_env(todo_path=todo_path, msg_path=instr.msg_path)
            cmd = ["git", "rebase", "-i", "--keep-empty", "--empty=keep"]
            cmd.append("--root" if instr.base is None else instr.base)
            r = self._run(cmd, env=env)
        finally:
            # Safe to delete both here: reword cannot conflict (pick replays
            # onto its own parent), so the exec step always runs within _run.
            for p in (todo_path, instr.msg_path if instr else None):
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

        if instr.extends_base and self._start is not None:
            # Old _start and the oldest visible commit were folded together;
            # advance _start to the resulting commit so the visible range is
            # stable across subsequent operations.
            new_visible = len(visible) - sum(1 for h in instr.hashes if h in visible)
            new_start = self._resolve_commit(f"HEAD~{new_visible}")
            if new_start is None:
                return ErrorResponse(error="start_update_failed")
            self._start = new_start

        self._append_log()
        before_links = self._gitlinks_at(visible[0])
        after_links = self._gitlinks_at(self._resolve_commit("HEAD"))
        return self.read_state(submodule_update_suggested=before_links != after_links)

    def rebase_continue(self):
        if not self._in_rebase():
            return ErrorResponse(error="not_in_rebase")
        err = self._drive_continue()
        if err is not None:
            return err
        self._append_log()
        orig_head = self._resolve_commit("ORIG_HEAD")
        before_links = self._gitlinks_at(orig_head) if orig_head else {}
        after_links = self._gitlinks_at(self._resolve_commit("HEAD"))
        return self.read_state(submodule_update_suggested=before_links != after_links)

    def rebase_abort(self):
        if not self._in_rebase():
            return ErrorResponse(ok=False, error="not_in_rebase")
        r = self._run(["git", "rebase", "--abort"])
        if r.returncode != 0:
            return self._git_err(r)
        return self.read_state()

    # ------------------------------------------------------------------
    # rebase helpers
    # ------------------------------------------------------------------

    def _move_instructions(self, order):
        visible = [c.commit_hash for c in self._list_commits()]
        if order is None or sorted(order) != sorted(visible):
            return ErrorResponse(error="invalid_request")
        if self._any_moved_commit_touches_gitmodules(visible, order):
            return ErrorResponse(error="gitmodules_in_range")
        changed_idx = [i for i, (h, h2) in enumerate(zip(visible, order)) if h != h2]
        if not changed_idx:
            return _RebaseInstructions(todo_lines=[], base=None)
        oldest_changed = max(changed_idx)
        range_set = set(visible[:oldest_changed + 1])
        todo_hashes = list(reversed([h for h in order if h in range_set]))
        base = visible[oldest_changed + 1] if oldest_changed + 1 < len(visible) else self._start
        return _RebaseInstructions(todo_lines=[f"pick {h}" for h in todo_hashes], base=base)

    def _squash_instructions(self, hashes):
        return self._fold_instructions(hashes, "squash")

    def _fixup_instructions(self, hashes):
        return self._fold_instructions(hashes, "fixup")

    def _fold_instructions(self, hashes, operation):
        visible = [c.commit_hash for c in self._list_commits()]
        if not hashes or any(h not in visible for h in hashes):
            return ErrorResponse(error="invalid_request")
        indices = sorted(visible.index(h) for h in hashes)
        if indices != list(range(indices[0], indices[-1] + 1)):
            return ErrorResponse(error="invalid_request")
        if len(hashes) == 1:
            rebase_commands = {hashes[0]: operation}
        else:
            # The oldest in the group stays as pick; the rest fold into it.
            oldest_in_group = max(hashes, key=visible.index)
            rebase_commands = {h: operation for h in hashes if h != oldest_in_group}
        oldest_idx = indices[-1]
        todo_hashes = list(reversed(visible[:oldest_idx + 2]))
        base = visible[oldest_idx + 2] if oldest_idx + 2 < len(visible) else self._start
        extends_base = visible[-1] in hashes
        if extends_base:
            if base is None:
                # Root commit is the squash target (nothing to fold into).
                if visible[-1] in rebase_commands:
                    return ErrorResponse(error="invalid_request")
            else:
                parent = self._resolve_commit(f"{base}^")
                if parent is None:
                    return ErrorResponse(error="invalid_request")
                todo_hashes = [base] + todo_hashes
                base = parent
        return _RebaseInstructions(
            todo_lines=[f"{rebase_commands.get(h, 'pick')} {h}" for h in todo_hashes],
            base=base,
            extends_base=extends_base,
            hashes=hashes,
        )

    def _reword_instructions(self, commit_hash, message):
        visible = [c.commit_hash for c in self._list_commits()]
        if not (message and message.strip()) or commit_hash not in visible:
            return ErrorResponse(error="invalid_request")
        idx = visible.index(commit_hash)
        todo_hashes = list(reversed(visible[:idx + 1]))
        base = visible[idx + 1] if idx + 1 < len(visible) else self._start
        msg_path = self._write_tempfile(message + "\n")
        todo_lines = []
        for h in todo_hashes:
            todo_lines.append(f"pick {h}")
            if h == commit_hash:
                todo_lines.append(f"exec git commit --amend --allow-empty -F {shlex.quote(msg_path)}")
        return _RebaseInstructions(todo_lines=todo_lines, base=base, msg_path=msg_path)

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
        editor_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(_EDITOR_PY)}"
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
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        except:
            os.unlink(path)
            raise
        return path


# ------------------------------------------------------------------
# Flask REST API
# ------------------------------------------------------------------

def create_app(repo_path, token, log_path=None):
    _static = str(Path(__file__).parent / "static")
    app = Flask(__name__, static_folder=_static, static_url_path="/static")
    app.config["GH"] = GitHistory(repo_path, log_path=log_path or _LOG_PATH)
    app.config["TOKEN"] = token

    @app.before_request
    def auth():
        if request.path.startswith("/api/") or request.path == "/log":
            tok = request.headers.get("X-Token", "") or request.args.get("t", "")
            if not hmac.compare_digest(tok, app.config["TOKEN"]):
                abort(403)

    @app.route("/")
    def index():
        return send_from_directory(_static, "index.html")

    @app.route("/manual")
    def manual():
        return send_from_directory(_static, "manual.html")

    @app.route("/api/state")
    def api_state():
        return jsonify(asdict(app.config["GH"].read_state()))

    @app.route("/api/stash", methods=["POST"])
    def api_stash():
        return jsonify(asdict(app.config["GH"].stash()))

    @app.route("/api/stash/pop", methods=["POST"])
    def api_stash_pop():
        return jsonify(asdict(app.config["GH"].stash_pop()))

    @app.route("/api/rebase", methods=["POST"])
    def api_rebase():
        body = request.get_json(silent=True) or {}
        gh = app.config["GH"]
        op = body.get("operation")
        if op == "move":
            result = gh.move(body.get("order"))
        elif op == "squash":
            result = gh.squash(body.get("commit_hashes"))
        elif op == "fixup":
            result = gh.fixup(body.get("commit_hashes"))
        elif op == "reword":
            commit_hashes = body.get("commit_hashes") or []
            if not commit_hashes:
                result = ErrorResponse(error="invalid_request")
            else:
                result = gh.reword(commit_hashes[0], body.get("new_message"))
        else:
            result = ErrorResponse(error="invalid_request")
        return jsonify(asdict(result))

    @app.route("/api/rebase/continue", methods=["POST"])
    def api_rebase_continue():
        return jsonify(asdict(app.config["GH"].rebase_continue()))

    @app.route("/api/rebase/abort", methods=["POST"])
    def api_rebase_abort():
        return jsonify(asdict(app.config["GH"].rebase_abort()))

    @app.route("/api/reset", methods=["POST"])
    def api_reset():
        body = request.get_json(silent=True) or {}
        return jsonify(asdict(app.config["GH"].reset(body.get("commit_hash", ""))))

    @app.route("/api/submodule/update", methods=["POST"])
    def api_submodule_update():
        return jsonify(asdict(app.config["GH"].submodule_update()))

    @app.route("/api/switch", methods=["POST"])
    def api_switch():
        body = request.get_json(silent=True) or {}
        return jsonify(asdict(app.config["GH"].switch_branch(body.get("branch", ""))))

    @app.route("/api/show")
    def api_show():
        return jsonify(asdict(app.config["GH"].show(request.args.get("commit_hash", ""))))

    @app.route("/log")
    def log_view():
        content = app.config["GH"].read_log()
        return Response(content, mimetype="text/plain")

    @app.route("/api/quit", methods=["POST"])
    def api_quit():
        response = jsonify({"ok": True})
        # os._exit skips atexit; safe because all temp files are unlinked in their own finally blocks
        response.call_on_close(lambda: os._exit(0))
        return response

    return app


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Rewrite branch history with unlimited undo.",
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

    # Must be inside a git repo.
    r = subprocess.run(["git", "rev-parse", "--git-dir"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print("fatal: not a git repository", file=sys.stderr)
        sys.exit(1)

    # Reject detached HEAD.
    r = subprocess.run(["git", "symbolic-ref", "--quiet", "HEAD"],
                       capture_output=True, text=True, check=False)
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
    flask.cli.show_server_banner = lambda *_: None

    url = f"http://127.0.0.1:{port}/?t={token}"
    print(f"git-history running at {url}  —  Ctrl+C to quit")
    webbrowser.open(url)
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=False)


if __name__ == "__main__":
    main()
