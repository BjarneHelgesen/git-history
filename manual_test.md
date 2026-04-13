# Manual Test Checklist

Run these checks before each release.

---

## 1. CLI startup

Use the test repo created by `python make_test_repo.py /tmp/test-repo`.

| # | Step | Expected |
|---|------|----------|
| 1 | `cd /tmp/test-repo && python git_history.py` | Browser opens at `http://127.0.0.1:<port>/` with `?t=<token>` in the URL |
| 2 | Wait for page to load, then inspect the address bar | Token is gone — URL is just `/` |
| 3 | Reload the page (no `?t=` in URL) | App still works (token persisted in localStorage) |
| 4 | `python git_history.py --port 9876` | Server binds to port 9876 |
| 5 | `python git_history.py HEAD~5` | Only 5 commits visible |

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
| 1 | With the app open, switch to another window and make a commit in the terminal (`git commit --allow-empty -m "test"`) | *(nothing yet)* |
| 2 | Click back on the browser window | Commit list refreshes automatically and shows the new commit |

---

## Suggested automated test additions

- **`--port` flag**: verify server binds to the specified port (testable in `test_cli.py` without a browser).
- **`HEAD~5` argument**: start the server with a revision limit and assert the API returns only that many commits.
- **Conflict continue**: after triggering a conflict, resolve the file programmatically and click Continue — currently untested in `test_ui.py`.
- **Window focus refresh**: dispatch a `visibilitychange` event via `page.evaluate` in Playwright and assert the commit list updates.
