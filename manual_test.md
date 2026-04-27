# Manual Test Checklist

Run these checks before each release.

---

## 1. CLI startup

Use the test repo created by `python make_test_repo.py test-repo` (run from the git-history folder).

| # | Step | Expected |
|---|------|----------|
| 1 | `cd test-repo` then `python ..\git_history.py` | Browser opens at `http://127.0.0.1:<port>/` with `?t=<token>` in the URL |
| 2 | Wait for page to load, then inspect the address bar | Token is gone — URL is just `/` |
| 3 | Reload the page (no `?t=` in URL) | App still works (token persisted in localStorage) |
| 4 | `python ..\git_history.py --port 9876` | Server binds to port 9876 |
| 5 | `python ..\git_history.py HEAD~5` | Only 5 commits visible |

---

## 2. Conflict — continue after manual resolve

Drag-and-drop and conflict abort are covered by automated tests.

| # | Step | Expected |
|---|------|----------|
| 1 | Trigger a conflicting delete (requires a repo with overlapping changes) | Conflict modal appears listing the conflicted files |
| 2 | Manually resolve the conflicted file, `git add` it, then click Continue | Modal disappears; rebase completes; commit list updated |

---

## 3. Window focus auto-refresh

| # | Step | Expected |
|---|------|----------|
| 1 | With the app open, switch to another window and make a commit in the terminal (`git commit --allow-empty -m "test"`) Click back on the browser window  | Commit list refreshes automatically and shows the new commit |

---

---

## 4. Branch switching

| # | Step | Expected |
|---|------|----------|
| 1 | With a repo that has multiple local branches, open the app | Branch dropdown shows current branch, all local branches listed |
| 2 | Select a different branch from the dropdown | Commit list and branch history update to the new branch; toolbar shows new branch name |
| 3 | Make a change without committing, then try to switch branches via the dropdown | Switch branch is not permitted |

---

## 5. Submodule guard

| # | Step | Expected |
|---|------|----------|
| 1 | In a repo with a submodule, find a commit that modifies `.gitmodules`, then drag it to a different position | Operation refused with an error message |
| 2 | Reset (double-click in branch history) to an entry where submodule commit pointers differ but `.gitmodules` is unchanged | Reset succeeds; user is prompted to "Update submodules"|

---

## 6. `--clear-log`

| # | Step | Expected |
|---|------|----------|
| 1 | `python git_history.py --clear-log` (after some operations have been logged) | Prints `Deleted <log path>` and exits |
| 2 | `python git_history.py --clear-log` (no log file present) | Prints `No log file at <log path>` and exits |

