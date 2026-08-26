"""
UI regression tests using Playwright against a live Flask server.

Requires: pip install pytest-playwright && playwright install chromium
Run with: python -m pytest tests/test_ui.py -v
"""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_warp.rest_api import create_app
from conftest import _ensure_persistent_test_repo

TOKEN = "test-ui-token-abcdefgh12345678"


def _setup_ui_server(tmp_path_factory, prefix):
    """Set up a live git-warp server for UI testing."""
    work = tmp_path_factory.mktemp(prefix)
    repo = work / "repo"
    template = _ensure_persistent_test_repo()
    subprocess.run(["git", "clone", str(template), str(repo)],
                   capture_output=True, check=True)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "protocol.file.allow", "always"], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "submodule", "update", "--init", "--recursive"], cwd=str(repo),
                   capture_output=True, check=True)
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=str(repo),
                   capture_output=True, check=True)
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(repo),
                             capture_output=True, check=True, text=True).stdout.strip()
    initial_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                                   capture_output=True, check=True, text=True).stdout.strip()
    app = create_app(str(repo), TOKEN)
    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {
        "url": f"http://127.0.0.1:{port}/?t={TOKEN}",
        "repo": repo,
        "branch": branch,
        "initial_head": initial_head,
    }
    server.shutdown()


def _restore_ui_repo(server):
    """Undo whatever the previous phase did, back to the fixture's initial commit."""
    repo = server["repo"]
    subprocess.run(["git", "rebase", "--abort"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "checkout", server["branch"]], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "reset", "--hard", server["initial_head"]], cwd=str(repo),
                    capture_output=True, check=True)
    subprocess.run(["git", "clean", "-fd"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "submodule", "update", "--init", "--force"],
                    cwd=str(repo), capture_output=True)


@pytest.fixture
def ui_live_server(tmp_path_factory):
    yield from _setup_ui_server(tmp_path_factory, "ui-work")


def _row(page, message):
    return page.locator(".commit-row").filter(has=page.locator(".message", has_text=message))


def drag_row(page, source_locator, target_locator, position="above"):
    """Simulate an HTML5 drag by dispatching DragEvents directly."""
    try:
        source_locator.scroll_into_view_if_needed()
        target_locator.scroll_into_view_if_needed()

        if not source_locator.is_visible() or not target_locator.is_visible():
            raise RuntimeError("Locators not visible")

        source_el = source_locator.element_handle()
        target_el = target_locator.element_handle()

        page.evaluate("""([src, tgt, pos]) => {
            const handle = src.querySelector('.drag-handle');
            handle.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() }));
            const rect = tgt.getBoundingClientRect();
            const clientY = pos === 'above' ? rect.top + 2 : rect.bottom - 2;
            const list = document.querySelector('#commits-list');
            list.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer(), clientY }));
            list.dispatchEvent(new DragEvent('drop',    { bubbles: true, cancelable: true, dataTransfer: new DataTransfer(), clientY }));
            list.dispatchEvent(new DragEvent('dragend', { bubbles: true }));
        }""", [source_el, target_el, position])
    except Exception as e:
        print(f"ERROR in drag_row: {e}")
        raise


def drag_row_and_wait(page, source_locator, target_locator, position="above"):
    response_received = []
    all_responses = []

    def capture_response(response):
        all_responses.append(response.url)
        if "/api/rebase" in response.url:
            response_received.append(response)

    # Set up listener before drag
    page.on("response", capture_response)
    drag_row(page, source_locator, target_locator, position)

    # Wait up to 15 seconds for response to be captured
    for _ in range(150):
        if response_received:
            break
        page.wait_for_timeout(100)

    # Remove listener after we're done with this drag
    page.remove_listener("response", capture_response)

    if not response_received:
        raise TimeoutError(f"No /api/rebase response received within 15 seconds")

    # Wait for render() to complete — hideSpinner() fires after handleResponse, which calls render()
    page.wait_for_selector("#spinner", state="hidden", timeout=5000)


def _selected_idx(page):
    return page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('.commit-row'));
        const sel = document.querySelector('.commit-row.selected');
        return sel ? rows.indexOf(sel) : -1;
    }""")


@pytest.mark.release
def test_all_ui(page, ui_live_server):
    # Live server tests (all use the same page, continuous mutations)
    page.goto(ui_live_server["url"])
    page.wait_for_selector(".commit-row")

    # Section 1: Basic page load and initial state
    assert page.locator(".commit-row").count() == 28
    assert page.locator(".undo-stack-row").count() > 0

    # Section 2: Selection and action bar
    assert not page.locator("#btn-squash").is_visible()
    page.locator(".commit-row").nth(0).click()
    assert not page.locator("#btn-squash").is_visible()
    page.locator(".commit-row").nth(1).click(modifiers=["Shift"])
    assert page.locator("#btn-squash").is_visible()

    # Section 3: Fixup button disabled on oldest
    oldest = page.locator(".commit-row").last
    oldest.hover()
    assert oldest.locator("button[title='Fixup']").is_disabled()

    # Section 4: Inline reword via pencil button
    page.locator(".commit-row").nth(0).hover()
    page.locator(".commit-row").nth(0).locator("button[title='Edit commit message']").click()
    ta = page.locator(".reword-input")
    ta.wait_for()
    ta.fill("reworded message")
    ta.press("Control+Enter")
    page.wait_for_function("!document.querySelector('.reword-input')")
    assert page.locator(".commit-row").nth(0).locator(".message").inner_text() == "reworded message"

    # Section 5: additional rewords
    for msg in ["first reword", "second reword"]:
        page.locator(".commit-row").nth(0).hover()
        page.locator(".commit-row").nth(0).locator("button[title='Edit commit message']").click()
        ta = page.locator(".reword-input")
        ta.wait_for()
        ta.fill(msg)
        ta.press("Control+Enter")
        page.wait_for_function("!document.querySelector('.reword-input')")

    # Section 6: Fixup preserves selection (26 → 25)
    count_before = page.locator(".commit-row").count()
    page.locator(".commit-row").nth(2).hover()
    page.locator(".commit-row").nth(2).locator("button[title='Fixup']").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 1}")
    assert _selected_idx(page) == 2

    # Section 7: Squash preserves selection (25 → 24)
    count_before = page.locator(".commit-row").count()
    page.locator(".commit-row").nth(2).click()
    page.locator(".commit-row").nth(3).click(modifiers=["Shift"])
    with page.expect_response("**/api/rebase/**"):
        page.locator("#btn-squash").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 1}")
    assert _selected_idx(page) == 2

    # Section 8: Reword preserves selection (24 still)
    page.locator(".commit-row").nth(3).hover()
    page.locator(".commit-row").nth(3).locator("button[title='Edit commit message']").click()
    ta = page.locator(".reword-input")
    ta.wait_for()
    ta.fill("reworded message 2")
    with page.expect_response("**/api/rebase/**"):
        ta.press("Control+Enter")
    page.wait_for_function("!document.querySelector('.reword-input')")
    assert _selected_idx(page) == 3

    # Section 9: Squash (24 → 23)
    count_before = page.locator(".commit-row").count()
    page.locator(".commit-row").nth(0).click()
    page.locator(".commit-row").nth(1).click(modifiers=["Shift"])
    page.locator("#btn-squash").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 1}")
    assert page.locator(".commit-row").count() == count_before - 1

    # Section 10: Fixup button (23 → 22)
    count_before = page.locator(".commit-row").count()
    row = page.locator(".commit-row").nth(1)
    row.hover()
    row.locator("button[title='Fixup']").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 1}")
    assert page.locator(".commit-row").count() == count_before - 1

    # Section 11: Right-click on non-HEAD commit resets branch to that commit
    count_before = page.locator(".commit-row").count()
    target_hash = page.locator(".commit-row").nth(2).get_attribute("data-commit-hash")
    page.locator(".commit-row").nth(2).click(button="right")
    with page.expect_response("**/api/reset"):
        page.locator("#ctx-reset").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 2}")
    assert page.locator(".commit-row").nth(0).get_attribute("data-commit-hash") == target_hash
    # Confirm submodule update if the reset crossed the submodule-pointer commit and
    # triggered the modal (the generic prompt-modal is reused for this confirmation).
    if page.evaluate("() => !document.getElementById('prompt-modal').classList.contains('hidden')"):
        with page.expect_response("**/api/submodule/update"):
            page.locator("#prompt-modal-ok").click()
        page.wait_for_function("document.getElementById('prompt-modal').classList.contains('hidden')")

    # Section 12: Right-click on HEAD commit does not show reset
    head_hash = page.locator(".commit-row").nth(0).get_attribute("data-commit-hash")
    count_before = page.locator(".commit-row").count()
    page.locator(".commit-row").nth(0).click(button="right")
    assert page.locator("#ctx-reset").is_hidden()
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    assert page.locator(".commit-row").count() == count_before
    assert page.locator(".commit-row").nth(0).get_attribute("data-commit-hash") == head_hash

    # Section 13: Stash button when dirty and stash/pop combined (end of ui_live_server tests)
    assert not page.locator("#btn-stash").is_visible()
    (ui_live_server["repo"] / "README.md").write_text("modified")
    page.locator("#btn-refresh").click()
    page.wait_for_selector("#btn-stash", state="visible")
    assert page.locator("#btn-stash").is_visible()
    page.locator("#btn-stash").click()
    page.wait_for_selector("#btn-stash-pop", state="visible")
    assert page.locator("#btn-stash-pop").is_visible()
    page.locator("#btn-stash-pop").click()
    page.wait_for_selector("#btn-stash", state="visible")
    assert page.locator("#btn-stash").is_visible()

    # Drag tests (repo restored to its initial state, same server)
    _restore_ui_repo(ui_live_server)
    page.goto(ui_live_server["url"])
    page.wait_for_selector(".commit-row")

    # Section 12: Drag to same position is noop (non-mutating, run first)
    original = [r.get_attribute("data-commit-hash")
                for r in page.locator(".commit-row").all()]
    drag_row(page,
        page.locator(".commit-row").nth(0),
        page.locator(".commit-row").nth(0),
        "above")
    page.wait_for_timeout(300)
    result = [r.get_attribute("data-commit-hash")
              for r in page.locator(".commit-row").all()]
    assert result == original

    # Section 13: Mid-drag visual state — drop indicator, data-dragging attribute, dragged-row opacity.
    # Dispatch HTML5 drag events directly: dragstart sets data-dragging and .dragging class,
    # dragover creates the drop indicator. Dragend (no drop) cancels without triggering a rebase.
    page.evaluate("""() => {
        const handle = document.querySelector('.commit-row .drag-handle');
        handle.dispatchEvent(new DragEvent('dragstart', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer() }));
    }""")
    page.evaluate("""() => {
        const list = document.querySelector('#commits-list');
        const rect = list.getBoundingClientRect();
        list.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true, dataTransfer: new DataTransfer(), clientY: rect.top + 5 }));
    }""")
    assert page.locator(".drop-indicator").count() > 0
    assert page.locator("#commits-list[data-dragging]").count() == 1
    assert page.evaluate("getComputedStyle(document.querySelector('.commit-row.dragging')).opacity") == "0.02"
    page.evaluate("""() => {
        document.querySelector('#commits-list').dispatchEvent(new DragEvent('dragend', { bubbles: true }));
    }""")

    # Reload to reset state for mutating tests
    page.reload()
    page.wait_for_selector(".commit-row")

    # Section 15: Drag reorder commits (must run first due to state dependency).
    # Reorder three commits near HEAD; dragging deeper commits would replay the
    # whole upper history for the same test result.
    drag_row_and_wait(page,
        _row(page, "Add admin panel"),
        _row(page, "Add error pages"),
        "above")
    drag_row_and_wait(page,
        _row(page, "Add deployment config"),
        _row(page, "Add error pages"),
        "above")
    all_messages = [r.locator(".message").inner_text()
                    for r in page.locator(".commit-row").all()]
    reordered = [m for m in all_messages
                 if m in {"Add error pages", "Add deployment config", "Add admin panel"}]
    assert reordered == [
        "Add admin panel",
        "Add deployment config",
        "Add error pages",
    ]

    # Section 16: Drag group of selected commits
    page.wait_for_timeout(200)
    msgs_before = [r.locator(".message").inner_text()
                   for r in page.locator(".commit-row").all()]
    msg5_before = msgs_before[5]
    msg6_before = msgs_before[6]

    page.locator(".commit-row").nth(5).click()
    page.locator(".commit-row").nth(6).click(modifiers=["Shift"])

    drag_row_and_wait(page,
        page.locator(".commit-row").nth(5),
        page.locator(".commit-row").nth(8),
        "below")

    msgs_after = [r.locator(".message").inner_text()
                  for r in page.locator(".commit-row").all()]

    assert msgs_after.index(msg5_before) > 6
    assert msgs_after.index(msg6_before) > 6
    assert sorted(msgs_after) == sorted(msgs_before)

    # Section 17: Drag preserves selection
    page.wait_for_timeout(500)
    # Select commit at index 2 before dragging (selection preservation works on selected commits)
    page.locator(".commit-row").nth(2).click()
    msg_at_2 = page.locator(".commit-row").nth(2).locator(".message").inner_text()
    page.wait_for_timeout(100)
    drag_row_and_wait(page,
        page.locator(".commit-row").nth(2),
        page.locator(".commit-row").nth(5),
        "below")
    # Selection should be preserved on the commit we dragged
    selected_msg = page.evaluate("""() => {
        const sel = document.querySelector('.commit-row.selected');
        return sel ? sel.querySelector('.message').textContent : null;
    }""")
    assert selected_msg == msg_at_2

    # Conflict tests (repo restored to its initial state, same server)
    _restore_ui_repo(ui_live_server)
    page.goto(ui_live_server["url"])
    page.wait_for_selector(".commit-row")
    initial_count = page.locator(".commit-row").count()

    # Section 18: Conflict modal appears
    drag_row(page, _row(page, "conflict: version A"), _row(page, "conflict: version B"), position="above")
    page.wait_for_selector("#conflict-modal:not(.hidden)")
    assert page.locator("#conflict-files li").count() > 0

    # Section 19: Conflict abort (continue from conflict state, don't re-drag)
    page.locator("#btn-abort").click()
    page.wait_for_selector("#conflict-modal", state="hidden")
    assert page.locator(".commit-row").count() == initial_count


@pytest.fixture
def context_menu_server(tmp_path_factory):
    yield from _setup_ui_server(tmp_path_factory, "ui-ctx")


@pytest.mark.release
def test_context_menu(page, context_menu_server):
    page.goto(context_menu_server["url"])
    page.wait_for_selector(".commit-row")

    # Section 1: Reword via pencil button
    page.locator(".commit-row").nth(0).hover()
    page.locator(".commit-row").nth(0).locator("button[title='Edit commit message']").click()
    ta = page.locator(".reword-input")
    ta.wait_for()
    ta.fill("reworded via pencil button")
    with page.expect_response("**/api/rebase/**"):
        ta.press("Control+Enter")
    page.wait_for_function("!document.querySelector('.reword-input')")
    assert page.locator(".commit-row").nth(0).locator(".message").inner_text() == "reworded via pencil button"

    # Section 2: Dismiss by clicking outside
    page.locator(".commit-row").nth(0).click(button="right")
    page.wait_for_selector("#context-menu:not(.hidden)")
    page.mouse.click(5, 5)
    page.wait_for_selector("#context-menu.hidden", state="attached")

    # Section 3: Dismiss by Escape
    page.locator(".commit-row").nth(0).click(button="right")
    page.wait_for_selector("#context-menu:not(.hidden)")
    page.keyboard.press("Escape")
    page.wait_for_selector("#context-menu.hidden", state="attached")

    # Section 4: Create branch via context menu
    page.locator(".commit-row").nth(1).click(button="right")
    page.wait_for_selector("#context-menu:not(.hidden)")
    page.locator("#ctx-create-branch").click()
    page.locator("#prompt-modal-input").fill("test-ctx-branch")
    with page.expect_response("**/api/branch"):
        page.locator("#prompt-modal-ok").click()
    assert page.locator(".commit-row").nth(1).locator("[data-branch='test-ctx-branch']").count() == 1

    # Section 5: Delete branch via context menu on badge
    page.locator(".commit-row").nth(1).locator("[data-branch='test-ctx-branch']").click(button="right")
    page.wait_for_selector("#context-menu:not(.hidden)")
    assert page.locator("#ctx-delete-branch").is_visible()
    page.locator("#ctx-delete-branch").click()
    with page.expect_response("**/api/branch"):
        page.locator("#prompt-modal-ok").click()
    page.wait_for_function("!document.querySelector('[data-branch=\"test-ctx-branch\"]')")

    # Section 6: The Undo Stack has no context menu. It highlights the current
    # position, navigates with the undo/redo arrows, and resets to a commit by double-click.
    head_before = page.locator(".commit-row").nth(0).get_attribute("data-commit-hash")

    # The current position (HEAD) is highlighted at the top of the stack.
    assert page.locator(".undo-stack-row.is-head").count() == 1
    assert "is-head" in page.locator(".undo-stack-row").nth(0).get_attribute("class")

    # Right-clicking the stack never opens a context menu.
    page.locator(".undo-stack-row").nth(1).click(button="right")
    page.wait_for_timeout(300)
    assert page.locator("#context-menu").evaluate("el => el.classList.contains('hidden')")

    # The undo arrow moves the position down to the commit below HEAD; redo moves it back.
    with page.expect_response("**/api/reset"):
        page.locator("#btn-undo").click()
    page.wait_for_function(f"document.querySelector('.commit-row').getAttribute('data-commit-hash') !== '{head_before}'")
    with page.expect_response("**/api/reset"):
        page.locator("#btn-redo").click()
    page.wait_for_function(f"document.querySelector('.commit-row').getAttribute('data-commit-hash') === '{head_before}'")

    # Double-clicking a non-HEAD row resets the branch to that commit.
    with page.expect_response("**/api/reset"):
        page.locator(".undo-stack-row").nth(1).dblclick()
    page.wait_for_function(f"document.querySelector('.commit-row').getAttribute('data-commit-hash') !== '{head_before}'")

    # Section 7: Dirty tree blocks context menu
    (context_menu_server["repo"] / "README.md").write_text("modified")
    page.locator("#btn-refresh").click()
    page.wait_for_selector("#btn-stash", state="visible")
    page.locator(".commit-row").nth(0).click(button="right")
    page.wait_for_timeout(300)
    assert page.locator("#context-menu").evaluate("el => el.classList.contains('hidden')")


@pytest.fixture
def copy_buttons_server(tmp_path_factory):
    yield from _setup_ui_server(tmp_path_factory, "ui-copy")


@pytest.mark.release
def test_copy_buttons(page, copy_buttons_server):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    page.goto(copy_buttons_server["url"])
    page.wait_for_selector(".commit-row")

    def clipboard_text():
        return page.evaluate("navigator.clipboard.readText()")

    # Section 1: Copy commit hash from a commit row
    row = page.locator(".commit-row").nth(1)
    full_hash = row.get_attribute("data-commit-hash")
    row.hover()
    row.locator("button[title='Copy commit hash']").click()
    page.wait_for_function(f"navigator.clipboard.readText().then(t => t === '{full_hash}')")
    assert clipboard_text() == full_hash

    # Section 2: Copy commit message from the same row
    message = row.locator(".message").inner_text()
    row.locator("button[title='Copy commit message']").click()
    page.wait_for_function(f"navigator.clipboard.readText().then(t => t === {json.dumps(message)})")
    assert clipboard_text() == message

    # Section 3: Copy commit hash from the Undo Stack
    # A fresh clone's branch reflog has only one entry ("clone: from ..."),
    # so commit once to give the Undo Stack a second row to select.
    (copy_buttons_server["repo"] / "extra.txt").write_text("extra")
    subprocess.run(["git", "add", "extra.txt"], cwd=str(copy_buttons_server["repo"]),
                    capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "extra commit"], cwd=str(copy_buttons_server["repo"]),
                    capture_output=True, check=True)
    page.locator("#btn-refresh").click()
    page.wait_for_selector(".undo-stack-row.is-head")

    undo_row = page.locator(".undo-stack-row").nth(1)
    undo_hash = undo_row.locator(".undo-stack-hash").inner_text()
    undo_row.hover()
    undo_row.locator("button[title='Copy commit hash']").click()
    page.wait_for_function(f"navigator.clipboard.readText().then(t => t.startsWith('{undo_hash}'))")
    assert clipboard_text().startswith(undo_hash)

    # Section 4: Copy buttons are disabled on the staged row
    (copy_buttons_server["repo"] / "README.md").write_text("modified")
    subprocess.run(["git", "add", "README.md"], cwd=str(copy_buttons_server["repo"]),
                    capture_output=True, check=True)
    page.locator("#btn-refresh").click()
    page.wait_for_selector(".badge-staged")
    staged_row = page.locator(".commit-row").filter(has=page.locator(".badge-staged"))
    assert staged_row.locator("button[title='Copy commit hash']").is_disabled()
    assert staged_row.locator("button[title='Copy commit message']").is_disabled()


@pytest.fixture
def single_roundtrip_server(tmp_path_factory):
    yield from _setup_ui_server(tmp_path_factory, "ui-roundtrip")


@pytest.mark.release
def test_mutation_single_round_trip(page, single_roundtrip_server):
    """A mutation bundles the selected commit's diff into its state response, so
    it costs exactly one /api/* request — no follow-up GET /api/show. The fixup
    below selects the merge result (not HEAD), exercising the general case, not
    just a selection that resolves to HEAD."""
    page.goto(single_roundtrip_server["url"])
    page.wait_for_selector(".commit-row")
    # A plain read does not bundle a diff, so the initial load fetches it via its
    # own /api/show. Wait for that to render before counting requests, so the
    # listener below measures only the mutation's round-trips.
    page.wait_for_selector("#diff-pane:not(.hidden)")
    # The selector resolves on a MutationObserver notification, which races the
    # initial /api/show response event over the CDP socket; wait for networkidle
    # so that event is flushed before the counting listener attaches below.
    page.wait_for_load_state("networkidle")
    count_before = page.locator(".commit-row").count()

    api_urls = []
    page.on("response", lambda r: "/api/" in r.url and api_urls.append(r.url))

    # Fixup a commit onto its parent — one mutation.
    row = page.locator(".commit-row").nth(1)
    row.hover()
    with page.expect_response("**/api/rebase/**"):
        row.locator("button[title='Fixup']").click()
    page.wait_for_function(f"document.querySelectorAll('.commit-row').length === {count_before - 1}")
    page.wait_for_selector("#spinner", state="hidden")

    assert len(api_urls) == 1, f"expected a single /api/* request, got {api_urls}"
    assert "/api/rebase/" in api_urls[0]

    # Reset hidden on HEAD, visible on non-HEAD
    page.locator(".commit-row").nth(0).click(button="right")
    page.wait_for_selector("#context-menu:not(.hidden)")
    assert page.locator("#ctx-reset").is_hidden()
    page.keyboard.press("Escape")
    page.wait_for_selector("#context-menu.hidden", state="attached")

    page.locator(".commit-row").nth(1).click(button="right")
    page.wait_for_selector("#context-menu:not(.hidden)")
    assert page.locator("#ctx-reset").is_visible()
    page.keyboard.press("Escape")
    page.wait_for_selector("#context-menu.hidden", state="attached")
