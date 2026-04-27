# git-history: Implementation Plan

## Overview

A command-line tool for Windows, run from inside any git repository. It starts a local HTTP server bound to `127.0.0.1`, opens a browser, and presents a single-page UI for rewriting git history. All git operations run server-side via `subprocess`; the browser is pure UI.

Design goal: simple, reliable code. The smallest interactive-rebase replacement that works, with no avoidable failure modes.

Dependencies: Flask, stdlib. No build step, no JS bundler.

## Project Structure

```
git-history/
├── git_history/
│   ├── __init__.py      # GitHistory class + Flask app factory + CLI entry point
│   ├── __main__.py      # python -m git_history entry point
│   └── static/
│       ├── index.html
│       ├── app.js
│       ├── style.css
│       └── manual.html
└── editor.py            # 6-line helper for GIT_SEQUENCE_EDITOR / GIT_EDITOR
```

Everything lives in `git_history/__init__.py`: the `GitHistory` class, the Flask app factory, and the CLI entry point.

## CLI

```
git-history [<start>] [--port <N>]
```

| Argument | Default   | Meaning |
|----------|-----------|---------|
| `start` | `HEAD~200` | Any git revision. Commits in `start..HEAD` are shown & editable. If `HEAD~200` fails (shallow history), falls back to the repo root. |
| `--port` | auto | TCP port on `127.0.0.1`. If omitted, picks a free port. |
| `--clear-log` | — | Delete `~/.git-history.log` and exit. |

## Startup

1. `git --version` — must be >= 2.26; otherwise exit with error.
2. `git rev-parse --git-dir` — must succeed; otherwise exit with error.
3. `git symbolic-ref --quiet HEAD` — must succeed; detached HEAD is rejected.
4. Resolve `start` to a full commit hash. If `HEAD~200` fails, use the repo root.
5. Pick a port: bind a socket to `('127.0.0.1', 0)`, read the port, close, reuse. If `--port` is given, use it.
6. Generate a 32-char token: `secrets.token_urlsafe(24)`.
7. Start Flask app bound to `127.0.0.1:<port>`.
8. Print: `git-history running at http://127.0.0.1:<port>/?t=<token>  —  Ctrl+C to quit`.
9. `webbrowser.open(url)`.

## Shutdown

Two triggers:

- Ctrl+C in the terminal (`KeyboardInterrupt` from `app.run()`).
- Quit button in the browser (`POST /api/quit`).

On Ctrl+C: `app.run()` exits normally. On quit button: `os._exit(0)` is called after the response is sent.

The server never touches the repo on shutdown. If a rebase is mid-conflict, state is preserved; restarting git-history picks it up from `.git/rebase-merge`.

## Security

- Server binds only to `127.0.0.1`.
- A 32-char random token is generated at startup and embedded in the launch URL as `?t=<token>`.
- `index.html` and `static/*` are served without token checks (harmless on localhost).
- `app.js`, on load: reads `t` from `window.location.search`, stores it in `localStorage` under `git_history_token`, then calls `history.replaceState(null, '', '/')` to strip the query string from history.
- Every `/api/*` request sends the token in the `X-Token` header.
- The server compares using `hmac.compare_digest`. Mismatch/missing → HTTP 403 with empty body.
- This defends against other local users reading `/api/*` and against DNS-rebinding attacks.

## Concurrency

Flask's development server (used here) is single-threaded. Requests are processed in arrival order; a long-running rebase blocks other requests until it finishes. No locks needed because there is no concurrency. The UI shows a spinner while any request is in flight and disables mutating controls.

This is exactly the desired behavior: no git operation should run concurrently with another.

## Working Tree Policy

Before every mutating operation the server runs:

```
git status --porcelain --untracked-files=no
```

If the output is non-empty, the operation is refused with:

```json
{ "ok": false, "error": "dirty_tree",
  "message": "Working tree has uncommitted changes. Commit or stash them, then refresh." }
```

The UI shows a persistent banner when the working tree is dirty and disables all rebase, reset, reword, and delete actions until the next refresh reports it clean.

## REST API (Flask)

### App structure

```python
from flask import Flask, request, jsonify, send_from_directory, abort
from backend import GitHistory

app = Flask(__name__, static_folder="static", static_url_path="/static")

# Middleware to check token on /api/* routes
@app.before_request
def auth():
    if request.path.startswith("/api/") and not hmac.compare_digest(
            request.headers.get("X-Token", ""), app.config["TOKEN"]):
        abort(403)

# Handlers for each endpoint (below)
...

def main():
    app.config["GH"] = gh        # GitHistory instance
    app.config["TOKEN"] = token  # Auth token
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
```

### Endpoints and handlers

#### `GET /`
```python
@app.route("/")
def index():
    return send_from_directory("static", "index.html")
```

#### `GET /static/<path>`
```python
@app.route("/static/<path:path>")
def static_files(path):
    return send_from_directory("static", path)
```

#### `GET /api/state`
```python
@app.route("/api/state")
def api_state():
    return jsonify(app.config["GH"].read_state())
```

Returns the full state object (see below).

#### `POST /api/refresh`
```python
@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    return jsonify(app.config["GH"].read_state())
```

Identical to `GET /api/state`.

#### `POST /api/stash`
```python
@app.route("/api/stash", methods=["POST"])
def api_stash():
    return jsonify(app.config["GH"].stash())
```

#### `POST /api/stash/pop`
```python
@app.route("/api/stash/pop", methods=["POST"])
def api_stash_pop():
    return jsonify(app.config["GH"].stash_pop())
```

#### `POST /api/rebase`
```python
@app.route("/api/rebase", methods=["POST"])
def api_rebase():
    body = request.get_json() or {}
    return jsonify(app.config["GH"].rebase(
        operation=body.get("operation"),
        hashes=body.get("hashes"),
        order=body.get("order"),
        new_message=body.get("new_message")))
```

Request body:
```json
{
  "operation": "move" | "squash" | "fixup" | "reword",
  "hashes": ["<full hash>", ...],
  "order": ["<full hash>", ...],
  "new_message": "..."
}
```

#### `POST /api/rebase/continue`
```python
@app.route("/api/rebase/continue", methods=["POST"])
def api_rebase_continue():
    return jsonify(app.config["GH"].rebase_continue())
```

#### `POST /api/rebase/abort`
```python
@app.route("/api/rebase/abort", methods=["POST"])
def api_rebase_abort():
    return jsonify(app.config["GH"].rebase_abort())
```

#### `POST /api/reset`
```python
@app.route("/api/reset", methods=["POST"])
def api_reset():
    body = request.get_json() or {}
    return jsonify(app.config["GH"].reset(body.get("hash", "")))
```

Request body:
```json
{ "hash": "<full hash>" }
```

#### `POST /api/switch`

Request body:
```json
{ "branch": "<branch name>" }
```

Calls `switch_branch()`. Returns state on success, or an error with code `dirty_tree`, `rebase_in_progress`, `invalid_branch`, or `git_failed`.

#### `POST /api/submodule/update`

Calls `git submodule update --init`. Returns state or `git_failed`.

#### `POST /api/quit`
```python
@app.route("/api/quit", methods=["POST"])
def api_quit():
    response = jsonify({"ok": True})
    response.call_on_close(lambda: os._exit(0))
    return response
```

Shuts down the server process after the response is sent. The browser closes or replaces its content.

#### `GET /api/show?hash=<full hash>`
```python
@app.route("/api/show")
def api_show():
    return jsonify(app.config["GH"].show(request.args.get("hash", "")))
```

Returns:
```json
{
  "ok": true,
  "commit": {
    "hash": "...", "short_hash": "...", "message": "...",
    "author": "...", "date": "...", "branches": [...], "tags": [...]
  },
  "info": "<short_hash> <subject>",
  "diff": "output of git show <hash>"
}
```

### Response format

All endpoints return JSON. Mutations return:

```json
{
  "ok": true,
  "branch": "main",
  "branches": ["main", "feature-x"],
  "dirty": false,
  "has_stash": false,
  "rebase_in_progress": false,
  "conflict_files": [],
  "commits": [ {...}, ... ],
  "branch_history": [ {...}, ... ]
}
```

Errors:
```json
{
  "ok": false,
  "error": "dirty_tree" | "no_stash" | "nothing_to_stash" | "invalid_request" | "git_failed" | "start_update_failed" | "gitmodules_differ" | "gitmodules_in_range" | "invalid_branch" | "rebase_in_progress"
}
```

Conflicts:
```json
{
  "ok": false,
  "conflict": true,
  "conflict_files": ["src/foo.c", ...],
  "rebase_in_progress": true,
  ...full state...
}
```

### Error handling

Token auth failures are 403 with empty body. No global exception handler — Flask's default 500 applies to unhandled exceptions.

## State object shape

```json
{
  "ok": true,
  "branch": "main",
  "branches": ["main", "feature-x"],
  "dirty": false,
  "has_stash": false,
  "rebase_in_progress": false,
  "conflict_files": [],
  "commits": [
    {
      "hash": "a1b2c3d4e5f6...",
      "short_hash": "a1b2c3d",
      "message": "Fix login bug",
      "author": "Jane Smith",
      "date": "2026-04-01",
      "branches": ["main"],
      "tags": ["v1.2.0"],
      "is_head": true
    }
  ],
  "branch_history": [
    {
      "hash": "a1b2c3d...",
      "label": "commit: Fix login bug",
      "timestamp": "2026-04-06T10:00:00"
    }
  ]
}
```

- `branch`: currently checked-out branch name.
- `branches`: all local branch names; used to populate the branch switcher dropdown.
- `commits`: from `git log <start>..HEAD --format=...`, newest first.
- `branch_history`: from `git reflog show --format=...`, newest first, deduped (keep oldest occurrence of each hash).
- `rebase_in_progress` is true iff `.git/rebase-merge` or `.git/rebase-apply` exists.
- `conflict_files` is populated from `git diff --name-only --diff-filter=U` when `rebase_in_progress` is true.
- `submodule_update_suggested` (optional, boolean): present and `true` when a `reset` changed the set of gitlinks, indicating the UI should offer to run `git submodule update --init`.

## Backend: `GitHistory` class

The backend is a single class exercised by `tests/test_backend.py`. Every method returns a JSON-serializable dict; none raise exceptions on git failures.

```python
class GitHistory:
    def __init__(self, repo_path: str, start: str = "HEAD~200"):
        """Validate repo and initialize. Raises on bad input."""
        self.repo_path = Path(repo_path).resolve()
        # Validate git repo, not detached, resolve start, create temp dir
    
    def read_state(self) -> dict:
        """Returns full state dict."""
    
    def refresh(self) -> dict:
        """Alias for read_state."""
    
    def stash(self) -> dict:
        """Run git stash push. Returns state | error."""
    
    def stash_pop(self) -> dict:
        """Run git stash pop. Returns state | error."""
    
    def rebase(self, operation: str, hashes=None, order=None, new_message=None) -> dict:
        """Execute one rebase operation."""
    
    def rebase_continue(self) -> dict:
        """git rebase --continue."""
    
    def rebase_abort(self) -> dict:
        """git rebase --abort."""
    
    def reset(self, hash: str) -> dict:
        """git reset --hard <hash>. Returns submodule_update_suggested if gitlinks changed."""
    
    def switch_branch(self, branch: str) -> dict:
        """git switch <branch>. Updates _start to HEAD~200 of new branch."""
    
    def submodule_update(self) -> dict:
        """git submodule update --init."""
    
    def show(self, hash: str) -> dict:
        """Returns {ok, info, diff} from git show."""
```

## Backend: The Single Rebase Function

All five operations go through one function:

```python
def rebase(self, operation, hashes, order=None, new_message=None):
    ensure_clean_working_tree()           # raises -> error response
    commits = list_commits(depth)         # current order, oldest first in git terms
    base_idx = deepest_affected_index(operation, hashes, commits)
    base = parent_of(commits[base_idx])   # or None for --root
    todo = build_todo(operation, hashes, commits, base_idx, order)

    write_temp_file(TODO_PATH, todo)
    if operation == "reword":
        write_temp_file(MSG_PATH, new_message)

    env = os.environ.copy()
    env["GIT_SEQUENCE_EDITOR"] = quote_cmd(sys.executable, EDITOR_PY)
    env["GIT_EDITOR"]          = quote_cmd(sys.executable, EDITOR_PY)
    env["GIT_HISTORY_TODO"] = TODO_PATH
    env["GIT_HISTORY_MSG"]  = MSG_PATH

    cmd = ["git", "rebase", "-i"]
    cmd.append("--root" if base is None else base)

    result = subprocess.run(cmd, env=env, capture_output=True)
    if result.returncode == 0:
        return read_state()  # success
    return conflict_state()  # rebase paused with conflicts
```

`editor.py` (invoked by git as both GIT_SEQUENCE_EDITOR and GIT_EDITOR):

```python
import os, shutil, sys
target = sys.argv[1]
if target.endswith("git-rebase-todo"):
    shutil.copyfile(os.environ["GIT_HISTORY_TODO"], target)
elif os.environ.get("GIT_HISTORY_MSG") and os.path.exists(os.environ["GIT_HISTORY_MSG"]):
    shutil.copyfile(os.environ["GIT_HISTORY_MSG"], target)
# else: leave file untouched (squash, fixup, default commit messages)
```

## Backend: Todo Construction Per Operation

Commits are the list from the rebase base to HEAD, oldest first (the order git wants in the todo).

### `move`

The UI sends the desired full ordering of visible commits, newest first. Convert to oldest-first. Find the longest shared prefix with the current order; everything before that prefix is untouched. Generate `pick` lines for the rebase range.

### `squash`

Only consecutive (adjacent) commits may be squashed. The selected hashes must form a contiguous block in the commit list. Each selected commit is marked `squash` in the todo so it folds into the commit directly before it. Non-selected commits keep their `pick`.

### `fixup`

The single selected hash H. If H is the root commit, refuse with `invalid_request`. Otherwise, the todo includes H's parent as `pick` and H as `fixup`. Extend the rebase base if needed so H's parent is in the todo.

### `reword`

The single selected hash H. The new message is written to a temp file. In the todo:
- H: `pick`
- After H's pick line: `exec git commit --amend --allow-empty -F <msg_path>`
- Everything else: `pick`

This avoids relying on `GIT_EDITOR` for the message; the exec line applies the new message directly.

## Submodule Guards

Two guards prevent `.gitmodules` / gitlink corruption:

1. **move**: before building the todo, check whether any moved commit touches `.gitmodules` (via `git diff-tree --name-only`). If yes, refuse with `gitmodules_in_range`.
2. **reset**: compare `.gitmodules` content at HEAD vs the target hash. If they differ, refuse with `gitmodules_differ`. If they are the same but the set of gitlinks (160000-mode tree entries) differs, allow the reset and return `submodule_update_suggested: true` in the state so the UI can offer a `git submodule update --init` button.

## Editor Helper

`editor.py`, 6 lines: thin shim that copies pre-written todo or message files into place, or leaves the git-managed file alone (for squash, fixup, default commit messages).

Lives as a real file on disk (not written out at runtime). Invoked directly by git via `GIT_SEQUENCE_EDITOR` and `GIT_EDITOR` env vars.

## UI: Layout

```
┌─────────────────────────────────┬──────────────────────────┐
│  Commit History                 │  Branch History          │
│  ⠿ a1b2c3d [main][v1.2] Fix…    │  ● commit: Fix login bug │
│  ⠿ b2c3d4e          Add setting │    rebase (finish)       │
│  ⠿ c3d4e5f [v1.1.0] Refactor… │    commit: Add setting   │
│  …                              │    …                     │
└─────────────────────────────────┴──────────────────────────┘
```

Toolbar above the columns: logo, branch switcher dropdown, then action buttons. Status banner below toolbar (dirty tree, errors). Spinner overlay while any request is in flight.

## UI: Commit row

Left to right:
- Drag handle `⠿` — the only way to start a move-drag.
- Short hash — monospace, muted.
- Ref badges — branches blue, tags green.
- Commit message — primary text; double-click to edit.
- Author — muted.
- Date — muted, right-aligned.
- Row-hover actions (right): fixup `⤵`.

Row states: default, selected (light blue), dragging, editing message.

## UI: Interactions

### Branch switcher

A `<select>` dropdown in the toolbar populated from `state.branches`. The current branch is pre-selected. Changing the selection → `POST /api/switch` with `{ "branch": "<name>" }`. Disabled while the working tree is dirty or a rebase is in progress.

### Selection

- Click: select that row, deselect all others. Set the shift-click anchor. Shows that commit's diff.
- Shift-click: extend selection contiguously from the anchor.
- Ctrl-click / Cmd-click: not supported. Only contiguous (adjacent) selection is allowed.
- Escape: cancel drag only. Selection is unchanged.
- One commit is always selected. On load and after every operation, the top commit (HEAD) is selected if no prior selection survives.

### Move (drag)

1. Mousedown on `⠿` starts move-drag. If the row was unselected, it becomes the only selection.
2. The selected block lifts visually; a horizontal drop line shows the insertion point.
3. On mouseup, `POST /api/rebase` is called with `operation: "move"` and the full new newest-first hash order.
4. UI is locked via spinner. On success: refresh state. On conflict: open the conflict dialog.

### Squash

A floating action bar appears above the selection when ≥2 consecutive rows are selected. Button: **Squash**. Non-consecutive selection is not supported for squash.

**Squash** → `POST /api/rebase` with `operation: "squash"` and the selected hashes (must be adjacent). Git's default squash concatenates messages automatically.

### Fixup

The `⤵` button on a row, visible on hover. Disabled on the root commit. Click → `POST /api/rebase` with `operation: "fixup"` and that single hash.

### Reword

Double-click the message → inline input. Enter or blur-with-changes → `POST /api/rebase` reword. Escape or blur-without-changes → cancel.

### Conflict dialog

A modal overlay will be shown whenever `state.rebase_in_progress` is true, prompting the user to either resolve the conflict outside git-history and then click "Continue" or press "Cancel" which will abort the rebase (resetting to the state before the rebase was attempted.) 

- **Cancel** → `POST /api/rebase/abort`.
- **Continue** → `POST /api/rebase/continue`. If still conflicted, the dialog stays open.
- All other UI is blocked while the dialog is open.
- The dialog is rebuilt from `state.conflict_files` on every refresh, so it survives tab reload.

### Branch History panel

- Vertical list, newest at top.
- Each entry: short hash + raw `%gs` label + date.
- Current HEAD entry has an accent border and a branch badge.
- **Double click** → `POST /api/reset` to that hash. Disabled while the working tree is dirty or a rebase is in progress.
- **Group consecutive rebases** checkbox: when checked, consecutive entries with label `"rebase"` are collapsed to one.

### Undo / Redo

**Undo** and **Redo** buttons navigate the Branch History by one step relative to the current HEAD entry.

- **Undo**: `POST /api/reset` to the entry one step older than HEAD in the Branch History list.
- **Redo**: `POST /api/reset` to the entry one step newer than HEAD.
- Both disabled when the working tree is dirty or a rebase is in progress.
- Undo disabled when HEAD is already the oldest visible Branch History entry.
- Redo disabled when HEAD is the newest visible Branch History entry.

### Quit

**Quit button** (top bar): sends `POST /api/quit` (keepalive), then closes the window and replaces the page content with a "Server stopped" message.

### Stash & refresh

- **Stash button**: visible in the dirty-tree banner (when dirty). Click → `POST /api/stash`. After success, the banner clears.
- **Stash pop button**: visible when `state.has_stash` is true and tree is clean. Click → `POST /api/stash/pop`. After success, the tree becomes dirty (changes restored).
- **Refresh button**: top-right, always visible. Click → `POST /api/refresh`, re-fetches state. Also fires automatically on `window.focus` so terminal changes are picked up.
- **Submodule update button**: shown in a banner when `state.submodule_update_suggested` is true. Click → `POST /api/submodule/update`. After success, the banner clears.

### Diff pane

A resizable panel at the bottom of the window shows the diff for the selected commit (`GET /api/show?hash=<hash>`). It is always visible when a commit is selected, and is refreshed automatically after every successful operation. Files changed are listed on the left; the unified diff is shown on the right with added lines in green and deleted lines in red.

## UI: Error handling

Non-conflict errors show a dismissible red banner above the columns. The list keeps its last-known-good state. Content is the `message` field from the API response.

`dirty_tree` errors show a persistent yellow banner with a **Stash** button.

## Logging

After every successful mutating operation (rebase, rebase_continue, reset), the backend appends one line to `~/.git-history.log`:

```
<iso-timestamp> <branch> <full-hash>
```

The file is append-only and shared across all repos and branches. The footer contains a **Log** link (`/log`) that serves the file as plain text (no token required).

## Implementation notes

1. **Full hashes everywhere internally.** Short hashes only for display.
2. **File encoding.** All temp files (TODO_PATH, MSG_PATH) are written with `encoding="utf-8"` and `newline="\n"`. Git rejects CRLF todo files on Windows.
3. **Forward slashes in env-var paths.** `GIT_SEQUENCE_EDITOR` and `GIT_EDITOR` are set using `shlex.quote`. Git's bundled bash on Windows parses this correctly.
4. **Temp file lifetime.** Per-operation temp files created via `tempfile.mkstemp`; deleted in a `finally` block after the rebase call.
5. **Dirty-tree check.** `git status --porcelain --untracked-files=no`. Any non-empty output means dirty.
6. **Conflict file list.** `git diff --name-only --diff-filter=U` when `.git/rebase-merge` exists.
7. **Token check failure.** `X-Token` mismatch → HTTP 403, empty body.
8. **Detached HEAD refusal.** Refused at startup via `git symbolic-ref --quiet HEAD`.
9. **git log format.** `--format=%H%x1f%h%x1f%an%x1f%ai%x1f%B%x1f%D%x00` — unit-separator (`\x1f`) delimited, null-byte record terminator. `%B` preserves the full commit body for reword round-trips. `%ai` gives ISO 8601 date; the UI displays only the first 10 characters.
10. **Single-process invariant.** If a second git-history is launched in the same repo, it gets its own port and token. Both can run; git's `.git/index.lock` provides safety.

## Installation

```
pip install git+https://github.com/BjarneH/git-history
```

Then run `git-history` from any git repository.
