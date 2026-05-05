/* git-history front-end */
(function () {
  "use strict";

  // ---- Token ----
  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get("t");
  if (urlToken) {
    localStorage.setItem("git_history_token", urlToken);
    history.replaceState(null, "", "/");
  }
  const TOKEN = localStorage.getItem("git_history_token") || "";
  document.getElementById("log-link").href = "/log?t=" + encodeURIComponent(TOKEN);

  // ---- State ----
  let state = null;         // latest server state
  let selected = new Set(); // set of selected hashes
  let anchor = null;        // shift-click anchor hash
  let busy = false;
  let dragState = null;     // {hashes, fromIndex, placeholder}
  let branchHistoryEntries = [];   // current filtered branch history list
  let headBranchHistoryIdx = -1;   // index of HEAD in branchHistoryEntries

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
  const $commitsList = document.getElementById("commits-list");
  const $branchHistoryList  = document.getElementById("branch-history-list");
  const $branchHistoryTitle  = document.getElementById("branch-history-title-text");
  const $commitsTitle = document.getElementById("commits-title-text");
  const $groupConsecRebases = document.getElementById("group-consec-rebases");
  const $conflictModal = document.getElementById("conflict-modal");
  const $submoduleModal = document.getElementById("submodule-modal");
  const $conflictFiles = document.getElementById("conflict-files");
  const $btnAbort    = document.getElementById("btn-abort");
  const $btnContinue = document.getElementById("btn-continue");
  const $commitsHelpModal = document.getElementById("commits-help-modal");
  const $branchHistoryHelpModal = document.getElementById("branch-history-help-modal");
  const $diffPane      = document.getElementById("diff-pane");
  const $diffResize    = document.getElementById("diff-resize");
  const $diffFiles     = document.getElementById("diff-files");
  const $diffContent   = document.getElementById("diff-content");
  const $branchSelect  = document.getElementById("branch-select");

  // ---- API helpers ----
  function headers(extra) {
    return Object.assign({"X-Token": TOKEN}, extra || {});
  }

  async function api(method, url, body) {
    const opts = {method, headers: headers()};
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  }

  function apiGet(url)        { return api("GET", url); }
  function apiPost(url, body) { return api("POST", url, body); }

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
    $branchHistoryTitle.textContent  = (state.branch || "(detached)") + " Branch History";
    $commitsTitle.textContent = (state.branch || "(detached)") + " Commit History";

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
    if (state.dirty) {
      showBanner("Working tree has uncommitted changes.", "warning");
      $btnStash.classList.remove("hidden");
    } else if (!state.branch) {
      showBanner("HEAD is detached. Select a branch from the dropdown.", "warning");
      $btnStash.classList.add("hidden");
    } else if (state.rebase_in_progress && state.conflict_files.length === 0) {
      showBanner("A rebase is in progress. Finish or abort it in your terminal to continue.", "warning");
      $btnStash.classList.add("hidden");
    } else {
      if ($banner.className === "warning") clearBanner();
      $btnStash.classList.add("hidden");
    }
    if (!state.has_stash || state.dirty) $btnStashPop.classList.add("hidden");
    else $btnStashPop.classList.remove("hidden");

    // Conflict modal
    if (state.rebase_in_progress && state.conflict_files.length > 0) {
      $conflictFiles.innerHTML = "";
      state.conflict_files.forEach(function (f) {
        const li = document.createElement("li");
        li.textContent = f;
        $conflictFiles.appendChild(li);
      });
      $conflictModal.classList.remove("hidden");
    } else {
      $conflictModal.classList.add("hidden");
    }

    renderCommits();
    renderBranchHistory();
    updateActionBar();
  }

  function showSubmoduleModal(onOk) {
    $submoduleModal.classList.remove("hidden");
    document.getElementById("btn-submodule-ok").onclick = async () => {
      $submoduleModal.classList.add("hidden");
      await onOk();
    };
    document.getElementById("btn-submodule-cancel").onclick = () => {
      $submoduleModal.classList.add("hidden");
    };
  }

  function renderCommits() {
    $commitsList.innerHTML = "";
    const mutDisabled = state.dirty || state.rebase_in_progress || !state.branch;
    state.commits.forEach(function (c, idx) {
      const row = document.createElement("div");
      row.className = "commit-row" + (selected.has(c.commit_hash) ? " selected" : "") + (c.is_head ? " is-head" : "");
      row.dataset.commitHash = c.commit_hash;
      row.dataset.idx = idx;
      row.dataset.message = c.message;

      // Drag handle
      const handle = document.createElement("span");
      handle.className = "drag-handle";
      handle.textContent = "\u2807";
      handle.addEventListener("mousedown", onDragStart);
      row.appendChild(handle);

      // Cloud indicator for pushed commits
      const cloud = document.createElement("span");
      cloud.className = "pushed-indicator";
      cloud.textContent = c.pushed ? "☁" : "";
      row.appendChild(cloud);

      // Short hash
      const sh = document.createElement("span");
      sh.className = "short-hash";
      sh.textContent = c.short_hash;
      row.appendChild(sh);

      // Badges
      const badges = document.createElement("span");
      badges.className = "badges";
      c.branches.forEach(function (b) {
        const s = document.createElement("span");
        s.className = "badge-branch";
        s.textContent = b;
        badges.appendChild(s);
      });
      c.tags.forEach(function (t) {
        const s = document.createElement("span");
        s.className = "badge-tag";
        s.textContent = t;
        badges.appendChild(s);
      });
      row.appendChild(badges);

      // Message
      const msg = document.createElement("span");
      msg.className = "message";
      msg.textContent = c.message;
      msg.addEventListener("dblclick", function () { startReword(row, c); });
      row.appendChild(msg);

      // Author
      const author = document.createElement("span");
      author.className = "author";
      author.textContent = c.author;
      row.appendChild(author);

      // Date
      const dt = document.createElement("span");
      dt.className = "date";
      dt.textContent = c.date ? c.date.slice(0, 10) : "";
      row.appendChild(dt);

      // Row actions
      const actions = document.createElement("span");
      actions.className = "row-actions";

      const btnFixup = document.createElement("button");
      btnFixup.innerHTML = '<img src="/static/fixup.png" width="18" height="18" alt="Fixup">';
      btnFixup.title = "Fixup";
      btnFixup.className = "btn-fixup";
      btnFixup.disabled = mutDisabled || idx === state.commits.length - 1;
      btnFixup.addEventListener("click", function (e) {
        e.stopPropagation();
        doRebase("fixup", {commit_hashes: [c.commit_hash]}, idx - 1);
      });
      actions.appendChild(btnFixup);

      row.appendChild(actions);

      // Click to select
      row.addEventListener("click", function (e) {
        if (e.target.closest(".row-actions") || e.target.closest(".drag-handle")) return;
        onRowClick(c.commit_hash, idx, e);
      });

      $commitsList.appendChild(row);
    });
  }

  function renderBranchHistory() {
    $branchHistoryList.innerHTML = "";
    if (!state.branch_history) return;
    let entries = state.branch_history;
    if ($groupConsecRebases.checked) {
      const filtered = [];
      let lastLabel = null;
      entries.forEach(function (entry) {
        if (entry.label === "rebase" && lastLabel === "rebase") return;
        filtered.push(entry);
        lastLabel = entry.label;
      });
      entries = filtered;
    }
    const headHash = state.commits.length > 0 ? state.commits[0].commit_hash : "";
    branchHistoryEntries = entries;
    headBranchHistoryIdx = entries.findIndex(function (e) { return e.commit_hash === headHash; });
    const canMutate = !state.dirty && !state.rebase_in_progress && !!state.branch;
    $btnUndo.disabled = !canMutate || headBranchHistoryIdx === -1 || headBranchHistoryIdx === entries.length - 1;
    $btnRedo.disabled = !canMutate || headBranchHistoryIdx <= 0;
    entries.forEach(function (entry) {
      const isHead = entry.commit_hash === headHash;
      const row = document.createElement("div");
      row.className = "branch-history-row" + (isHead ? " is-head" : "");

      const hashSpan = document.createElement("span");
      hashSpan.className = "branch-history-hash";
      hashSpan.textContent = entry.commit_hash.slice(0, 7);
      row.appendChild(hashSpan);

      if (isHead && state.branch) {
        const badge = document.createElement("span");
        badge.className = "badge-branch";
        badge.textContent = state.branch;
        row.appendChild(badge);
      }

      const labelSpan = document.createElement("span");
      labelSpan.className = "branch-history-label";
      labelSpan.textContent = entry.label;
      row.appendChild(labelSpan);

      const tsSpan = document.createElement("span");
      tsSpan.className = "branch-history-timestamp";
      tsSpan.textContent = entry.timestamp ? entry.timestamp.slice(0, 10) : "";
      row.appendChild(tsSpan);

      row.addEventListener("dblclick", function (e) {
        e.preventDefault();
        if (!isHead) doReset(entry.commit_hash);
      });
      $branchHistoryList.appendChild(row);
    });
  }

  // ---- Selection ----
  function applySelectionClasses() {
    document.querySelectorAll(".commit-row").forEach(function (r) {
      r.classList.toggle("selected", selected.has(r.dataset.commitHash));
    });
  }

  function onRowClick(hash, idx, e) {
    if (e.shiftKey && anchor !== null) {
      // Extend selection contiguously from anchor.
      const anchorIdx = state.commits.findIndex(function (c) { return c.commit_hash === anchor; });
      if (anchorIdx === -1) { anchor = hash; selected = new Set([hash]); }
      else {
        const lo = Math.min(anchorIdx, idx);
        const hi = Math.max(anchorIdx, idx);
        selected = new Set();
        for (let i = lo; i <= hi; i++) selected.add(state.commits[i].commit_hash);
      }
    } else {
      selected = new Set([hash]);
      anchor = hash;
      showDiff(hash);
    }
    applySelectionClasses();
    updateActionBar();
  }

  function updateActionBar() {
    if (selected.size >= 2) {
      $btnSquash.classList.remove("hidden");
      const mutDisabled = state.dirty || state.rebase_in_progress || !state.branch;
      $btnSquash.disabled = mutDisabled;
    } else {
      $btnSquash.classList.add("hidden");
    }
  }

  // ---- Reword ----
  function startReword(row, commit) {
    if (state.dirty || state.rebase_in_progress || busy) return;
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
  function removeDragIndicator() {
    const el = $commitsList.querySelector(".drop-indicator");
    if (el) el.remove();
  }

  function cancelDrag() {
    document.removeEventListener("mousemove", onDragMove);
    document.removeEventListener("mouseup", onDragEnd);
    delete $commitsList.dataset.dragging;
    removeDragIndicator();
    if (dragState) {
      dragState.originalRows.forEach(function (r) { $commitsList.appendChild(r); });
      document.querySelectorAll(".commit-row.dragging").forEach(function (r) { r.classList.remove("dragging"); r.style.opacity = ""; });
    }
    dragState = null;
  }

  function onDragStart(e) {
    if (state.dirty || state.rebase_in_progress || !state.branch || busy) return;
    e.preventDefault();
    const row = e.target.closest(".commit-row");
    const hash = row.dataset.commitHash;

    if (!selected.has(hash)) {
      selected = new Set([hash]);
      anchor = hash;
    }

    const originalRows = Array.from($commitsList.querySelectorAll(".commit-row"));
    dragState = {originalRows: originalRows, currentInsert: -1};

    document.querySelectorAll(".commit-row").forEach(function (r) {
      if (selected.has(r.dataset.commitHash)) { r.classList.add("dragging"); r.style.opacity = "0.4"; }
    });

    document.addEventListener("mousemove", onDragMove);
    document.addEventListener("mouseup", onDragEnd);
    $commitsList.dataset.dragging = "1";
  }

  function onDragMove(e) {
    if (!dragState) return;
    const rows = Array.from($commitsList.querySelectorAll(".commit-row"));

    let insertIdx = rows.length;
    for (let i = 0; i < rows.length; i++) {
      const rect = rows[i].getBoundingClientRect();
      if (e.clientY < rect.top + rect.height / 2) { insertIdx = i; break; }
    }

    const selectedRows = rows.filter(function (r) { return selected.has(r.dataset.commitHash); });
    const nonSelectedRows = rows.filter(function (r) { return !selected.has(r.dataset.commitHash); });

    let nonSelInsert = 0;
    for (let i = 0; i < insertIdx; i++) {
      if (!selected.has(rows[i].dataset.commitHash)) nonSelInsert++;
    }

    if (nonSelInsert === dragState.currentInsert) return;
    dragState.currentInsert = nonSelInsert;

    removeDragIndicator();
    nonSelectedRows.slice(0, nonSelInsert).forEach(function (r) { $commitsList.appendChild(r); });
    selectedRows.forEach(function (r) { $commitsList.appendChild(r); });
    nonSelectedRows.slice(nonSelInsert).forEach(function (r) { $commitsList.appendChild(r); });

    const firstDragging = $commitsList.querySelector(".commit-row.dragging");
    if (firstDragging) {
      const ind = document.createElement("div");
      ind.className = "drop-indicator";
      $commitsList.insertBefore(ind, firstDragging);
    }
  }

  function onDragEnd() {
    document.removeEventListener("mousemove", onDragMove);
    document.removeEventListener("mouseup", onDragEnd);
    delete $commitsList.dataset.dragging;
    removeDragIndicator();
    document.querySelectorAll(".commit-row.dragging").forEach(function (r) { r.classList.remove("dragging"); r.style.opacity = ""; });

    if (!dragState) return;
    const originalRows = dragState.originalRows;
    dragState = null;

    const rows = Array.from($commitsList.querySelectorAll(".commit-row"));
    const newOrder = rows.map(function (r) { return r.dataset.commitHash; });
    const originalOrder = originalRows.map(function (r) { return r.dataset.commitHash; });

    if (newOrder.join(",") === originalOrder.join(",")) return;
    doRebase("move", {order: newOrder}, selected.size === 1 ? newOrder.indexOf([...selected][0]) : null);
  }

  // ---- Diff resize ----
  $diffResize.addEventListener("mousedown", function (e) {
    e.preventDefault();
    const startY = e.clientY, startH = $diffPane.offsetHeight;
    function onMove(e) { $diffPane.style.height = Math.max(60, startH - (e.clientY - startY)) + "px"; }
    function onUp() { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });

  // ---- Diff pane ----
  async function showDiff(hash) {
    try {
      const data = await apiGet("/api/show?commit_hash=" + hash);
      if (data.ok) {
        // Colorize added lines green and deleted lines red, skipping +++ / --- headers
        $diffContent.innerHTML = (data.diff || "").split("\n").map(function (line) {
          const escaped = line.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
          if (/^\+(?!\+\+)/.test(line)) return '<span class="diff-add">' + escaped + '</span>';
          if (/^-(?!--)/.test(line)) return '<span class="diff-del">' + escaped + '</span>';
          return escaped;
        }).join("\n");
        $diffFiles.innerHTML = "";
        (data.diff || "").split("\n").forEach(function (line) {
          const m = line.match(/^diff --git .+ b\/(.+)$/);
          if (m) { const d = document.createElement("div"); d.textContent = m[1]; $diffFiles.appendChild(d); }
        });
        $diffPane.classList.remove("hidden");
      } else {
        showBanner(data.error || "Failed to load diff", "error");
      }
    } catch (err) { showBanner("Failed to load diff: " + err.message, "error"); }
  }

  // ---- API actions ----
  async function doRebase(operation, params, selectIdx) {
    if (busy) return;
    showSpinner();
    try {
      const body = Object.assign({operation: operation}, params);
      const data = await apiPost("/api/rebase", body);
      handleResponse(data, selectIdx);
    } catch (err) {
      showBanner("Request failed: " + err.message, "error");
    }
    hideSpinner();
  }

  async function refreshState() {
    if (busy) return;
    showSpinner();
    try {
      const data = await apiGet("/api/state");
      handleResponse(data);
    } catch (err) {
      showBanner("Request failed: " + err.message, "error");
    }
    hideSpinner();
  }

  function handleResponse(data, selectIdx) {
    if (data.ok || data.conflict) {
      state = data;
      // Prune selection to commits that still exist.
      const validHashes = new Set(state.commits.map(function (c) { return c.commit_hash; }));
      selected = new Set(Array.from(selected).filter(function (h) { return validHashes.has(h); }));
      if (selectIdx != null) {
        const idx = Math.max(0, Math.min(selectIdx, state.commits.length - 1));
        selected = new Set([state.commits[idx].commit_hash]);
        anchor = state.commits[idx].commit_hash;
      } else if (selected.size === 0 && state.commits.length > 0) {
        selected = new Set([state.commits[0].commit_hash]);
        anchor = state.commits[0].commit_hash;
      }
      clearBanner();
      render();
      if (selected.size > 0) showDiff([...selected][0]);
    } else {
      const errorMessages = {
        "gitmodules_differ": "Reset to a different set of subrepos is not supported.",
        "gitmodules_in_range": "Cannot reorder: range contains a commit that changes .gitmodules.",
      };
      showBanner(errorMessages[data.error] || data.message || data.error || "Operation failed", "error");
    }
  }

  // ---- Event wiring ----
  async function doReset(hash) {
    if (busy) return;
    showSpinner();
    try {
      const data = await apiPost("/api/reset", {commit_hash: hash});
      handleResponse(data);
      if (data.ok && data.submodule_update_suggested) {
        hideSpinner();
        showSubmoduleModal(async () => {
          showSpinner();
          handleResponse(await apiPost("/api/submodule/update"));
          hideSpinner();
        });
        return;
      }
    } catch (err) { showBanner("Reset failed: " + err.message, "error"); }
    hideSpinner();
  }

  $btnUndo.addEventListener("click", function () {
    if ($btnUndo.disabled || headBranchHistoryIdx === -1) return;
    doReset(branchHistoryEntries[headBranchHistoryIdx + 1].commit_hash);
  });

  $btnRedo.addEventListener("click", function () {
    if ($btnRedo.disabled || headBranchHistoryIdx <= 0) return;
    doReset(branchHistoryEntries[headBranchHistoryIdx - 1].commit_hash);
  });

  $btnRefresh.addEventListener("click", refreshState);

  $btnStash.addEventListener("click", async function () {
    if (busy) return;
    showSpinner();
    try { handleResponse(await apiPost("/api/stash")); }
    catch (err) { showBanner("Stash failed: " + err.message, "error"); }
    hideSpinner();
  });

  $btnStashPop.addEventListener("click", async function () {
    if (busy) return;
    showSpinner();
    try { handleResponse(await apiPost("/api/stash/pop")); }
    catch (err) { showBanner("Stash pop failed: " + err.message, "error"); }
    hideSpinner();
  });

  $btnSquash.addEventListener("click", function () {
    if (selected.size < 2) return;
    // commits are newest-first; findIndex returns the newest selected index, which is where squash places the result.
    doRebase("squash", {commit_hashes: Array.from(selected)}, state.commits.findIndex(function (c) { return selected.has(c.commit_hash); }));
  });

  $btnQuit.addEventListener("click", function () {
    fetch("/api/quit", {method: "POST", headers: headers(), keepalive: true}).catch(() => {});
    window.close();
    document.body.innerHTML = "<p>Server stopped. Close this tab.</p>";
  });

  $btnAbort.addEventListener("click", async function () {
    if (busy) return;
    showSpinner();
    try { handleResponse(await apiPost("/api/rebase/abort")); }
    catch (err) { showBanner("Abort failed: " + err.message, "error"); }
    hideSpinner();
  });

  $btnContinue.addEventListener("click", async function () {
    if (busy) return;
    showSpinner();
    try { handleResponse(await apiPost("/api/rebase/continue")); }
    catch (err) { showBanner("Continue failed: " + err.message, "error"); }
    hideSpinner();
  });

  // Keyboard shortcuts
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (dragState) cancelDrag();
      if (selected.size) { selected.clear(); anchor = null; renderCommits(); }
    }
  });

  $branchSelect.addEventListener("change", async function () {
    if (busy) return;
    selected.clear();
    anchor = null;
    showSpinner();
    try {
      const data = await apiPost("/api/switch", {branch: this.value});
      handleResponse(data);
      if (data.ok && data.submodule_update_suggested) {
        hideSpinner();
        showSubmoduleModal(async () => {
          showSpinner();
          handleResponse(await apiPost("/api/submodule/update"));
          hideSpinner();
        });
        return;
      }
    } catch (err) { showBanner("Switch failed: " + err.message, "error"); }
    hideSpinner();
  });

  $groupConsecRebases.addEventListener("change", renderBranchHistory);

  // Help modal
  document.getElementById("commits-help-btn").addEventListener("click", function () {
    $commitsHelpModal.classList.remove("hidden");
  });
  document.getElementById("commits-help-close").addEventListener("click", function () {
    $commitsHelpModal.classList.add("hidden");
  });
  document.getElementById("branch-history-help-btn").addEventListener("click", function () {
    $branchHistoryHelpModal.classList.remove("hidden");
  });
  document.getElementById("branch-history-help-close").addEventListener("click", function () {
    $branchHistoryHelpModal.classList.add("hidden");
  });

  // Auto-refresh on window focus
  window.addEventListener("focus", refreshState);

  // ---- Initial load ----
  refreshState();
})();
