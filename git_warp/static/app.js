/* git-warp front-end */
(function () {
  "use strict";

  // ---- Token ----
  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get("t");
  const dark = params.get("dark") === "1";
  const light = params.get("light") === "1";
  if (urlToken) {
    sessionStorage.setItem("git_warp_token", urlToken);
    let query = "";
    if (dark) query = "?dark=1";
    else if (light) query = "?light=1";
    history.replaceState(null, "", "/" + query);
  }
  const TOKEN = sessionStorage.getItem("git_warp_token") || "";
  const HEADERS = {"X-Token": TOKEN};
  const prefersDark = matchMedia("(prefers-color-scheme: dark)").matches;
  if (dark || (prefersDark && !light)) document.body.classList.add("dark");

  // ---- State ----
  const STAGED_HASH = "(Staged)";
  let state = null;         // latest server state
  let selected = new Set(); // set of selected hashes
  let anchor = null;        // shift-click anchor hash
  let busy = false;
  let diffHash = null;      // hash currently shown in diffFiles panel
  let selectedFilesInDiff = new Set();
  let splitAnchor = null; // for shift-click range selection
  let headIdx = -1;

  // ---- DOM refs ----
  const $banner      = document.getElementById("banner");
  const $spinner     = document.getElementById("spinner");
  const $btnUndo     = document.getElementById("btn-undo");
  const $btnRedo     = document.getElementById("btn-redo");
  const $btnStash    = document.getElementById("btn-stash");
  const $btnStashPop = document.getElementById("btn-stash-pop");
  const $btnRefresh  = document.getElementById("btn-refresh");
  const $btnSquash   = document.getElementById("btn-squash");
  const $btnQuit     = document.getElementById("btn-quit");
  const $contextMenu = document.getElementById("context-menu");
  const $ctxCreateBranch  = document.getElementById("ctx-create-branch");
  const $ctxReset         = document.getElementById("ctx-reset");
  const $ctxDeleteBranch  = document.getElementById("ctx-delete-branch");
  const $commitsList = document.getElementById("commits-list");
  const $undoStackList  = document.getElementById("undo-stack-list");
  const $undoStackBranch = document.getElementById("undo-stack-branch");
  const $undoStackExpiry = document.getElementById("undo-stack-expiry");
  const $conflictModal = document.getElementById("conflict-modal");
  const $conflictModalTitle = document.getElementById("conflict-modal-title");
  const $conflictModalText = document.getElementById("conflict-modal-text");
  const $conflictFiles = document.getElementById("conflict-files");
  const $btnAbort    = document.getElementById("btn-abort");
  const $btnContinue = document.getElementById("btn-continue");
  const $commitsHelpModal = document.getElementById("commits-help-modal");
  const $undoStackHelpModal = document.getElementById("undo-stack-help-modal");
  const $diffPane      = document.getElementById("diff-pane");
  const $diffResize    = document.getElementById("diff-resize");
  const $diffFiles     = document.getElementById("diff-files");
  const $diffContent   = document.getElementById("diff-content");
  const $btnSplit      = document.getElementById("btn-split");
  const $branchSelect  = document.getElementById("branch-select");
  const $promptModal       = document.getElementById("prompt-modal");
  const $promptModalMsg    = document.getElementById("prompt-modal-message");
  const $promptModalInput  = document.getElementById("prompt-modal-input");
  const $promptModalCancel = document.getElementById("prompt-modal-cancel");
  const $promptModalOk     = document.getElementById("prompt-modal-ok");

  // showModal replaces native confirm()/prompt() to avoid the browser origin prefix.
  // withInput=false → resolves true/false; withInput=true → resolves the text or null.
  function showModal(message, withInput) {
    return new Promise(resolve => {
      $promptModalMsg.textContent = message;
      $promptModalInput.value = "";
      $promptModalInput.classList.toggle("hidden", !withInput);
      $promptModal.classList.remove("hidden");
      if (withInput) $promptModalInput.focus();
      function cleanup(result) {
        $promptModal.classList.add("hidden");
        $promptModalCancel.removeEventListener("click", onCancel);
        $promptModalOk.removeEventListener("click", onOk);
        document.removeEventListener("keydown", onKey);
        resolve(result);
      }
      function onCancel() { cleanup(withInput ? null : false); }
      function onOk() { cleanup(withInput ? ($promptModalInput.value || null) : true); }
      function onKey(e) { if (e.key === "Enter") onOk(); else if (e.key === "Escape") onCancel(); }
      $promptModalCancel.addEventListener("click", onCancel);
      $promptModalOk.addEventListener("click", onOk);
      document.addEventListener("keydown", onKey);
    });
  }

  function canMutate() { return state && !state.dirty && !state.rebase_in_progress && !!state.branch; }

  // ---- API helpers ----
  async function api(method, url, body) {
    const opts = {method, headers: {...HEADERS}};
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (resp.status === 403) {
      sessionStorage.removeItem("git_warp_token");
      throw new Error("This git warp tab has expired. Switch tab or restart git warp");
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  async function withSpinner(fn) {
    if (busy) return;
    showSpinner();
    try { return await fn(); }
    catch (err) { showBanner("Request failed: " + err.message, "error"); }
    finally { hideSpinner(); }
  }

  async function spinnerCall(method, url, body, selectIdx) {
    // Tell the server which commit the UI will select so it can bundle that
    // commit's diff into the response (single round-trip).
    if (selectIdx != null) body = Object.assign({}, body, {select_index: selectIdx});
    const d = await withSpinner(() => api(method, url, body));
    if (d) handleResponse(d, selectIdx);
  }

  // ---- Spinner ----
  function showSpinner() { busy = true; $spinner.classList.remove("hidden"); $branchSelect.disabled = true; }
  function hideSpinner() { busy = false; $spinner.classList.add("hidden"); $branchSelect.disabled = !!(state && (state.dirty || state.rebase_in_progress)); }

  // ---- Banner ----
  function showBanner(msg, type) {
    $banner.textContent = msg;
    $banner.className = type; // "error" or "warning"
  }
  function clearBanner() { $banner.className = ""; $banner.textContent = ""; }

  // ---- Render ----
  function render() {
    if (!state) return;
    $undoStackBranch.textContent = state.branch;
    $undoStackExpiry.textContent = state.reflog_expiry;

    // Branch dropdown
    $branchSelect.innerHTML = "";
    if (!state.branch) {
      const opt = document.createElement("option");
      opt.value = ""; opt.disabled = true; opt.selected = true;
      opt.textContent = "(detached)";
      $branchSelect.appendChild(opt);
    }
    (state.branches || []).forEach(function (b) {
      const opt = document.createElement("option");
      opt.value = b; opt.selected = b === state.branch; opt.textContent = b;
      $branchSelect.appendChild(opt);
    });
    $branchSelect.disabled = state.dirty || state.rebase_in_progress;

    // Dirty / detached / rebase / stash buttons
    if (state.dirty && !state.rebase_in_progress) {
      showBanner("Working tree has uncommitted changes.", "warning");
      $btnStash.classList.remove("hidden");
    } else if (!state.branch && !state.rebase_in_progress) {
      showBanner("HEAD is detached. Select a branch from the dropdown.", "warning");
      $btnStash.classList.add("hidden");
    } else {
      if ($banner.className === "warning") clearBanner();
      $btnStash.classList.add("hidden");
    }
    if (!state.has_stash || state.dirty) $btnStashPop.classList.add("hidden");
    else $btnStashPop.classList.remove("hidden");

    // Conflict / paused-rebase modal
    if (state.rebase_in_progress) {
      if (state.conflict_files.length > 0) {
        $conflictModalTitle.textContent = "Merge conflict";
        $conflictModalText.textContent = "Resolve in your editor, then:";
        $conflictFiles.innerHTML = "";
        state.conflict_files.forEach(function (f) {
          const li = document.createElement("li");
          li.textContent = f;
          $conflictFiles.appendChild(li);
        });
        $conflictFiles.classList.remove("hidden");
      } else {
        $conflictModalTitle.textContent = "Rebase paused";
        $conflictModalText.textContent = "The rebase stopped without a conflict.";
        $conflictFiles.classList.add("hidden");
      }
      $conflictModal.classList.remove("hidden");
    } else {
      $conflictModal.classList.add("hidden");
    }

    const headHash = (state.commits.find(function (c) { return c.is_head; }) || {}).commit_hash || "";
    const entries = state.undo_stack || [];
    headIdx = entries.findIndex(function (e) { return e.commit_hash === headHash; });
    renderCommits();
    renderUndoStack(entries);
    updateActionBar();
  }

  function createCopyButton(title, getText) {
    const btn = document.createElement("button");
    btn.textContent = "📜️";
    btn.title = title;
    btn.className = "row-icon-btn";
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      hideContextMenu();
      // Clipboard write failures have no user-visible recovery, so the promise is intentionally left unhandled.
      navigator.clipboard.writeText(getText());
    });
    return btn;
  }

  function createCommitRow(commit, idx) {
    const row = document.createElement("div");
    row.className = "commit-row" + (selected.has(commit.commit_hash) ? " selected" : "") + (commit.is_head ? " is-head" : "");
    row.dataset.commitHash = commit.commit_hash;
    row.dataset.idx = idx;

    // Drag handle
    const handle = document.createElement("span");
    handle.className = "drag-handle";
    handle.textContent = "⠿";
    handle.title = "Drag to reorder";
    if (commit.commit_hash !== STAGED_HASH) {
      handle.draggable = true;
      handle.addEventListener("dragstart", onDragStart);
    } else {
      handle.style.visibility = "hidden";
    }
    row.appendChild(handle);

    // Cloud indicator for pushed commits
    const cloud = document.createElement("span");
    cloud.className = "pushed-indicator";
    cloud.textContent = commit.pushed ? "☁" : "";
    row.appendChild(cloud);

    // Short hash section (with copy button on top)
    const hashSection = document.createElement("span");
    hashSection.className = "hash-section";

    const sh = document.createElement("span");
    sh.className = "short-hash";
    sh.textContent = commit.short_hash;
    hashSection.appendChild(sh);

    const btnCopyHash = createCopyButton("Copy commit hash", function () { return commit.commit_hash; });
    btnCopyHash.disabled = commit.commit_hash === STAGED_HASH;
    hashSection.appendChild(btnCopyHash);

    row.appendChild(hashSection);

    // Badges
    const badges = document.createElement("span");
    badges.className = "badges";
    if (commit.commit_hash === STAGED_HASH) {
      const s = document.createElement("span");
      s.className = "badge-staged";
      s.textContent = "Staged";
      badges.appendChild(s);
    }
    commit.branches.forEach(function (b) {
      const s = document.createElement("span");
      let badgeClass;
      if (b === state.branch) badgeClass = "badge-branch-current";
      else if (b === state.upstream) badgeClass = "badge-branch-tracking";
      else if (state.branches.indexOf(b) !== -1) badgeClass = "badge-branch";
      else badgeClass = "badge-branch-remote";
      s.className = badgeClass;
      s.textContent = b;
      s.dataset.branch = b;
      badges.appendChild(s);
    });
    commit.tags.forEach(function (t) {
      const s = document.createElement("span");
      s.className = "badge-tag";
      s.textContent = t;
      badges.appendChild(s);
    });
    row.appendChild(badges);

    // Message section (with edit button on top)
    const msgSection = document.createElement("span");
    msgSection.className = "message-section";

    const btnEdit = document.createElement("button");
    btnEdit.textContent = "✏️";
    btnEdit.title = "Edit commit message";
    btnEdit.className = "row-icon-btn row-icon-btn-2nd";
    btnEdit.disabled = !canMutate() || commit.commit_hash === STAGED_HASH;
    btnEdit.addEventListener("click", function (e) {
      e.stopPropagation();
      hideContextMenu();
      selectAndRefresh(commit.commit_hash);
      startReword(row, commit);
    });
    msgSection.appendChild(btnEdit);

    const btnCopyMsg = createCopyButton("Copy commit message", function () { return commit.message; });
    btnCopyMsg.disabled = commit.commit_hash === STAGED_HASH;
    msgSection.appendChild(btnCopyMsg);

    const msg = document.createElement("span");
    msg.className = "message";
    msg.textContent = commit.message;
    msgSection.appendChild(msg);

    row.appendChild(msgSection);

    // Author
    const author = document.createElement("span");
    author.className = "author";
    author.textContent = commit.author;
    row.appendChild(author);

    // Date section (with fixup button on top)
    const dateSection = document.createElement("span");
    dateSection.className = "date-section";

    const btnFixup = document.createElement("button");
    btnFixup.textContent = "⤵️";
    btnFixup.title = "Fixup";
    btnFixup.className = "row-icon-btn";
    btnFixup.disabled = !canMutate() || commit.commit_hash === STAGED_HASH || idx === state.commits.length - 1;
    btnFixup.addEventListener("click", function (e) {
      e.stopPropagation();
      hideContextMenu();
      doRebase("fixup", {commit_hashes: [commit.commit_hash]}, idx);
    });
    dateSection.appendChild(btnFixup);

    const dt = document.createElement("span");
    dt.className = "date";
    dt.textContent = commit.date ? commit.date.slice(0, 16) : "";
    dateSection.appendChild(dt);

    row.appendChild(dateSection);

    // Click to select
    row.addEventListener("click", function (e) {
      if (e.target.closest(".row-icon-btn, .drag-handle")) return;
      onRowClick(commit.commit_hash, idx, e);
    });

    row.addEventListener("contextmenu", function (e) {
      if (e.target.closest(".row-icon-btn, .drag-handle, [data-branch]")) return;
      e.preventDefault();
      if (commit.commit_hash === STAGED_HASH) return;
      selectAndRefresh(commit.commit_hash);
      if (!canMutate()) return;
      e.stopPropagation();
      showContextMenu(e.clientX, e.clientY, commitMenuItems(commit));
    });

    // Branch badge right-click → commit actions + delete branch (local, non-current branches only)
    badges.querySelectorAll("[data-branch]").forEach(function (badge) {
      const branchName = badge.dataset.branch;
      badge.addEventListener("contextmenu", function (e) {
        e.preventDefault();
        e.stopPropagation();
        selectAndRefresh(commit.commit_hash);
        if (!canMutate()) return;
        const items = commitMenuItems(commit);
        const deletable = branchName !== state.branch && state.branches.indexOf(branchName) !== -1;
        items.deleteBranch = deletable ? function () {
          hideContextMenu();
          showModal("Delete branch \"" + branchName + "\"?", false).then(function (ok) {
            if (!ok) return;
            spinnerCall("DELETE", "/api/branch", {branch_name: branchName});
          });
        } : null;
        showContextMenu(e.clientX, e.clientY, items);
      });
    });

    return row;
  }

  function renderCommits() {
    $commitsList.innerHTML = "";
    state.commits.forEach(function (c, idx) {
      $commitsList.appendChild(createCommitRow(c, idx));
    });
  }

  function renderUndoStack(entries) {
    $undoStackList.innerHTML = "";
    $btnUndo.disabled = !canMutate() || headIdx === -1 || headIdx === entries.length - 1;
    $btnRedo.disabled = !canMutate() || headIdx <= 0;
    entries.forEach(function (entry, idx) {
      const isHead = idx === headIdx;
      const row = document.createElement("div");
      row.className = "undo-stack-row" + (isHead ? " is-head" : "");

      const hashSection = document.createElement("span");
      hashSection.className = "hash-section";

      const hashSpan = document.createElement("span");
      hashSpan.className = "undo-stack-hash";
      hashSpan.textContent = entry.commit_hash.slice(0, 7);
      hashSection.appendChild(hashSpan);

      const btnCopyHash = createCopyButton("Copy commit hash", function () { return entry.commit_hash; });
      hashSection.appendChild(btnCopyHash);

      row.appendChild(hashSection);

      const labelSpan = document.createElement("span");
      labelSpan.className = "undo-stack-label";
      labelSpan.textContent = entry.label;
      row.appendChild(labelSpan);

      const tsSpan = document.createElement("span");
      tsSpan.className = "undo-stack-timestamp";
      tsSpan.textContent = entry.timestamp ? entry.timestamp.slice(0, 16) : "";
      row.appendChild(tsSpan);

      row.addEventListener("dblclick", function (e) {
        e.preventDefault();
        if (canMutate() && !isHead) doReset(entry.commit_hash);
      });
      $undoStackList.appendChild(row);
    });
  }

  // ---- Selection ----
  function setSingleSelection(hash) {
    selected = new Set([hash]);
    anchor = hash;
  }

  function selectAndRefresh(hash) {
    setSingleSelection(hash);
    applySelectionClasses();
    updateActionBar();
  }

  function applySelectionClasses() {
    document.querySelectorAll(".commit-row").forEach(function (r) {
      r.classList.toggle("selected", selected.has(r.dataset.commitHash));
    });
  }

  function onRowClick(hash, idx, e) {
    if (hash === STAGED_HASH) {
      setSingleSelection(STAGED_HASH);
    } else if (e.shiftKey && anchor !== null) {
      const anchorIdx = state.commits.findIndex(function (c) { return c.commit_hash === anchor; });
      if (anchorIdx === -1) { setSingleSelection(hash); }
      else {
        const firstIdx = Math.min(anchorIdx, idx);
        const lastIdx = Math.max(anchorIdx, idx);
        selected = new Set();
        for (let i = firstIdx; i <= lastIdx; i++) selected.add(state.commits[i].commit_hash);
      }
    } else {
      setSingleSelection(hash);
    }
    showDiff(hash);
    applySelectionClasses();
    updateActionBar();
  }

  function updateActionBar() {
    const showSquash = selected.size >= 2 && !selected.has(STAGED_HASH);
    $btnSquash.classList.toggle("hidden", !showSquash);
    if (showSquash) $btnSquash.disabled = !canMutate();
  }

  // ---- Context menu ----
  function hideContextMenu() { $contextMenu.classList.add("hidden"); }

  function createBranchAt(commitHash) {
    hideContextMenu();
    showModal("New branch name:", true).then(function (name) {
      if (!name) return;
      spinnerCall("POST", "/api/branch", {commit_hash: commitHash, branch_name: name});
    });
  }

  // createBranch/reset items shared by the commit-row and branch-badge menus.
  function commitMenuItems(commit) {
    return {
      createBranch: function () { createBranchAt(commit.commit_hash); },
      reset: commit.is_head ? null : function () { hideContextMenu(); doReset(commit.commit_hash); },
    };
  }

  function showContextMenu(x, y, items) {
    // items: {createBranch, reset, deleteBranch} — each is a callback or null
    $ctxCreateBranch.classList.toggle("hidden", !items.createBranch);
    $ctxReset.classList.toggle("hidden", !items.reset);
    $ctxDeleteBranch.classList.toggle("hidden", !items.deleteBranch);

    $ctxCreateBranch.onclick = items.createBranch || null;
    $ctxReset.onclick = items.reset || null;
    $ctxDeleteBranch.onclick = items.deleteBranch || null;

    $contextMenu.classList.remove("hidden");
    // Keep menu within viewport
    const menuW = $contextMenu.offsetWidth, menuH = $contextMenu.offsetHeight;
    const left = x + menuW > window.innerWidth ? window.innerWidth - menuW - 4 : x;
    const top = y + menuH > window.innerHeight ? window.innerHeight - menuH - 4 : y;
    $contextMenu.style.left = left + "px";
    $contextMenu.style.top = top + "px";
  }

  document.addEventListener("click", hideContextMenu);
  document.addEventListener("contextmenu", function (e) {
    if (!e.target.closest("#context-menu")) hideContextMenu();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hideContextMenu(); }, true);

  // ---- Reword ----
  function startReword(row, commit) {
    if (commit.commit_hash === STAGED_HASH || !canMutate() || busy) return;
    const msgEl = row.querySelector(".message");
    const ta = document.createElement("textarea");
    ta.className = "reword-input";
    ta.value = commit.message;
    ta.rows = Math.max(2, commit.message.split("\n").length);
    msgEl.replaceWith(ta);
    ta.focus();
    ta.select();

    function finish(save) {
      ta.removeEventListener("keydown", onKey);
      ta.removeEventListener("blur", onBlur);
      if (save && ta.value !== commit.message) {
        doRebase("reword", {commit_hashes: [commit.commit_hash], new_message: ta.value}, state.commits.findIndex(function (c) { return c.commit_hash === commit.commit_hash; }));
      } else {
        renderCommits();
      }
    }
    function onKey(e) {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) finish(true);
      if (e.key === "Escape") finish(false);
    }
    function onBlur() { if (document.hasFocus()) finish(true); }
    ta.addEventListener("keydown", onKey);
    ta.addEventListener("blur", onBlur);
  }

  // ---- Drag & drop (move) ----
  // Native HTML5 drag: the grabbed commit — and any co-selected commits — are
  // drawn as a ghost (a custom drag image) that follows the cursor, so the user
  // sees what he drags. A drop-indicator line shows where the block will land;
  // on drop we send the new order to the backend. The DOM is never reordered
  // during the drag, so Escape (handled by the browser) cancels it for free.
  function removeDragIndicator() {
    const el = $commitsList.querySelector(".drop-indicator");
    if (el) el.remove();
  }

  // Row whose upper half the cursor is over (insert the block before it); null → list end.
  function rowBefore(clientY) {
    for (const r of $commitsList.querySelectorAll(".commit-row")) {
      const rect = r.getBoundingClientRect();
      if (clientY < rect.top + rect.height / 2) return r;
    }
    return null;
  }

  function createDragGhost(row, selectedRows, clientX, clientY) {
    const rect = row.getBoundingClientRect();
    // Browser applies a transparency gradient from the drag anchor point to the edges of the drag image.
    // We add padding (especially right/top/bottom) to keep the content in a zone where the gradient
    // doesn't make it too transparent. Left padding is unnecessary since the drag handle is on the left.
    const ghostWidth = rect.width * 3; // 2x right padding
    const ghostHeight = rect.height * selectedRows.length * 4; // Padding on both top and bottom

    const ghost = document.createElement("div");
    ghost.className = "drag-ghost";
    ghost.style.cssText = "width:" + ghostWidth + "px; height:" + ghostHeight + "px; display:flex; align-items:center; justify-content:flex-start;";
    const content = document.createElement("div");
    content.style.width = rect.width + "px";
    selectedRows.forEach(function (r) { content.appendChild(r.cloneNode(true)); });
    ghost.appendChild(content);
    document.body.appendChild(ghost);

    return {
      element: ghost,
      offsetX: clientX - rect.left,
      offsetY: (ghostHeight - selectedRows.length * rect.height) / 2 + (selectedRows.indexOf(row) * rect.height) + (clientY - rect.top)
    };
  }

  function onDragStart(e) {
    const row = e.target.closest(".commit-row");
    const hash = row.dataset.commitHash;
    if (!canMutate() || busy || hash === STAGED_HASH) { e.preventDefault(); return; }
    if (!selected.has(hash)) { setSingleSelection(hash); applySelectionClasses(); updateActionBar(); }

    const selectedRows = Array.from($commitsList.querySelectorAll(".commit-row"))
      .filter(function (r) { return selected.has(r.dataset.commitHash); });

    const dragImage = createDragGhost(row, selectedRows, e.clientX, e.clientY);
    e.dataTransfer.setDragImage(dragImage.element, dragImage.offsetX, dragImage.offsetY);
    setTimeout(function () { dragImage.element.remove(); }, 0);

    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", hash); // Firefox needs a payload to start a drag
    $commitsList.dataset.dragging = "1";
    selectedRows.forEach(function (r) { r.classList.add("dragging"); });
  }

  function onDragOver(e) {
    if (!$commitsList.dataset.dragging) return;
    e.preventDefault();
    removeDragIndicator();
    const ind = document.createElement("div");
    ind.className = "drop-indicator";
    $commitsList.insertBefore(ind, rowBefore(e.clientY));
  }

  function onDrop(e) {
    if (!$commitsList.dataset.dragging) return;
    e.preventDefault();
    const hashes = Array.from($commitsList.querySelectorAll(".commit-row"))
      .map(function (r) { return r.dataset.commitHash; })
      .filter(function (h) { return h !== STAGED_HASH; });
    const ref = rowBefore(e.clientY);
    const insertIdx = ref ? hashes.indexOf(ref.dataset.commitHash) : hashes.length;
    const block = hashes.filter(function (h) { return selected.has(h); });
    const rest = hashes.filter(function (h) { return !selected.has(h); });
    // Insert the block where the cursor points, counting only the rows that stay put.
    let pos = 0;
    for (let i = 0; i < insertIdx; i++) if (!selected.has(hashes[i])) pos++;
    const order = rest.slice(0, pos).concat(block, rest.slice(pos));
    if (order.join() !== hashes.join())
      doRebase("move", {order: order}, selected.size === 1 ? order.indexOf(block[0]) : null);
  }

  function onDragEnd() {
    delete $commitsList.dataset.dragging;
    removeDragIndicator();
    document.querySelectorAll(".commit-row.dragging").forEach(function (r) { r.classList.remove("dragging"); });
  }

  $commitsList.addEventListener("dragover", onDragOver);
  $commitsList.addEventListener("drop", onDrop);
  $commitsList.addEventListener("dragend", onDragEnd);

  // ---- Diff resize ----
  $diffResize.addEventListener("mousedown", function (e) {
    e.preventDefault();
    const startY = e.clientY, startH = $diffPane.offsetHeight;
    function onMove(e) { $diffPane.style.height = (startH - (e.clientY - startY)) + "px"; }
    function onUp() { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  // ---- Diff pane ----
  async function showDiff(hash) {
    try {
      diffHash = hash;
      const data = await api("GET", "/api/show?commit_hash=" + encodeURIComponent(hash));
      if (diffHash !== hash) return; // a newer showDiff call may have run during the await
      renderDiff(data, hash);
    } catch (err) { showBanner("Failed to load diff: " + err.message, "error"); }
  }

  function renderDiff(data, hash) {
    diffHash = hash;
    selectedFilesInDiff.clear();
    splitAnchor = null;
    if (data.ok) {
      // Format commit message and metadata
      let headerLines = [];
      if (data.commit.message) {
        headerLines.push(data.commit.message);
        headerLines.push("");
      }
      if (data.commit.author) {
        headerLines.push("Author: " + data.commit.author);
      }
      if (data.commit.date) {
        headerLines.push("Date: " + data.commit.date);
      }
      if (headerLines.length > 1) {
        headerLines.push("");
      }

      const allLines = headerLines.concat((data.diff || "").split("\n"));
      $diffContent.innerHTML = allLines.map(function (line, idx) {
        const escaped = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        let cls = "";
        // Only apply diff styling to actual diff lines (after the header)
        if (idx >= headerLines.length) {
          if (/^\+(?!\+\+)/.test(line)) cls = "diff-add";
          else if (/^-(?!--)/.test(line)) cls = "diff-del";
          else if (/^@@/.test(line)) cls = "diff-hunk";
        }
        return '<span' + (cls ? ' class="' + cls + '"' : '') + '>' + escaped + '\n</span>';
      }).join("");
      $diffFiles.querySelectorAll("div").forEach(d => d.remove());
      const files = data.files || [];
      files.forEach(function (name) {
        const d = document.createElement("div");
        d.textContent = name;
        d.dataset.filename = name;
        d.addEventListener("click", onFileClick);
        $diffFiles.appendChild(d);
      });
      $btnSplit.classList.toggle("hidden", !canMutate() || hash === STAGED_HASH || files.length < 2 || selected.size !== 1);
      renderFileSelection();
      $diffPane.classList.remove("hidden");
    } else {
      showBanner(data.error || "Failed to load diff", "error");
    }
  }

  // ---- File selection and split ----
  function onFileClick(e) {
    e.preventDefault();
    const filename = e.currentTarget.dataset.filename;
    if (e.shiftKey && splitAnchor) {
      const divs = Array.from($diffFiles.querySelectorAll("div[data-filename]"));
      const names = divs.map(d => d.dataset.filename);
      const a = names.indexOf(splitAnchor), b = names.indexOf(filename);
      selectedFilesInDiff = new Set(names.slice(Math.min(a, b), Math.max(a, b) + 1));
    } else if (e.ctrlKey || e.metaKey) {
      selectedFilesInDiff[selectedFilesInDiff.has(filename) ? "delete" : "add"](filename);
      splitAnchor = filename;
    } else {
      selectedFilesInDiff = new Set([filename]);
      splitAnchor = filename;
    }
    renderFileSelection();
  }

  function renderFileSelection() {
    const fileDivs = $diffFiles.querySelectorAll("div[data-filename]");
    fileDivs.forEach(function (d) {
      d.classList.toggle("selected", selectedFilesInDiff.has(d.dataset.filename));
    });
    // Split needs a strict, non-empty subset of the files (matches the backend).
    $btnSplit.disabled = selectedFilesInDiff.size === 0 || selectedFilesInDiff.size === fileDivs.length;
  }

  // ---- API actions ----
  async function doRebase(operation, params, selectIdx) {
    await spinnerCall("POST", "/api/rebase/" + operation, params || {}, selectIdx);
  }

  async function refreshState() {
    if ($commitsList.dataset.dragging) return;
    await spinnerCall("GET", "/api/state");
  }

  // Applies a backend response and resolves the new selection:
  //   selectIdx != null -> single-select state.commits[selectIdx] (clamped).
  //   selectIdx == null -> keep previous selection (filtered to surviving hashes);
  //                        if now empty, single-select state.commits[0] (HEAD).
  function handleResponse(data, selectIdx) {
    if (data.ok || data.conflict) {
      state = data;
      const validHashes = new Set(state.commits.map(c => c.commit_hash));
      if (selectIdx != null) {
        const idx = Math.max(0, Math.min(selectIdx, state.commits.length - 1));
        setSingleSelection(state.commits[idx].commit_hash);
      } else {
        selected = new Set(Array.from(selected).filter(h => validHashes.has(h)));
        if (selected.size === 0 && state.commits.length > 0) {
          setSingleSelection(state.commits[0].commit_hash);
        }
      }
      clearBanner();
      render();
      if (selected.size > 0) {
        const sel = [...selected][0];
        // When the response bundled the selected commit's diff (the mutation was
        // told which commit the UI would select), render it without a second
        // round-trip. Otherwise fetch it on demand.
        if (data.diff && data.diff.commit && data.diff.commit.commit_hash === sel) renderDiff(data.diff, sel);
        else showDiff(sel);
      }
      if (data.ok && data.submodule_update_suggested) {
        showModal("Submodule pointers have changed. Run git submodule update --init? " +
                  "Refusing leaves the working tree dirty, which disables git warp.", false).then(async function (ok) {
          if (!ok) return;
          await spinnerCall("POST", "/api/submodule/update");
        });
      }
    } else {
      const errorMessages = {
        "gitmodules_differ": "Reset to a different set of subrepos is not supported.",
        "gitmodules_in_range": "Cannot reorder: range contains a commit that changes .gitmodules.",
        "split_failed": "Split failed; the commit was left unchanged.",
        "cannot_delete_current_branch": "Cannot delete the current branch.",
        "stash_conflict": "Stash pop caused conflicts. Resolve them in your editor, then run git stash drop.",
      };
      render();
      showBanner(errorMessages[data.error] || data.message || data.error || "Operation failed", "error");
    }
  }

  // ---- Event wiring ----
  async function doReset(hash) {
    await spinnerCall("POST", "/api/reset", {commit_hash: hash});
  }

  $btnUndo.addEventListener("click", () => {
    const entries = state.undo_stack || [];
    doReset(entries[headIdx + 1].commit_hash);
  });
  $btnRedo.addEventListener("click", () => {
    const entries = state.undo_stack || [];
    doReset(entries[headIdx - 1].commit_hash);
  });

  $btnRefresh.addEventListener("click", refreshState);

  $btnStash.addEventListener("click", () => spinnerCall("POST", "/api/stash"));
  $btnStashPop.addEventListener("click", () => spinnerCall("POST", "/api/stash/pop"));

  $btnSquash.addEventListener("click", function () {
    if (selected.size < 2) return;
    // commits are newest-first; the newest selected index is where squash places the result.
    doRebase("squash", {commit_hashes: Array.from(selected)}, state.commits.findIndex(function (c) { return selected.has(c.commit_hash); }));
  });

  $btnQuit.addEventListener("click", function () {
    fetch("/api/quit", {method: "POST", headers: {...HEADERS}, keepalive: true}).catch(() => {});
    window.close();
    document.body.innerHTML = "<p>Server stopped. Close this tab.</p>";
  });

  $btnAbort.addEventListener("click", () => spinnerCall("POST", "/api/rebase/abort"));
  $btnContinue.addEventListener("click", () => spinnerCall("POST", "/api/rebase/continue"));

  $btnSplit.addEventListener("click", () => {
    const selectIdx = state.commits.findIndex(function (c) { return c.commit_hash === diffHash; });
    spinnerCall("POST", "/api/rebase/split", {
      commit_hash: diffHash,
      files_to_split: Array.from(selectedFilesInDiff),
    }, selectIdx);
  });

  // Keyboard shortcuts
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (!$promptModal.classList.contains("hidden")) return;
      if (document.querySelector(".reword-input")) return;
      if (selected.size) { selected.clear(); anchor = null; renderCommits(); updateActionBar(); }
    }
  });

  $branchSelect.addEventListener("change", async function () {
    selected.clear();
    anchor = null;
    const branch = this.value;
    let data = await withSpinner(() => api("POST", "/api/switch", {branch}));
    if (data && !data.ok && data.error === "gitmodules_differ") {
      const ok = await showModal("This branch registers a different set of subrepos. Switching may leave an orphaned subrepo directory that git submodule update cannot clean up. Switch anyway?", false);
      if (!ok) {
        render();  // abandon switch; restore dropdown to current branch
        return;
      }
      data = await withSpinner(() => api("POST", "/api/switch", {branch, allow_different_gitmodules: true}));
    }
    if (data) handleResponse(data);
  });

  // Help modals
  function setupHelpModal(btnId, closeId, modal) {
    document.getElementById(btnId).addEventListener("click", () => modal.classList.remove("hidden"));
    document.getElementById(closeId).addEventListener("click", () => modal.classList.add("hidden"));
  }
  setupHelpModal("commits-help-btn", "commits-help-close", $commitsHelpModal);
  setupHelpModal("undo-stack-help-btn", "undo-stack-help-close", $undoStackHelpModal);

  // Auto-refresh on window focus
  window.addEventListener("focus", refreshState);

  // ---- Initial load ----
  refreshState();
})();
