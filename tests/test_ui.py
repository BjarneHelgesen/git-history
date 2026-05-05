"""
UI regression tests using Playwright against a live Flask server.

Requires: pip install pytest-playwright && playwright install chromium
Run with: python -m pytest tests/test_ui.py -v
"""
import shutil
import sys
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from git_history import create_app
from make_test_repo import COMMITS, create_lib_repo, init_repo, make_commit
from conftest import _commit_raw, _build_conflict_repo

TOKEN = "test-ui-token-abcdefgh12345678"

# Step N/5 commits created in scrambled order so they display as 3,5,2,4,1.
_DRAG_COMMITS = [
    ("Step 1/5: Create homepage",   "bob",   [("drag/home.py",     "def home(): return 'home'\n")],     None),
    ("Step 4/5: Add contact page",  "carol", [("drag/contact.py",  "def contact(): return 'contact'\n")], None),
    ("Step 2/5: Add about page",    "alice", [("drag/about.py",    "def about(): return 'about'\n")],   None),
    ("Step 5/5: Add settings page", "bob",   [("drag/settings.py", "def settings(): return 'settings'\n")], None),
    ("Step 3/5: Add search page",   "carol", [("drag/search.py",   "def search(): return 'search'\n")],  None),
]


@pytest.fixture(scope="session")
def _template_repo(tmp_path_factory):
    template_dir = tmp_path_factory.mktemp("ui-template")
    lib = template_dir / "lib"
    lib_hash1, lib_hash2 = create_lib_repo(lib)
    sub = {"url": str(lib), "hash1": lib_hash1, "hash2": lib_hash2}
    repo = template_dir / "repo"
    repo.mkdir()
    init_repo(repo)
    for i, (msg, author_key, files, tag) in enumerate(COMMITS):
        make_commit(repo, i, msg, author_key, files, tag, sub=sub)
    return repo


@pytest.fixture
def live_server(tmp_path_factory, _template_repo):
    work = tmp_path_factory.mktemp("ui-work")
    repo = work / "repo"
    shutil.copytree(str(_template_repo), str(repo))
    app = create_app(str(repo), TOKEN)
    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{port}/?t={TOKEN}", "repo": repo}
    server.shutdown()


@pytest.fixture
def drag_server(tmp_path_factory):
    work = tmp_path_factory.mktemp("ui-drag")
    repo = work / "repo"
    repo.mkdir()
    init_repo(repo)
    for i, (msg, author_key, files, tag) in enumerate(_DRAG_COMMITS):
        make_commit(repo, i, msg, author_key, files, tag, sub=None)
    app = create_app(str(repo), TOKEN)
    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{port}/?t={TOKEN}", "repo": repo}
    server.shutdown()


@pytest.fixture
def conflict_server(tmp_path_factory):
    work = tmp_path_factory.mktemp("ui-conflict")
    repo = _build_conflict_repo(work)
    app = create_app(str(repo), TOKEN)
    server = make_server("127.0.0.1", 0, app)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{port}/?t={TOKEN}", "repo": repo}
    server.shutdown()


def _row(page, message):
    return page.locator(f'.commit-row[data-message="{message}"]')


def drag_row(page, source_locator, target_locator, position="above"):
    """Simulate a mousedown/mousemove/mouseup drag using the row's drag handle."""
    handle = source_locator.locator(".drag-handle")
    src = handle.bounding_box()
    sx = src["x"] + src["width"] / 2
    sy = src["y"] + src["height"] / 2
    page.mouse.move(sx, sy)
    page.mouse.down()
    page.mouse.move(sx, sy + 3)  # small move to trigger onDragMove
    tgt = target_locator.bounding_box()
    if position == "above":
        dy = tgt["y"] + 2
    else:
        dy = tgt["y"] + tgt["height"] - 2
    page.mouse.move(tgt["x"] + tgt["width"] / 2, dy)
    page.mouse.up()


def drag_row_and_wait(page, source_locator, target_locator, position="above"):
    with page.expect_response("**/api/rebase", timeout=10000):
        drag_row(page, source_locator, target_locator, position)


def test_page_loads(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    assert page.locator(".commit-row").count() == 24
    assert page.locator(".branch-history-row").count() > 0


def test_selection_and_action_bar(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    assert not page.locator("#btn-squash").is_visible()
    page.locator(".commit-row").nth(0).click()
    assert not page.locator("#btn-squash").is_visible()
    page.locator(".commit-row").nth(1).click(modifiers=["Shift"])
    assert page.locator("#btn-squash").is_visible()


def test_inline_reword(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    page.locator(".commit-row").nth(0).locator(".message").dblclick()
    ta = page.locator(".reword-input")
    ta.wait_for()
    ta.fill("reworded message")
    ta.press("Control+Enter")
    page.wait_for_function("!document.querySelector('.reword-input')")
    assert page.locator(".commit-row").nth(0).locator(".message").inner_text() == "reworded message"


def test_squash(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    page.locator(".commit-row").nth(0).click()
    page.locator(".commit-row").nth(1).click(modifiers=["Shift"])
    page.locator("#btn-squash").click()
    page.wait_for_function("document.querySelectorAll('.commit-row').length === 23")
    assert page.locator(".commit-row").count() == 23


def test_stash_button_when_dirty(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    assert not page.locator("#btn-stash").is_visible()
    (live_server["repo"] / "README.md").write_text("modified")
    page.locator("#btn-refresh").click()
    page.wait_for_selector("#btn-stash", state="visible")
    assert page.locator("#btn-stash").is_visible()


def test_stash_pop(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    (live_server["repo"] / "README.md").write_text("modified")
    page.locator("#btn-refresh").click()
    page.wait_for_selector("#btn-stash", state="visible")
    page.locator("#btn-stash").click()
    page.wait_for_selector("#btn-stash-pop", state="visible")
    page.locator("#btn-stash-pop").click()
    page.wait_for_selector("#btn-stash", state="visible")


def test_fixup_button(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    row = page.locator(".commit-row").nth(1)
    row.hover()
    row.locator("button[title='Fixup']").click()
    page.wait_for_function("document.querySelectorAll('.commit-row').length === 23")
    assert page.locator(".commit-row").count() == 23


def test_fixup_button_disabled_on_oldest(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    oldest = page.locator(".commit-row").last
    oldest.hover()
    assert oldest.locator("button[title='Fixup']").is_disabled()


def test_branch_history_group_consecutive_rebases(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    # Two rewrites produce two consecutive "rebase" entries in the branch history.
    for msg in ["first reword", "second reword"]:
        page.locator(".commit-row").nth(0).locator(".message").dblclick()
        ta = page.locator(".reword-input")
        ta.wait_for()
        ta.fill(msg)
        ta.press("Control+Enter")
        page.wait_for_function("!document.querySelector('.reword-input')")
    checkbox = page.locator("#group-consec-rebases")
    checkbox.check()
    count_filtered = page.locator(".branch-history-row").count()
    checkbox.uncheck()
    count_unfiltered = page.locator(".branch-history-row").count()
    assert count_filtered < count_unfiltered


# ---------------------------------------------------------------------------
# Section 2: Drag-and-drop
# ---------------------------------------------------------------------------

def test_drag_changes_order(page, drag_server):
    page.goto(drag_server["url"])
    page.wait_for_selector(".commit-row")
    original_hash = page.locator(".commit-row").nth(0).get_attribute("data-commit-hash")
    drag_row_and_wait(page,
        page.locator(".commit-row").nth(0),
        page.locator(".commit-row").nth(2),
        "below")
    assert page.locator(".commit-row").nth(0).get_attribute("data-commit-hash") != original_hash


def test_drag_reorder_step_commits(page, drag_server):
    # Initial display order (newest first): Step 3, Step 5, Step 2, Step 4, Step 1
    # Goal: Step 5, Step 4, Step 3, Step 2, Step 1
    page.goto(drag_server["url"])
    page.wait_for_selector(".commit-row")
    drag_row_and_wait(page,
        _row(page, "Step 5/5: Add settings page"),
        _row(page, "Step 3/5: Add search page"),
        "above")
    drag_row_and_wait(page,
        _row(page, "Step 4/5: Add contact page"),
        _row(page, "Step 3/5: Add search page"),
        "above")
    messages = [r.get_attribute("data-message")
                for r in page.locator(".commit-row").all()]
    assert messages == [
        "Step 5/5: Add settings page",
        "Step 4/5: Add contact page",
        "Step 3/5: Add search page",
        "Step 2/5: Add about page",
        "Step 1/5: Create homepage",
    ]


def test_drag_group_of_commits(page, drag_server):
    # Select rows 0+1 (Step 3, Step 5), drag group below row 3 (Step 4).
    page.goto(drag_server["url"])
    page.wait_for_selector(".commit-row")
    msg0 = page.locator(".commit-row").nth(0).get_attribute("data-message")
    msg1 = page.locator(".commit-row").nth(1).get_attribute("data-message")
    page.locator(".commit-row").nth(0).click()
    page.locator(".commit-row").nth(1).click(modifiers=["Shift"])
    drag_row_and_wait(page,
        page.locator(".commit-row").nth(0),
        page.locator(".commit-row").nth(3),
        "below")
    messages = [r.get_attribute("data-message")
                for r in page.locator(".commit-row").all()]
    assert messages.index(msg0) > 1
    assert messages.index(msg1) > 1


def test_drag_to_same_position_is_noop(page, drag_server):
    page.goto(drag_server["url"])
    page.wait_for_selector(".commit-row")
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


def test_drop_indicator_visible_during_drag(page, drag_server):
    page.goto(drag_server["url"])
    page.wait_for_selector(".commit-row")
    handle = page.locator(".commit-row").nth(0).locator(".drag-handle")
    src = handle.bounding_box()
    page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
    page.mouse.down()
    page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2 + 3)
    tgt = page.locator(".commit-row").nth(2).bounding_box()
    page.mouse.move(tgt["x"] + tgt["width"] / 2, tgt["y"] + 2)
    assert page.locator(".drop-indicator").count() > 0
    page.mouse.up()


def test_dragged_row_opacity(page, drag_server):
    page.goto(drag_server["url"])
    page.wait_for_selector(".commit-row")
    handle = page.locator(".commit-row").nth(0).locator(".drag-handle")
    src = handle.bounding_box()
    page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2)
    page.mouse.down()
    page.mouse.move(src["x"] + src["width"] / 2, src["y"] + src["height"] / 2 + 3)
    page.wait_for_selector("#commits-list[data-dragging]")
    opacity = page.evaluate(
        "document.querySelector('.commit-row').style.opacity"
    )
    page.mouse.up()
    assert opacity == "0.4"


# ---------------------------------------------------------------------------
# Section 3: Conflict modal
# ---------------------------------------------------------------------------

def test_conflict_modal_appears(page, conflict_server):
    page.goto(conflict_server["url"])
    page.wait_for_selector(".commit-row")
    # Swap version A above version B — version B's patch can't apply to initial → conflict.
    drag_row(page, _row(page, "version A"), _row(page, "version B"), position="above")
    page.wait_for_selector("#conflict-modal:not(.hidden)")
    assert page.locator("#conflict-files li").count() > 0


def test_conflict_abort(page, conflict_server):
    page.goto(conflict_server["url"])
    page.wait_for_selector(".commit-row")
    initial_count = page.locator(".commit-row").count()
    drag_row(page, _row(page, "version A"), _row(page, "version B"), position="above")
    page.wait_for_selector("#conflict-modal:not(.hidden)")
    page.locator("#btn-abort").click()
    page.wait_for_selector("#conflict-modal", state="hidden")
    assert page.locator(".commit-row").count() == initial_count


# ---------------------------------------------------------------------------
# Section 4: Selection after operations
# ---------------------------------------------------------------------------

def _selected_idx(page):
    return page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll('.commit-row'));
        const sel = document.querySelector('.commit-row.selected');
        return sel ? rows.indexOf(sel) : -1;
    }""")


def test_fixup_preserves_selection(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    page.locator(".commit-row").nth(2).hover()
    page.locator(".commit-row").nth(2).locator("button[title='Fixup']").click()
    page.wait_for_function("document.querySelectorAll('.commit-row').length === 23")
    assert _selected_idx(page) == 1  # idx - 1 = 2 - 1


def test_squash_preserves_selection(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    page.locator(".commit-row").nth(2).click()
    page.locator(".commit-row").nth(3).click(modifiers=["Shift"])
    with page.expect_response("**/api/rebase"):
        page.locator("#btn-squash").click()
    page.wait_for_function("document.querySelectorAll('.commit-row').length === 23")
    assert _selected_idx(page) == 2  # first of the selected range


def test_reword_preserves_selection(page, live_server):
    page.goto(live_server["url"])
    page.wait_for_selector(".commit-row")
    page.locator(".commit-row").nth(3).locator(".message").dblclick()
    ta = page.locator(".reword-input")
    ta.wait_for()
    ta.fill("reworded message")
    with page.expect_response("**/api/rebase"):
        ta.press("Control+Enter")
    page.wait_for_function("!document.querySelector('.reword-input')")
    assert _selected_idx(page) == 3


def test_drag_preserves_selection(page, drag_server):
    page.goto(drag_server["url"])
    page.wait_for_selector(".commit-row")
    drag_row_and_wait(page,
        page.locator(".commit-row").nth(0),
        page.locator(".commit-row").nth(3),
        "below")
    assert _selected_idx(page) == 3
