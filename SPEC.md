# git warp: Specification

See [README.md](README.md) for an overview. 

## CLI


| Argument | Default   | Meaning |
|----------|-----------|---------|
| `--port` | auto | TCP port on `127.0.0.1`. If omitted, picks a free port. |
| `--clear-log` | — | Delete `~/.git-warp.log` and exit. |
| `--undo [N]` | N=1 | Reset HEAD back N steps in the Undo Stack and exit. |
| `--redo [N]` | N=1 | Reset HEAD forward N steps in the Undo Stack and exit. |
| `--dark` | off | Force dark mode in the browser UI. |
| `--light` | off | Force light mode in the browser UI. |

## Startup

1. Validate: git >= 2.26, in a repo, not detached HEAD.
2. Pick port (auto or `--port N`), generate 32-char token (`secrets.token_urlsafe(24)`).
3. Start Flask server on `127.0.0.1:<port>` with state starting at `HEAD~1000` (or repo root if fewer commits).
4. Print `git warp running at http://127.0.0.1:<port>/?t=<token>  —  Ctrl+C to quit` and open browser.

## Shutdown

Two triggers: Ctrl+C in terminal or Quit button (`POST /api/quit`). On Ctrl+C, the server exits normally. On quit, `os._exit(0)` is called after the response.

The server never touches the repo on shutdown. Conflicted rebase state is preserved in `.git/rebase-merge`; restarting `git warp` picks it up.

## Security

- Server binds only to `127.0.0.1`.
- A 32-char random token is generated at startup and embedded in the launch URL as `?t=<token>`.
- `index.html` and `static/*` are served without token checks (harmless on localhost).
- `app.js`, on load: reads `t` from `window.location.search`, stores it in `sessionStorage` under `git_warp_token`, then calls `history.replaceState(null, '', '/')` to strip the query string from history.
- Every `/api/*` request sends the token in the `X-Token` header. Mismatch/missing → HTTP 403 with empty body.

## Concurrency

Flask's development server (used here) is single-threaded. Requests are processed in arrival order; a long-running rebase blocks other requests until it finishes. No locks needed because there is no concurrency. The UI shows a spinner while any request is in flight and disables mutating controls.


## Working Tree Policy

Before every mutating operation the server runs:

```
git status --porcelain --untracked-files=no
```

If the output is non-empty, the operation is refused.

The UI shows a persistent banner when the working tree is dirty and disables all rebase, reset, reword, and delete actions until the next refresh reports it clean.

## Empty Commits

An operation never removes a commit the user did not ask to remove. A commit that ends up with no changes is kept, empty, and stays in the list with its message.

This covers three cases:

- **Replays to nothing** — a reorder moves a commit past the one reverting it, so its changes are already in its new parent. `--empty=keep` keeps it and the rebase does not pause.
- **Squash or fixup with an empty combined diff** — e.g. a commit creating a file folded with the one deleting it. Git pauses here (it refuses to amend a commit into emptiness) and `--empty=` does not apply; `git rebase --continue` commits it empty. The fold itself still reduces the commit count, as any squash does.
- **Already empty** — a commit created with `git commit --allow-empty` survives every operation unchanged.

The working tree is unaffected in all three cases: the change was already present, so only the commit record is empty. Removing an unwanted empty commit is an explicit fixup or squash.

## REST API

### Endpoints

- `GET /` — serves `index.html`.
- `GET /static/<path>` — serves static files.
- `GET /api/state` — returns full state object.
- `POST /api/stash` — runs `git stash push`.
- `POST /api/stash/pop` — runs `git stash pop`.
- `POST /api/rebase/move` — reorder commits. Request body: `{ "order": ["<hash>", ...], "select_index": <int> }`
- `POST /api/rebase/squash` — squash commits. Request body: `{ "commit_hashes": ["<hash>", ...], "select_index": <int> }`
- `POST /api/rebase/fixup` — fixup commit into parent. Request body: `{ "commit_hashes": ["<hash>", ...], "select_index": <int> }`
- `POST /api/rebase/reword` — rename commit message. Request body: `{ "commit_hashes": ["<hash>"], "new_message": "...", "select_index": <int> }`
- `POST /api/rebase/split` — split commit. Request body: `{ "commit_hash": "<hash>", "files_to_split": ["<path>", ...] }`

  `select_index` (move/squash/fixup/reword, optional) is the index into the post-operation `commits` list whose diff the server bundles into the response `diff` field (see State object shape). The client sends an index rather than a hash because the resulting hash does not exist until the operation runs. Omitted or out of range → `diff` stays `null`.
- `POST /api/branch` — create branch. Request body: `{ "branch_name": "<name>", "commit_hash": "<hash>" }`.
- `DELETE /api/branch` — delete branch. Request body: `{ "branch_name": "<name>" }`.
- `POST /api/rebase/continue` — runs `git rebase --continue`.
- `POST /api/rebase/abort` — runs `git rebase --abort`.
- `POST /api/reset` — hard reset. Request body: `{ "commit_hash": "<hash>" }`.
- `POST /api/switch` — switch branch. Request body: `{ "branch": "<name>", "allow_different_gitmodules": <bool> }` (allow_different_gitmodules optional, default false).
- `POST /api/submodule/update` — runs `git submodule update --init`.
- `POST /api/quit` — shuts down server.
- `GET /api/show?commit_hash=<hash>` — returns commit details and diff.

### Response format

All responses are JSON. Mutations return the state object. Errors return:
```json
{
  "ok": false,
  "error": "dirty_tree" | "no_stash" | "nothing_to_stash" | "stash_conflict" | "invalid_request" | "git_failed" | "gitmodules_differ" | "gitmodules_in_range" | "invalid_branch" | "rebase_in_progress" | "detached_head" | "invalid_commit" | "not_in_rebase" | "history_changed" | "cannot_delete_current_branch" | "split_failed",
  "message": "<optional human-readable detail>"
}
```

Conflicts return full state with `"ok": false, "conflict": true, "conflict_files": [...]`.

Token auth failures return HTTP 403 with empty body. Unhandled exceptions return Flask's default 500.

## State object shape

```json
{
  "ok": true,
  "branch": "main",
  "upstream": "origin/main",
  "branches": ["main", "feature-x"],
  "dirty": false,
  "has_stash": false,
  "rebase_in_progress": false,
  "conflict": false,
  "conflict_files": [],
  "reflog_expiry": "30 days",
  "commits": [
    {
      "commit_hash": "a1b2c3d4e5f6...",
      "short_hash": "a1b2c3d",
      "message": "Fix login bug",
      "author": "Jane Smith",
      "date": "2026-04-01",
      "branches": ["main"],
      "tags": ["v1.2.0"],
      "is_head": true,
      "pushed": false
    }
  ],
  "undo_stack": [
    {
      "commit_hash": "a1b2c3d...",
      "label": "commit: Fix login bug",
      "timestamp": "2026-04-06T10:00:00"
    }
  ],
  "diff": null
}
```

Fields:
- `branch` — currently checked-out branch.
- `upstream` — the branch's upstream (e.g. `origin/main`), or `""` if none; drives the `pushed` flag on commits.
- `branches` — all local branches.
- `commits` — all visible commits, newest first. When the index has staged changes, a synthetic first entry with `commit_hash`/`short_hash` `"(Staged)"` represents them; `GET /api/show?commit_hash=%28Staged%29` returns the staged diff (`git diff --cached`).
- `undo_stack` — branch operations from reflog, newest first, deduped to oldest occurrence of each hash. Persists for `gc.reflogExpireUnreachable` (default 30 days).
- `dirty` — working tree has uncommitted changes.
- `has_stash` — stashed changes exist.
- `rebase_in_progress` — `.git/rebase-merge` or `.git/rebase-apply` exists.
- `conflict` — rebase has conflicts.
- `conflict_files` — files with conflicts.
- `reflog_expiry` — git config value shown in Undo Stack footer.
- `submodule_update_suggested` (optional) — reset changed gitlinks, offer `git submodule update --init`.
- `diff` — `null` normally. When a move/squash/fixup/reword request supplies `select_index` (see REST API), the server bundles the post-operation selected commit's show response (`{ "commit": {...}, "diff": "...", "files": [...] }`) here so the diff pane refreshes in one round-trip.

## Safeguards

**Undo Stack Expiration:** sourced from reflog, persists for `gc.reflogExpireUnreachable` (default 30 days, configurable).

**Submodule Guards:** prevent `.gitmodules` / gitlink corruption:
- **move**: refuse with `gitmodules_in_range` if any moved commit touches `.gitmodules`.
- **reset**: refuse with `gitmodules_differ` if `.gitmodules` content differs. If same content but gitlink set differs, allow and return `submodule_update_suggested: true`.
- **switch**: warn with `gitmodules_differ` if `.gitmodules` content differs; the UI confirms and re-sends with `allow_different_gitmodules: true` to proceed (unlike `reset`, which refuses).

## UI Layout

Two-column layout: Commit History (left) and Undo Stack (right).

Commit row elements (left to right): drag handle `⠿`, short hash, ref badges (blue=branch, green=tag), message, author, date. Row-hover action: fixup (⤵). States: default, selected (blue), dragging, editing.

Toolbar: logo, branch switcher, action buttons. Status banner below (dirty tree, errors). Spinner overlay while request in flight.

## UI Interactions

**Selection:** Click to select (set anchor), Shift-click to extend contiguously. Ctrl/Cmd-click not supported. Escape cancels drag, or clears selection entirely if not dragging. HEAD is selected as a fallback whenever an operation leaves the selection empty. Clicking a row shows its diff.

**Move (drag):** Mousedown on `⠿` lifts selection with drop-line. Mouseup sends `POST /api/rebase/move` with new hash order. On conflict, opens dialog.

**Squash:** Floating action bar when ≥2 consecutive selected. Sends `POST /api/rebase/squash` with hashes.

**Fixup:** Row-hover button ⤵ (disabled on root). Sends fixup operation.

**Reword:** Row-hover pencil button (✏️) between message and author → inline edit. Ctrl/Cmd+Enter or blur-with-changes sends reword. Escape/blur-without-changes cancels.

**Split:** Select a single commit; the diff pane lists its files. Click files to choose a strict, non-empty subset (Shift-click extends a range, Ctrl/Cmd-click toggles individual files). The "Split Commit" button (in the diff pane) sends `POST /api/rebase/split` with `commit_hash` and `files_to_split`. The button is shown only for a single non-staged commit with ≥2 files, and is enabled only while a strict subset is selected. The commit is replaced by two: the unselected files, then the selected files, both reusing its author and message.

**Branch switcher:** `<select>` dropdown. Sends `POST /api/switch`. Disabled if dirty or rebase in progress.

**Undo Stack:** Vertical list, newest first. Double-click entry → `POST /api/reset` to that hash. Current HEAD has accent border and badge. Disabled if dirty or rebase in progress.

**Undo/Redo:** Navigate the Undo Stack by one step. Disabled when at oldest/newest or if dirty/rebasing.

**Conflict dialog:** Modal shown when `rebase_in_progress` is true. Cancel → `POST /api/rebase/abort`. Continue → `POST /api/rebase/continue`. Blocks other UI. Survives tab reload.

**Stash button:** Visible in dirty-tree banner. Sends `POST /api/stash`.

**Stash pop button:** Visible when stash exists and tree clean. Sends `POST /api/stash/pop`.

**Refresh button:** Top-right, always visible. Sends `GET /api/state`. Also fires on `window.focus`.

**Submodule update button:** Modal confirm shown after reset/switch when `submodule_update_suggested` true. Sends `POST /api/submodule/update`.

**Diff pane:** Resizable panel at bottom. Shows selected commit's diff with files left, unified diff right (green=added, red=deleted). Refreshes after every successful operation. The file list also hosts the "Split Commit" button (see **Split**).

**Quit button:** Sends `POST /api/quit` then displays "Server stopped" message.

**Error handling:** Non-conflict errors show a red banner, cleared on the next successful operation or refresh. Dirty-tree errors show persistent yellow banner with Stash button.

## Logging

After every history-rewriting operation, append one line to `~/.git-warp.log`:
```
<iso-timestamp> <branch> <full-hash>
```
Append-only, shared across all repos/branches. Served as plain text via `/log` link (no token required).
