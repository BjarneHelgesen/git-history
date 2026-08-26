from __future__ import annotations

import concurrent.futures
import datetime
from dataclasses import dataclass, field
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


class WarpStateError(Exception):
    """Raised when history state cannot be determined."""
    pass


class GitError(Exception):
    """Raised when a git command fails."""
    pass


class InvalidCommit(GitError):
    """Raised when a commit reference cannot be resolved."""
    pass


class GitWarpError(GitError):
    """Raised by GitWarp methods for validation or git operation failures."""
    def __init__(self, code, message=""):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}" if message else code)


_LOG_PATH = Path.home() / ".git-warp.log"


def clear_log():
    """Delete the log file if it exists. Returns its path and whether it was deleted."""
    existed = _LOG_PATH.exists()
    if existed:
        _LOG_PATH.unlink()
    return _LOG_PATH, existed

# Limit visible commits to roughly this many (first-parent depth) to avoid overwhelming
# the UI and slow performance on large repos. On merge-heavy repos, _start..HEAD can
# include additional commits reachable via merged branches, so this is not a hard cap.
_HISTORY_DEPTH = 1000

_MAX_DIFF_CHARS = 100_000

_EDITOR_PY = str(Path(__file__).parent / "editor.py")

_STAGED_HASH = "(Staged)"


def _unlink_safe(path):
    if path:
        try:
            os.unlink(path)
        except OSError as e:
            print(f"warning: failed to delete temp file {path}: {e}", file=sys.stderr)


def _write_tempfile(content):
    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except:
        _unlink_safe(path)
        raise
    return path


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
class UndoStackEntry:
    commit_hash: str
    label: str
    timestamp: str


@dataclass
class _StatusOutput:
    branch: str
    upstream: str
    has_staged: bool
    is_dirty: bool


@dataclass
class StateResponse:
    branch: str
    branches: list
    dirty: bool
    has_stash: bool
    rebase_in_progress: bool
    conflict_files: list
    commits: list
    undo_stack: list
    upstream: str = ""
    ok: bool = True
    submodule_update_suggested: bool = False
    conflict: bool = False
    reflog_expiry: str = ""
    diff: dict | None = None  # asdict(ShowResponse) for the selected commit, bundled by the REST layer for a single round-trip; None otherwise


@dataclass
class ErrorResponse:
    error: str
    message: str = ""
    ok: bool = False


@dataclass
class ShowCommit:
    # Only content-addressed fields; ref-dependent branches/tags are excluded so
    # the hash-keyed show cache stays immutable and never serves stale refs.
    commit_hash: str
    short_hash: str
    message: str
    author: str
    date: str


@dataclass
class ShowResponse:
    commit: ShowCommit
    diff: str
    files: list = field(default_factory=list)
    ok: bool = True


@dataclass
class _RebaseInstructions:
    todo_lines: list
    base: str | None # None means --root
    visible_commits: list # cached commit list to avoid re-fetching in _rebase()
    msg_path: str | None = None # temp file to unlink; reword only


class Git:
    """Low-level git plumbing for a single repository. Owns every git
    invocation and all subprocess details, exposing intention-revealing
    methods that return plain Python values. Most queries degrade to a safe
    default on failure; the few whose failure must reach the user return
    (value, error). Mutating methods return None on success or raise GitError
    on failure. Knows nothing of git-warp's domain — the commit
    window, the state model, or the operation log.

    Cache invariant: per-commit-hash caches use full 40-char hashes (immutable
    content addresses) so they do not need invalidation.
    """

    def __init__(self, repo_path):
        self.repo = Path(repo_path)
        self._resolve_cache = {}
        self._gitmodules_cache = {}
        self._gitlinks_cache = {}
        self._commit_touches_gitmodules_cache = {}
        r = self._run(["git", "rev-parse", "--git-dir"])
        if r.returncode != 0:
            raise WarpStateError("not a git repository")
        p = Path(r.stdout.strip())
        self._git_dir = p if p.is_absolute() else (self.repo / p).resolve()

    # ------------------------------------------------------------------
    # internals — the only place git is executed
    # ------------------------------------------------------------------

    def _run(self, args, env=None):
        return subprocess.run(
            args, cwd=str(self.repo), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        )

    def _mutate(self, *args, env=None):
        r = self._run(list(args), env=env)
        if r.returncode != 0:
            raise GitError(r.stderr.strip())

    def _rebase_env(self, todo_path=None, msg_path=None):
        env = os.environ.copy()
        editor_cmd = f"{shlex.quote(sys.executable)} {shlex.quote(_EDITOR_PY)}"
        # Both GIT_SEQUENCE_EDITOR (todo file) and GIT_EDITOR (message editing)
        # use the same custom script; the script differentiates via env vars.
        env["GIT_SEQUENCE_EDITOR"] = editor_cmd
        env["GIT_EDITOR"] = editor_cmd
        env["GIT_TERMINAL_PROMPT"] = "0"
        if todo_path:
            env["GIT_WARP_TODO"] = todo_path
        if msg_path:
            env["GIT_WARP_MSG"] = msg_path
        return env

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def config_value(self, key):
        r = self._run(["git", "config", key])
        return r.stdout.strip() if r.returncode == 0 else None

    def resolve_commit(self, ref):
        if ref is None:
            raise InvalidCommit("cannot resolve None")
        if ref in self._resolve_cache:
            return self._resolve_cache[ref]
        r = self._run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"])
        if r.returncode == 0:
            result = r.stdout.strip()
            if len(ref) == 40:  # only cache full hashes — symbolic refs (HEAD, ORIG_HEAD, base^, …) change across mutations
                self._resolve_cache[ref] = result
            return result
        raise InvalidCommit(f"cannot resolve commit: {ref}")

    def is_ancestor(self, ancestor, descendant):
        return self._run(["git", "merge-base", "--is-ancestor", ancestor, descendant]).returncode == 0

    def current_branch(self):
        r = self._run(["git", "symbolic-ref", "--short", "HEAD"])
        return r.stdout.strip() if r.returncode == 0 else ""

    def list_local_branches(self):
        r = self._run(["git", "--no-optional-locks", "branch", "--format=%(refname:short)"])
        if r.returncode != 0:
            return []
        return [b.strip() for b in r.stdout.splitlines() if b.strip()]

    def is_dirty(self):
        r = self._run(["git", "status", "--porcelain", "--untracked-files=no"])
        if r.returncode != 0:
            return True
        return bool(r.stdout.strip())

    def has_stash(self):
        r = self._run(["git", "--no-optional-locks", "stash", "list"])
        if r.returncode != 0:
            return False
        return bool(r.stdout.strip())

    def in_rebase(self):
        return (self._git_dir / "rebase-merge").exists() or (self._git_dir / "rebase-apply").exists()

    def conflict_files(self):
        r = self._run(["git", "--no-optional-locks", "diff", "--name-only", "--diff-filter=U"])
        if r.returncode != 0:
            return []
        return [ln for ln in r.stdout.splitlines() if ln]

    def status(self):
        """Read branch, upstream, staged, and dirty state from one git call.
        Raises GitError if git status fails.

        Parses `git status --porcelain --branch`. The first line is the branch
        header; the rest are file statuses where column 0 is the index (staged)
        status and column 1 the worktree status:

          ## main...origin/main [ahead 1]   branch with upstream, ahead/behind
          ## main                           branch without upstream
          ## HEAD (no branch)               detached HEAD (also normal mid-rebase)
          ## No commits yet on main         unborn branch
          M  file.txt                       staged modification, worktree clean
           M file.txt                       worktree modification, not staged
          MM file.txt                       modified in both index and worktree
        """
        r = self._run(["git", "status", "--porcelain", "--branch", "--untracked-files=no"])
        if r.returncode != 0:
            raise GitError(r.stderr.strip())

        branch = ''
        upstream = ''
        has_staged = False
        is_dirty = False

        for line in r.stdout.splitlines():
            if not line:
                continue
            if line.startswith('## '):
                branch_info = line[3:]
                if branch_info.startswith('HEAD (no branch)'):
                    # Detached HEAD (the normal state mid-rebase too): no branch checked out.
                    branch = ''
                elif branch_info.startswith('No commits yet on '):
                    branch = branch_info[len('No commits yet on '):]
                elif branch_info.startswith('Initial commit on '):
                    # Older git wording for an unborn branch.
                    branch = branch_info[len('Initial commit on '):]
                else:
                    # "branch" or "branch...upstream [ahead/behind]"
                    parts = branch_info.split('...', 1)
                    branch = parts[0]
                    if len(parts) > 1:
                        upstream = parts[1].split()[0]
            else:
                is_dirty = True
                if line[0] != ' ':  # any index status means staged changes exist, regardless of type
                    has_staged = True

        return _StatusOutput(branch=branch, upstream=upstream, has_staged=has_staged, is_dirty=is_dirty)

    def log_commits(self, rev_range):
        """Return [(hash, short_hash, author, date, body, refs), ...] newest-first
        for the given revision range, or [] on failure."""
        fmt = "%H%x1f%h%x1f%an%x1f%ai%x1f%B%x1f%D%x00"
        r = self._run(["git", "--no-optional-locks", "log", "--abbrev=7", f"--format={fmt}", rev_range])
        if r.returncode != 0:
            return []
        records = []
        for record in r.stdout.split("\x00"):
            record = record.lstrip("\n")
            if not record:
                continue
            parts = record.split("\x1f")
            if len(parts) >= 6:
                records.append(tuple(parts[:6]))
        return records

    def commit_record(self, commit_hash):
        """Return (record, None) where record is (hash, short_hash, author, date,
        body), or (None, stderr) on failure."""
        fmt = "%H%x00%h%x00%an%x00%ai%x00%B"
        r = self._run(["git", "log", "--abbrev=7", f"--format={fmt}", "-1", commit_hash])
        if r.returncode != 0:
            return None, r.stderr.strip()
        parts = r.stdout.split("\x00")
        return (tuple(parts[:5]), None) if len(parts) >= 5 else (None, "")

    def commit_diff(self, commit_hash):
        r = self._run(["git", "show", "--format=", commit_hash])
        return (r.stdout, None) if r.returncode == 0 else (None, r.stderr.strip())

    def staged_diff(self):
        r = self._run(["git", "diff", "--cached"])
        return (r.stdout, None) if r.returncode == 0 else (None, r.stderr.strip())

    def rev_list(self, rev_range):
        """Full hashes reachable in the range (newest first), or [] on failure."""
        r = self._run(["git", "--no-optional-locks", "log", "--format=%H", rev_range])
        if r.returncode != 0:
            return []
        return r.stdout.splitlines()

    def reflog(self, ref):
        """Return [(hash, subject, iso_date), ...] for the ref's reflog
        (newest first), or [] on failure."""
        r = self._run(["git", "--no-optional-locks", "reflog", ref, "--format=%H%x1f%gs%x1f%ci"])
        if r.returncode != 0:
            return []
        entries = []
        for line in r.stdout.splitlines():
            if not line:
                continue
            parts = line.split("\x1f")
            if len(parts) >= 3:
                entries.append(tuple(parts[:3]))
        return entries

    def subjects(self, commit_hashes):
        """Return {hash: subject} for the given hashes (missing ones omitted)."""
        result = {}
        fmt = "%H%x1f%s%x00"
        for i in range(0, len(commit_hashes), 500):  # chunk to stay within command-line length limits
            chunk = commit_hashes[i:i + 500]
            r = self._run(["git", "log", f"--format={fmt}", "--no-walk", "--ignore-missing"] + chunk)
            if r.returncode != 0:
                continue
            for record in r.stdout.split("\x00"):
                record = record.strip()
                if not record:
                    continue
                parts = record.split("\x1f", 1)
                if len(parts) == 2:
                    result[parts[0]] = parts[1].rstrip()
        return result

    def get_gitmodules(self, commit_hash):
        if commit_hash in self._gitmodules_cache:
            return self._gitmodules_cache[commit_hash]
        r = self._run(["git", "show", f"{commit_hash}:.gitmodules"])
        result = r.stdout if r.returncode == 0 else ""
        self._gitmodules_cache[commit_hash] = result
        return result

    def gitlinks_at(self, commit_hash):
        if commit_hash in self._gitlinks_cache:
            return self._gitlinks_cache[commit_hash]
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
        self._gitlinks_cache[commit_hash] = result
        return result

    def commit_touches_gitmodules(self, commit_hash):
        if commit_hash in self._commit_touches_gitmodules_cache:
            return self._commit_touches_gitmodules_cache[commit_hash]
        r = self._run(["git", "diff-tree", "--root", "--no-commit-id", "-r", "--name-only", commit_hash])
        result = ".gitmodules" in r.stdout.splitlines()
        self._commit_touches_gitmodules_cache[commit_hash] = result
        return result

    def is_merge_commit(self, commit_hash):
        r = self._run(["git", "rev-list", "--parents", "-n", "1", commit_hash])
        if r.returncode == 0:
            parents = r.stdout.strip().split()
            return len(parents) > 2
        return False

    def commit_files(self, commit_hash):
        r = self._run(["git", "-c", "core.quotePath=false", "diff-tree", "--no-commit-id", "-r", "--name-only", commit_hash])
        return r.stdout.splitlines() if r.returncode == 0 else []

    # ------------------------------------------------------------------
    # mutations — return None on success, raise GitError on failure
    # ------------------------------------------------------------------

    def create_branch(self, branch_name, commit_hash):
        # "--" stops a branch name beginning with "-" being parsed as an option.
        self._mutate("git", "branch", "--", branch_name, commit_hash)

    def delete_branch(self, branch_name):
        self._mutate("git", "branch", "-d", "--", branch_name)

    def stash_push(self):
        self._mutate("git", "stash", "push")

    def stash_pop(self):
        self._mutate("git", "stash", "pop")

    def reset_hard(self, commit_hash):
        self._mutate("git", "reset", "--hard", commit_hash)

    def switch(self, branch):
        self._mutate("git", "switch", branch)

    def submodule_update_init(self):
        self._mutate("git", "submodule", "update", "--init")

    def rebase_interactive(self, todo_lines, base, msg_path=None):
        """Run `git rebase -i` against a pre-written todo (base=None means
        --root). Raises GitError on failure. The caller checks in_rebase() to
        tell a clean finish from a paused/conflicted rebase."""
        todo_path = None
        try:
            todo_path = _write_tempfile("\n".join(todo_lines) + "\n")
            env = self._rebase_env(todo_path=todo_path, msg_path=msg_path)
            # --empty=keep: a commit that replays to nothing is kept, not dropped,
            # so an operation never silently removes a commit. See SPEC.md.
            cmd = ["git", "rebase", "-i", "--empty=keep"]
            cmd.append("--root" if base is None else base)
            r = self._run(cmd, env=env)
            if r.returncode != 0 and not self.in_rebase():
                raise GitError(r.stderr.strip())
        finally:
            # Only the todo file is ours; msg_path (reword) is owned by the caller.
            _unlink_safe(todo_path)

    def rebase_continue(self):
        self._mutate("git", "rebase", "--continue", env=self._rebase_env())

    def rebase_abort(self):
        self._mutate("git", "rebase", "--abort")


class GitWarp:
    """Wraps git operations for a single repository.

    All public methods return a typed dataclass (StateResponse, ShowResponse);
    known failures raise GitWarpError. See SPEC.md for the public API
    contract and REST endpoints.
    """

    def __init__(self, repo_path, log_path=_LOG_PATH):
        self._git = Git(repo_path)
        self._log_path = Path(log_path)
        # Caches keyed by full 40-char hash (immutable), so they never need invalidation.
        # _commit_cache is unbounded, but most diffs are far smaller than _MAX_DIFF_CHARS,
        # so even a 1000-commit session stays in the single-digit MB range; no cap needed.
        self._commit_cache = {}
        self._subject_cache = {}
        if not self._git.current_branch():
            raise WarpStateError("HEAD is detached; checkout a branch first")
        # Load reflog expiry once at startup, not per read_state() call
        raw_expiry = self._git.config_value("gc.reflogExpireUnreachable")
        self._reflog_expiry = raw_expiry.replace('.', ' ') if raw_expiry else "30 days"
        self._start = self._resolve_window_start()

    # ------------------------------------------------------------------
    # domain helpers
    # ------------------------------------------------------------------

    def _resolve_window_start(self):
        # None when history is shallower than the window depth: show it all.
        try:
            return self._git.resolve_commit(f"HEAD~{_HISTORY_DEPTH}")
        except GitError:
            return None

    @staticmethod
    def _truncate_diff(diff):
        return diff[:_MAX_DIFF_CHARS] + "\n..." if len(diff) > _MAX_DIFF_CHARS else diff

    def _require_branch(self):
        # Detached HEAD is unsupported for mutations: a reset/rebase there would move
        # HEAD without updating any branch, leaving the repo in an unspecified state.
        if not self._git.current_branch():
            raise GitWarpError("detached_head")

    def _append_log(self):
        branch = self._git.current_branch()
        if branch:
            head = self._git.resolve_commit("HEAD")
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(f"{ts} {branch} {head}\n")
            except OSError as e:
                print(f"warning: failed to append to log at {self._log_path}: {e}", file=sys.stderr)

    def _any_moved_commit_touches_gitmodules(self, current_order, new_order):
        moved = [h for h, h2 in zip(current_order, new_order) if h != h2]
        return any(self._git.commit_touches_gitmodules(h) for h in moved)

    def _conflict_response(self):
        state = self.read_state()
        state.ok = False
        state.conflict = True
        return state

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    def read_state(self, submodule_update_suggested=False):
        if self._start is not None and not self._git.is_ancestor(self._start, "HEAD"):
            raise GitWarpError(
                "history_changed",
                "Commit out of range; please restart git warp.")

        with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
            future_commits = executor.submit(self._list_commits)
            future_branches = executor.submit(self._git.list_local_branches)
            future_stash = executor.submit(self._git.has_stash)

            status = self._git.status()

            future_unpushed = executor.submit(self._get_unpushed_hashes, status.branch, status.upstream)
            future_branch_reflog = executor.submit(self._get_branch_reflog, status.branch)
            future_rebase_labels = executor.submit(self._build_head_rebase_labels)

            # Cheap filesystem check; run it on the main thread while the pool works.
            # Conflict files are only reported mid-rebase, so skip that git call otherwise.
            in_rebase = self._git.in_rebase()
            future_conflict = executor.submit(self._git.conflict_files) if in_rebase else None

            commits = future_commits.result()
            branches = future_branches.result()
            has_stash = future_stash.result()
            conflict_files = future_conflict.result() if future_conflict else []
            unpushed = future_unpushed.result()
            raw_undo_stack = future_branch_reflog.result()
            rebase_labels = future_rebase_labels.result()

        # Populate the subject cache after the pool has joined, so no cache
        # write races the parallel git calls. _process_undo_stack reuses these.
        for c in commits:
            self._subject_cache[c.commit_hash] = c.message.split("\n", 1)[0].rstrip()

        undo_stack = self._process_undo_stack(raw_undo_stack, rebase_labels)

        for c in commits:
            c.pushed = bool(status.upstream) and c.commit_hash not in unpushed
        if status.has_staged:
            staged = Commit(commit_hash=_STAGED_HASH, short_hash=_STAGED_HASH, message="(Staged changes)",
                           author="", date="", branches=[], tags=[])
            commits.insert(0, staged)

        return StateResponse(
            branch=status.branch,
            upstream=status.upstream,
            branches=branches,
            dirty=status.is_dirty,
            has_stash=has_stash,
            rebase_in_progress=in_rebase,
            conflict_files=conflict_files,
            commits=commits,
            undo_stack=undo_stack,
            submodule_update_suggested=submodule_update_suggested,
            reflog_expiry=self._reflog_expiry,
            conflict=bool(conflict_files),
        )

    def get_history_state(self):
        """Get undo stack and current HEAD index for undo/redo operations.

        Returns (history, head_index) tuple on success, raises WarpStateError on failure.
        """
        if self._git.in_rebase():
            raise WarpStateError("rebase in progress")
        branch = self._git.current_branch()
        history = self._list_undo_stack(branch, self._build_head_rebase_labels())
        if not history:
            raise WarpStateError("no history available")
        head = self._git.resolve_commit("HEAD")
        hashes = [e.commit_hash for e in history]
        try:
            idx = hashes.index(head)
        except ValueError:
            raise WarpStateError("HEAD not in undo stack")
        return history, idx

    def _get_unpushed_hashes(self, branch, upstream):
        if not branch or not upstream:
            return set()
        return set(self._git.rev_list(f"{upstream}..HEAD"))

    def _list_commits(self):
        rev_range = f"{self._start}..HEAD" if self._start else "HEAD"
        commits = []
        for h, sh, author, date, body, refs in self._git.log_commits(rev_range):
            branches, tags = self._parse_refs(refs)
            message = body.rstrip("\n")
            commits.append(Commit(
                commit_hash=h,
                short_hash=sh[:7],
                message=message,
                author=author,
                date=date,
                branches=branches,
                tags=tags,
            ))
        if commits:
            commits[0].is_head = True
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
    def _describe_rebase_group(labels):
        """Return a label from HEAD reflog subject strings between rebase (finish) and rebase (start)."""
        def msgs(*prefixes):
            return [lbl[len(p):] for lbl in labels for p in prefixes if lbl.startswith(p)]
        fixup_msgs = msgs("rebase (fixup): ")
        squash_msgs = msgs("rebase (squash): ")
        reword_msgs = msgs("rebase (reword): ", "commit (amend): ")
        op_count = sum(bool(m) for m in [fixup_msgs, squash_msgs, reword_msgs])
        if op_count > 1:
            return "rebase"
        if fixup_msgs:
            return "fixup: " + "; ".join(dict.fromkeys(fixup_msgs))
        if squash_msgs:
            # Intermediate squash commits carry git's "# This is a combination…"
            # template as their subject; only the newest (final) entry holds the
            # finalized destination subject, which names the fold once.
            return "squash: " + squash_msgs[0]
        if reword_msgs:
            return "reword: " + reword_msgs[0]
        pick_count = sum(1 for lbl in labels if lbl.startswith("rebase (pick)"))
        if pick_count:
            return f"reorder: HEAD~{pick_count}"
        return "rebase"

    def _build_head_rebase_labels(self):
        """Read HEAD reflog and return {finish_hash: label} for each completed rebase."""
        labels = {}
        finish_hash = None
        group = []
        for h, gs, _ in self._git.reflog("HEAD"):
            if gs.startswith("rebase (finish)"):
                finish_hash = h
                group = []
            elif gs.startswith("rebase (start)"):
                if finish_hash:
                    labels[finish_hash] = GitWarp._describe_rebase_group(group)
                    finish_hash = None
                    group = []
            elif finish_hash is not None:
                group.append(gs)
        return labels

    @staticmethod
    def _filter_rebase_groups(entries, rebase_labels=None):
        """Keep only rebase (finish) from rebase groups; filter all other rebase entries."""
        result = []
        for e in entries:
            if e.label.startswith("rebase (finish)"):
                label = (rebase_labels or {}).get(e.commit_hash, "rebase")
                result.append(UndoStackEntry(commit_hash=e.commit_hash, label=label, timestamp=e.timestamp))
            elif not e.label.startswith("rebase"):
                result.append(e)
        return result

    def _enhance_reset_labels(self, entries):
        """Look up commit message for entries starting with 'reset' and label them 'reset: <subject>'."""
        result = []
        for e in entries:
            if e.label.startswith("reset"):
                subject = self._subject_cache.get(e.commit_hash)
                if subject:
                    e = UndoStackEntry(commit_hash=e.commit_hash, label=f"reset: {subject}", timestamp=e.timestamp)
            result.append(e)
        return result

    def _get_branch_reflog(self, branch):
        if not branch:
            return []
        return [UndoStackEntry(commit_hash=h, label=label, timestamp=ts)
                for h, label, ts in self._git.reflog(f"refs/heads/{branch}")]

    def _process_undo_stack(self, raw_entries, rebase_labels):
        if not raw_entries:
            return []
        # Deduplicate by hash keeping the oldest entry (last in newest-first list).
        # This ensures reset/rebase intermediates are discarded in favour of the
        # original commit label, keeping the displayed list stable after undo/redo.
        filtered = self._filter_rebase_groups(raw_entries, rebase_labels)
        seen = set()
        oldest_first = []
        for e in reversed(filtered):
            if e.commit_hash not in seen:
                seen.add(e.commit_hash)
                oldest_first.append(e)
        result = list(reversed(oldest_first))
        # Cache messages for all undo stack commits to avoid individual lookups
        uncached = [e.commit_hash for e in result if e.commit_hash not in self._subject_cache]
        if uncached:
            self._subject_cache.update(self._git.subjects(uncached))
        return self._enhance_reset_labels(result)

    def _list_undo_stack(self, branch, rebase_labels):
        raw = self._get_branch_reflog(branch)
        return self._process_undo_stack(raw, rebase_labels)

    # ------------------------------------------------------------------
    # stash
    # ------------------------------------------------------------------

    def stash(self):
        if self._git.in_rebase():
            raise GitWarpError("rebase_in_progress")
        self._require_branch()
        if not self._git.is_dirty():
            raise GitWarpError("nothing_to_stash")
        self._git.stash_push()
        return self.read_state()

    def stash_pop(self):
        if self._git.in_rebase():
            raise GitWarpError("rebase_in_progress")
        self._require_branch()
        if not self._git.has_stash():
            raise GitWarpError("no_stash")
        if self._git.is_dirty():
            raise GitWarpError("dirty_tree")
        try:
            self._git.stash_pop()
        except GitError:
            if self._git.conflict_files():
                raise GitWarpError("stash_conflict")
            raise
        return self.read_state()

    # ------------------------------------------------------------------
    # reset
    # ------------------------------------------------------------------

    def reset(self, commit_hash):
        if self._git.in_rebase():
            raise GitWarpError("rebase_in_progress")
        self._require_branch()
        resolved_hash = self._git.resolve_commit(commit_hash)
        head_hash = self._git.resolve_commit("HEAD")
        if self._git.is_dirty():
            raise GitWarpError("dirty_tree")
        if head_hash and self._git.get_gitmodules(head_hash) != self._git.get_gitmodules(resolved_hash):
            raise GitWarpError("gitmodules_differ")
        self._git.reset_hard(resolved_hash)
        self._append_log()
        return self.read_state(submodule_update_suggested=self._gitlinks_changed(head_hash))

    def create_branch(self, branch_name, commit_hash):
        if self._git.in_rebase():
            raise GitWarpError("rebase_in_progress")
        resolved_hash = self._git.resolve_commit(commit_hash)
        self._git.create_branch(branch_name, resolved_hash)
        return self.read_state()

    def delete_branch(self, branch_name):
        if self._git.in_rebase():
            raise GitWarpError("rebase_in_progress")
        if branch_name == self._git.current_branch():
            raise GitWarpError("cannot_delete_current_branch")
        self._git.delete_branch(branch_name)
        return self.read_state()

    def submodule_update(self):
        if self._git.in_rebase():
            raise GitWarpError("rebase_in_progress")
        self._require_branch()
        self._git.submodule_update_init()
        return self.read_state()

    def switch_branch(self, branch, allow_different_gitmodules=False):
        if self._git.in_rebase():
            raise GitWarpError("rebase_in_progress")
        if self._git.is_dirty():
            raise GitWarpError("dirty_tree")
        if branch not in self._git.list_local_branches():
            raise GitWarpError("invalid_branch")
        head_hash = self._git.resolve_commit("HEAD")
        if not allow_different_gitmodules and head_hash and self._git.get_gitmodules(head_hash) != self._git.get_gitmodules(self._git.resolve_commit(branch)):
            raise GitWarpError("gitmodules_differ")
        self._git.switch(branch)
        self._start = self._resolve_window_start()
        return self.read_state(submodule_update_suggested=self._gitlinks_changed(head_hash))

    # ------------------------------------------------------------------
    # show
    # ------------------------------------------------------------------

    def show(self, commit_hash):
        if commit_hash == _STAGED_HASH:
            diff, err = self._git.staged_diff()
            if diff is None:
                raise GitWarpError("git_failed", err)
            return ShowResponse(commit=ShowCommit(commit_hash=_STAGED_HASH, short_hash=_STAGED_HASH,
                                                  message="", author="", date=""),
                               diff=self._truncate_diff(diff))
        resolved = self._git.resolve_commit(commit_hash)
        if resolved in self._commit_cache:
            return self._commit_cache[resolved]
        rec, err = self._git.commit_record(resolved)
        if rec is None:
            raise GitWarpError("git_failed", err)
        h, sh, author, date, body = rec
        diff, err = self._git.commit_diff(resolved)
        if diff is None:
            raise GitWarpError("git_failed", err)
        result = ShowResponse(commit=ShowCommit(commit_hash=h, short_hash=sh[:7], message=body.rstrip("\n"),
                                               author=author, date=date),
                             diff=self._truncate_diff(diff), files=self._git.commit_files(resolved))
        self._commit_cache[resolved] = result
        return result

    # ------------------------------------------------------------------
    # log
    # ------------------------------------------------------------------

    def read_log(self) -> str:
        try:
            return self._log_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    # ------------------------------------------------------------------
    # rebase
    #
    # All four mutating operations (move, squash, fixup, reword) build a
    # ``_RebaseInstructions`` and hand it to ``_rebase``, which writes the
    # todo file, runs ``git rebase -i --empty=keep``, and
    # cleans up. Commits are processed in the order git wants in the todo:
    # oldest first, even though the rest of this module is newest-first.
    #
    #   move: the UI sends the desired ordering of visible commits,
    #     newest-first. After reversing to oldest-first, the longest shared
    #     prefix with the current order is identified; everything before
    #     that prefix is untouched. Generate ``pick`` lines for the rebase
    #     range. Refuses with ``gitmodules_in_range`` if any moved commit
    #     touches ``.gitmodules``.
    #
    #   squash: the selected hashes must form a contiguous block in the
    #     commit list. Each selected commit is marked ``squash`` in the
    #     todo so it folds into the commit directly before it; non-
    #     selected commits keep their ``pick``. Git's default squash
    #     concatenates messages automatically.
    #
    #   fixup: a single selected hash H. If H is the root commit, refuse
    #     with ``invalid_request``. Otherwise the todo includes H's parent
    #     as ``pick`` and H as ``fixup``. The rebase base is extended if
    #     needed so H's parent is in the todo.
    #
    #   reword: a single selected hash H. The new message is written to a
    #     temp file. The todo lists H as ``pick`` followed by
    #     ``exec git commit --amend --allow-empty -F <msg_path>``;
    #     everything else is ``pick``. This avoids relying on
    #     ``GIT_EDITOR`` for the message — the exec line applies it
    #     directly. The exec step always runs within ``_run`` (a pick
    #     replaying onto its own parent cannot conflict), so it is safe to
    #     unlink the message temp file in the ``finally`` block of
    #     ``_rebase``.
    #
    #   split: a single hash H and a strict, non-empty subset of the files it
    #     changes. The todo lists H as ``pick`` followed by one ``exec`` that
    #     resets H out of the way (mixed reset to its parent, leaving all of
    #     H's changes in the worktree) and rebuilds it as two commits: the
    #     unselected files, then the selected files. Both reuse H's author and
    #     message via ``git commit -C H`` (``--no-verify``, since rebase picks
    #     run no hooks). The two commits' combined tree equals H's, so the rest
    #     of the todo replays unchanged and cannot conflict; if the exec ever
    #     fails, ``split`` aborts the rebase and reports ``split_failed`` rather
    #     than leaving a half-finished rebase. Merge commits, the root commit,
    #     and commits touching ``.gitmodules`` are refused.
    # ------------------------------------------------------------------

    def move(self, desired_commit_order):
        visible_commits = self._list_commits()
        instr = self._move_instructions(desired_commit_order, visible_commits)
        if not instr.todo_lines:
            return self.read_state()
        return self._rebase(instr)

    def squash(self, hashes):
        visible_commits = self._list_commits()
        instr = self._squash_instructions(hashes, visible_commits)
        return self._rebase(instr)

    def fixup(self, hashes):
        visible_commits = self._list_commits()
        instr = self._fixup_instructions(hashes, visible_commits)
        return self._rebase(instr)

    def reword(self, commit_hash, message):
        visible_commits = self._list_commits()
        instr = self._reword_instructions(commit_hash, message, visible_commits)
        return self._rebase(instr)

    def split(self, commit_hash, files_to_split):
        visible_commits = self._list_commits()
        instr = self._split_instructions(commit_hash, files_to_split, visible_commits)
        # A valid split rebuilds H's exact tree, so replayed children never
        # conflict; any leftover rebase means the exec failed. Abort it so the
        # repo returns to its original state instead of a half-finished rebase.
        try:
            state = self._rebase(instr)
        except GitWarpError:
            raise
        except GitError:
            if self._git.in_rebase():
                self._git.rebase_abort()
            raise GitWarpError("split_failed")
        if state.rebase_in_progress:
            self._git.rebase_abort()
            raise GitWarpError("split_failed")
        return state

    def _rebase(self, instr):
        try:
            if self._git.is_dirty():
                raise GitWarpError("dirty_tree")
            if self._git.in_rebase():
                raise GitWarpError("invalid_request")
            self._require_branch()

            visible = [c.commit_hash for c in instr.visible_commits]
            if not visible:
                raise GitWarpError("invalid_request")

            self._git.rebase_interactive(instr.todo_lines, instr.base, instr.msg_path)

            err = self._check_rebase_completed()
            if err is not None:
                return err

            self._append_log()
            state = self.read_state(submodule_update_suggested=self._gitlinks_changed(visible[0]))
            return state
        finally:
            _unlink_safe(instr.msg_path)

    def _check_rebase_completed(self):
        # A squash or fixup whose combined diff is empty pauses the rebase — git
        # refuses to amend a commit into emptiness, and --empty=keep does not
        # cover that path. _drive_continue runs --continue, which commits it
        # empty. See SPEC.md "Empty Commits".
        if self._git.in_rebase():
            return self._drive_continue()
        return None

    def _gitlinks_changed(self, before_hash):
        if not before_hash:
            return False
        return self._git.gitlinks_at(before_hash) != self._git.gitlinks_at(self._git.resolve_commit("HEAD"))

    def rebase_continue(self):
        if not self._git.in_rebase():
            raise GitWarpError("not_in_rebase")
        err = self._drive_continue()
        if err is not None:
            return err
        self._append_log()
        orig_head = self._git.resolve_commit("ORIG_HEAD")
        return self.read_state(submodule_update_suggested=self._gitlinks_changed(orig_head))

    def rebase_abort(self):
        if not self._git.in_rebase():
            raise GitWarpError("not_in_rebase")
        self._git.rebase_abort()
        return self.read_state()

    # ------------------------------------------------------------------
    # rebase helpers
    # ------------------------------------------------------------------

    def _move_instructions(self, desired_commit_order: list[str], visible_commits):
        visible = [c.commit_hash for c in visible_commits]
        if desired_commit_order is None or sorted(desired_commit_order) != sorted(visible):
            raise GitWarpError("invalid_request")
        if self._any_moved_commit_touches_gitmodules(visible, desired_commit_order):
            raise GitWarpError("gitmodules_in_range")
        changed_idx = [i for i, (h, h2) in enumerate(zip(visible, desired_commit_order)) if h != h2]
        if not changed_idx:
            return _RebaseInstructions(todo_lines=[], base=None, visible_commits=visible_commits)
        oldest_changed = max(changed_idx)
        range_set = set(visible[:oldest_changed + 1])
        todo_hashes = self._to_interactive_rebase_order([h for h in desired_commit_order if h in range_set])
        base = visible[oldest_changed + 1] if oldest_changed + 1 < len(visible) else self._start
        return _RebaseInstructions(todo_lines=[f"pick {h}" for h in todo_hashes], base=base, visible_commits=visible_commits)

    def _squash_instructions(self, hashes, visible_commits):
        return self._fold_instructions(hashes, "squash", visible_commits)

    def _fixup_instructions(self, hashes, visible_commits):
        return self._fold_instructions(hashes, "fixup", visible_commits)

    def _fold_instructions(self, hashes, operation, visible_commits):
        visible = [c.commit_hash for c in visible_commits]
        if not hashes or any(h not in visible for h in hashes):
            raise GitWarpError("invalid_request")
        indices = sorted(visible.index(h) for h in hashes)
        if indices != list(range(indices[0], indices[-1] + 1)):
            raise GitWarpError("invalid_request")
        if len(hashes) == 1:
            rebase_commands = {hashes[0]: operation}
        else:
            # The oldest in the group stays as pick; the rest fold into it.
            oldest_in_group = max(hashes, key=visible.index)
            rebase_commands = {h: operation for h in hashes if h != oldest_in_group}
        oldest_idx = indices[-1]
        todo_hashes = self._to_interactive_rebase_order(visible[:oldest_idx + 2])
        base = visible[oldest_idx + 2] if oldest_idx + 2 < len(visible) else self._start
        # The oldest visible commit's only parent is _start (the frozen window anchor)
        # or --root; folding it INTO that parent would rewrite the anchor, so refuse.
        # Multi-select where the oldest stays the pick target is fine — it is not in
        # rebase_commands and the newer commits fold into it.
        if visible[-1] in rebase_commands:
            raise GitWarpError("invalid_request")
        return _RebaseInstructions(
            todo_lines=[f"{rebase_commands.get(h, 'pick')} {h}" for h in todo_hashes],
            base=base,
            visible_commits=visible_commits,
        )

    def _pick_with_exec(self, visible, commit_hash, exec_line):
        idx = visible.index(commit_hash)
        todo_hashes = self._to_interactive_rebase_order(visible[:idx + 1])
        base = visible[idx + 1] if idx + 1 < len(visible) else self._start
        todo_lines = []
        for h in todo_hashes:
            todo_lines.append(f"pick {h}")
            if h == commit_hash:
                todo_lines.append(exec_line)
        return todo_lines, base

    def _reword_instructions(self, commit_hash, message: str, visible_commits):
        visible = [c.commit_hash for c in visible_commits]
        if not (message and message.strip()) or commit_hash not in visible:
            raise GitWarpError("invalid_request")
        msg_path = _write_tempfile(message + "\n")
        exec_line = f"exec git commit --amend --allow-empty -F {shlex.quote(msg_path)}"
        todo_lines, base = self._pick_with_exec(visible, commit_hash, exec_line)
        return _RebaseInstructions(todo_lines=todo_lines, base=base, msg_path=msg_path, visible_commits=visible_commits)

    def _split_instructions(self, commit_hash, files_to_split, visible_commits):
        visible = [c.commit_hash for c in visible_commits]
        if commit_hash not in visible:
            raise GitWarpError("invalid_request")
        try:
            self._git.resolve_commit(commit_hash + "^")  # root commit has no parent to reset onto
        except GitError:
            raise GitWarpError("invalid_request")
        if self._git.is_merge_commit(commit_hash) or self._git.commit_touches_gitmodules(commit_hash):
            raise GitWarpError("invalid_request")
        all_files = self._git.commit_files(commit_hash)
        selected = set(files_to_split)
        if not selected or not selected.issubset(all_files) or selected == set(all_files):
            raise GitWarpError("invalid_request")
        kept = [f for f in all_files if f not in selected]
        split = [f for f in all_files if f in selected]
        # Mixed reset leaves H's whole diff unstaged in the worktree; stage and
        # commit the kept files, then the split files, reusing H's author+message.
        # --no-verify: rebase picks run no commit hooks, so neither does a split.
        add = lambda fs: "git add -A -- " + " ".join(shlex.quote(f) for f in fs)
        commit = f"git commit --quiet --no-verify -C {commit_hash}"
        exec_line = (f"exec git reset --quiet HEAD^ && {add(kept)} && {commit}"
                     f" && {add(split)} && {commit}")
        todo_lines, base = self._pick_with_exec(visible, commit_hash, exec_line)
        return _RebaseInstructions(todo_lines=todo_lines, base=base, visible_commits=visible_commits)

    def _advance_rebase(self):
        # git rebase --continue exits with code 1 (raising GitError) in two distinct
        # situations: a genuine git failure, and stopping because the next commit
        # conflicts. Both look identical at the call site. Check conflict_files()
        # after the exception to tell them apart.
        try:
            self._git.rebase_continue()
            return False  # step completed without a new conflict
        except GitError:
            if self._git.conflict_files():
                return True  # --continue stopped because the next commit conflicts
            raise            # genuine git failure

    def _drive_continue(self):
        # A rebase may pause multiple times: once per conflicting commit, and once
        # per squash or fixup that comes out empty. Loop until the rebase
        # finishes (in_rebase() returns False) or a conflict requires user action.
        while self._git.in_rebase():
            if self._git.conflict_files():
                # Conflict is waiting for the user to resolve — surface it.
                return self._conflict_response()
            if self._advance_rebase():
                # --continue advanced to the next commit, which also conflicts.
                return self._conflict_response()
        return None

    @staticmethod
    def _to_interactive_rebase_order(commit_instructions_newest_first):
        # git rebase -i requires oldest-first; the rest of this module is newest-first
        return list(reversed(commit_instructions_newest_first))
