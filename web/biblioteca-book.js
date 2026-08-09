(function (global) {
  "use strict";

  var STAGE_LABELS = {
    polyindex_toc: "Polyindex TOC",
    polyindex_index: "Polyindex INDEX",
    time_index: "Polyindex TIME_INDEX",
    polyindex_biblio: "Polyindex BIBLIO"
  };

  var api = null;
  var mode = "overview";
  var gridLoadGen = 0;
  var thumbObserver = null;
  var runningStage = null;
  var selectedPages = Object.create(null);
  var selectedBookSha = null;

  function $(id) {
    return document.getElementById(id);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function setStageStatus(msg) {
    var el = $("biblio-stage-status");
    if (el) el.textContent = msg || "";
  }

  function syncChrome() {
    var isOverview = mode === "overview";
    var overview = $("biblio-overview-view");
    var pageView = $("biblio-page-view");
    var closeBtn = $("biblio-panel-close");
    if (overview) overview.classList.toggle("hidden", !isOverview);
    if (pageView) pageView.classList.toggle("hidden", isOverview);
    if (closeBtn) closeBtn.title = isOverview ? "Chiudi" : "Torna alla griglia";
    document.querySelectorAll("[data-biblio-chrome='overview']").forEach(function (el) {
      el.classList.toggle("hidden", !isOverview);
    });
    document.querySelectorAll("[data-biblio-chrome='page']").forEach(function (el) {
      if (isOverview) el.classList.add("hidden");
      else if (el.id !== "biblio-btn-edit") el.classList.remove("hidden");
    });
    if (isOverview) {
      var editBtn = $("biblio-btn-edit");
      var actions = $("biblio-transcript-actions");
      var status = $("biblio-edit-status");
      if (editBtn) editBtn.classList.add("hidden");
      if (actions) actions.classList.add("hidden");
      if (status) status.classList.add("hidden");
    }
    syncClearSelectionBtn();
    if (api && typeof api.onChromeSync === "function") api.onChromeSync();
  }

  function disconnectObserver() {
    if (thumbObserver) {
      thumbObserver.disconnect();
      thumbObserver = null;
    }
  }

  function syncSelectedBook() {
    var book = api && api.getSelected();
    var sha = book && book.source_sha256 ? book.source_sha256 : null;
    if (sha !== selectedBookSha) {
      selectedBookSha = sha;
      selectedPages = Object.create(null);
    }
  }

  function selectedCount() {
    return Object.keys(selectedPages).length;
  }

  function getSelectedPages() {
    return Object.keys(selectedPages).map(Number).filter(function (page) {
      return page >= 1;
    }).sort(function (a, b) { return a - b; });
  }

  function notifySelectionChange() {
    syncClearSelectionBtn();
    if (api && typeof api.onSelectionChange === "function") api.onSelectionChange();
    else if (api && typeof api.onChromeSync === "function") api.onChromeSync();
  }

  function syncClearSelectionBtn() {
    var btn = $("biblio-btn-clear-selection");
    if (!btn) return;
    var show = mode === "overview" && selectedCount() > 0;
    btn.classList.toggle("is-slot-hidden", !show);
    btn.setAttribute("aria-hidden", show ? "false" : "true");
    btn.tabIndex = show ? 0 : -1;
  }

  function clearPageSelection() {
    selectedPages = Object.create(null);
    document.querySelectorAll(".biblio-overview-thumb.is-selected").forEach(function (el) {
      el.classList.remove("is-selected");
      el.setAttribute("aria-pressed", "false");
    });
    notifySelectionChange();
  }

  function resetOverlaySelection() {
    selectedBookSha = null;
    clearPageSelection();
  }

  function togglePageSelection(pageNum, thumbEl) {
    if (selectedPages[pageNum]) delete selectedPages[pageNum];
    else selectedPages[pageNum] = true;
    if (thumbEl) {
      thumbEl.classList.toggle("is-selected", !!selectedPages[pageNum]);
      thumbEl.setAttribute("aria-pressed", selectedPages[pageNum] ? "true" : "false");
    }
    notifySelectionChange();
  }

  function renderGrid() {
    var grid = $("biblio-overview-grid");
    if (!grid || !api) return;
    var book = api.getSelected();
    disconnectObserver();
    gridLoadGen += 1;
    var loadGen = gridLoadGen;
    syncSelectedBook();
    if (!book) {
      grid.innerHTML = "<p class='hint'>Nessun libro selezionato.</p>";
      return;
    }
    var pages = (typeof api.listPages === "function") ? api.listPages() : null;
    if (!Array.isArray(pages) || !pages.length) {
      var max = api.maxPage();
      pages = [];
      for (var page = 1; page <= max; page += 1) pages.push(page);
    }
    var allowed = Object.create(null);
    pages.forEach(function (page) { allowed[page] = true; });
    Object.keys(selectedPages).forEach(function (key) {
      if (!allowed[Number(key)]) delete selectedPages[key];
    });
    var html = [];
    var checkSvg = "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.8' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M5 13l4 4L19 7'/></svg>";
    var eyeSvg = "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'><path d='M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z'/><circle cx='12' cy='12' r='3'/></svg>";
    pages.forEach(function (page) {
      var selected = !!selectedPages[page];
      var dirty = api && typeof api.isPagePending === "function" && api.isPagePending(page);
      var needsVision = api && typeof api.isPageNeedsVision === "function" && api.isPageNeedsVision(page);
      html.push(
        "<div class='biblio-overview-thumb" + (selected ? " is-selected" : "") + (dirty ? " is-dirty" : "") + (needsVision ? " is-needs-vision" : "") + "' data-page='" + page + "' role='button' tabindex='0' title='Pagina " + page + (needsVision ? " · richiede Vision" : "") + "' aria-pressed='" + (selected ? "true" : "false") + "'>" +
        "<span class='biblio-overview-thumb-ph'>…</span>" +
        "<img alt='' data-src='" + esc(api.previewUrl(page)) + "'>" +
        "<span class='biblio-overview-thumb-num'>" + page + "</span>" +
        "<span class='biblio-overview-thumb-badges'>" +
        "<button type='button' class='biblio-overview-thumb-check' title='Seleziona pagina " + page + "' aria-label='Seleziona pagina " + page + "'>" + checkSvg + "</button>" +
        "<span class='biblio-overview-thumb-vision' title='Richiede modello Vision' aria-label='Richiede modello Vision'>" + eyeSvg + "</span>" +
        "</span>" +
        "</div>"
      );
    });
    grid.innerHTML = html.join("");
    grid.querySelectorAll(".biblio-overview-thumb").forEach(function (btn) {
      var pageNum = parseInt(btn.getAttribute("data-page"), 10);
      var check = btn.querySelector(".biblio-overview-thumb-check");
      if (check) {
        check.addEventListener("click", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          if (!isNaN(pageNum)) togglePageSelection(pageNum, btn);
        });
      }
      btn.addEventListener("click", function () {
        if (!isNaN(pageNum)) showPage(pageNum);
      });
      btn.addEventListener("contextmenu", function (ev) {
        ev.preventDefault();
        if (!isNaN(pageNum)) togglePageSelection(pageNum, btn);
      });
      btn.addEventListener("keydown", function (ev) {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        if (ev.target !== btn) return;
        ev.preventDefault();
        if (!isNaN(pageNum)) showPage(pageNum);
      });
    });
    if (!("IntersectionObserver" in window)) {
      grid.querySelectorAll("img[data-src]").forEach(function (img) {
        loadThumb(img, loadGen);
      });
      return;
    }
    thumbObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        var img = entry.target;
        thumbObserver.unobserve(img);
        loadThumb(img, loadGen);
      });
    }, { root: $("biblio-overview-grid-wrap"), rootMargin: "120px" });
    grid.querySelectorAll("img[data-src]").forEach(function (img) {
      thumbObserver.observe(img);
    });
    syncClearSelectionBtn();
  }

  function loadThumb(img, loadGen) {
    if (loadGen !== gridLoadGen) return;
    var src = img.getAttribute("data-src");
    if (!src) return;
    img.onload = function () {
      if (loadGen !== gridLoadGen) return;
      img.classList.add("is-loaded");
      var ph = img.parentElement && img.parentElement.querySelector(".biblio-overview-thumb-ph");
      if (ph) ph.classList.add("hidden");
    };
    img.onerror = function () {
      if (loadGen !== gridLoadGen) return;
      var ph = img.parentElement && img.parentElement.querySelector(".biblio-overview-thumb-ph");
      if (ph) ph.textContent = "—";
    };
    img.src = src;
    img.removeAttribute("data-src");
  }

  function showOverview() {
    mode = "overview";
    setStageStatus("");
    if (api && typeof api.onOverview === "function") api.onOverview();
    syncChrome();
    renderGrid();
    requestAnimationFrame(syncOverviewGridColumns);
  }

  function showPage(pageNum) {
    mode = "page";
    syncChrome();
    if (api && typeof api.openPage === "function") api.openPage(pageNum || 1);
    syncChrome();
  }

  function currentMode() {
    return mode;
  }

  function handleEscape() {
    if (mode !== "page") return false;
    if (api && typeof api.beforeLeavePage === "function") {
      var handled = api.beforeLeavePage(function () { showOverview(); });
      if (handled) return true;
    }
    showOverview();
    return true;
  }

  function setButtonsBusy(busy) {
    document.querySelectorAll("[data-biblio-stage], [data-biblio-stage-toggle]").forEach(function (btn) {
      btn.disabled = !!busy;
    });
  }

  function collapseStageCards(exceptStage) {
    document.querySelectorAll("[data-biblio-stage-card]").forEach(function (card) {
      var stage = card.getAttribute("data-biblio-stage-card") || "";
      var open = exceptStage && stage === exceptStage && !card.classList.contains("is-open");
      var panel = card.querySelector(".biblio-stage-panel");
      var toggle = card.querySelector("[data-biblio-stage-toggle]");
      if (open) {
        card.classList.add("is-open");
        if (panel) panel.hidden = false;
        if (toggle) toggle.setAttribute("aria-expanded", "true");
      } else {
        card.classList.remove("is-open");
        if (panel) panel.hidden = true;
        if (toggle) toggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  function toggleStageCard(stage) {
    if (!stage) return;
    var card = document.querySelector('[data-biblio-stage-card="' + stage + '"]');
    if (!card) return;
    if (card.classList.contains("is-open")) collapseStageCards(null);
    else collapseStageCards(stage);
  }

  function runStage(stage) {
    if (!api || runningStage) return;
    var book = api.getSelected();
    if (!book) {
      setStageStatus("Nessun libro selezionato.");
      return;
    }
    var body = {
      source_sha256: book.source_sha256,
      stage: stage,
      compute_mode: "local"
    };
    if (stage === "polyindex_biblio") {
      if (!book.biblio_range || book.biblio_range.start == null || book.biblio_range.end == null) {
        setStageStatus("Serve biblio_range nel manifest per BIBLIO.");
        return;
      }
      body.biblio_range = book.biblio_range;
    }
    runningStage = stage;
    setButtonsBusy(true);
    setStageStatus("Avvio " + (STAGE_LABELS[stage] || stage) + "…");
    fetch("/api/admin/biblio/polyindex/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (data) { return { r: r, data: data }; }); })
      .then(function (pack) {
        if (!pack.r.ok || !pack.data.ok) {
          throw new Error((pack.data && pack.data.error) || ("HTTP " + pack.r.status));
        }
        var jobId = pack.data.job_id;
        setStageStatus((STAGE_LABELS[stage] || stage) + " avviato · job " + jobId);
        return watchJob(jobId, stage);
      })
      .catch(function (err) {
        setStageStatus(String(err.message || err));
      })
      .finally(function () {
        runningStage = null;
        setButtonsBusy(false);
      });
  }

  function watchJob(jobId, stage) {
    return new Promise(function (resolve) {
      var es;
      try {
        es = new EventSource("/api/ingest/" + encodeURIComponent(jobId) + "/events");
      } catch (_) {
        setStageStatus((STAGE_LABELS[stage] || stage) + " avviato (no SSE). Vedi /jobs.");
        resolve();
        return;
      }
      var settled = false;
      function finish(msg) {
        if (settled) return;
        settled = true;
        try { es.close(); } catch (_) {}
        setStageStatus(msg);
        resolve();
      }
      es.onmessage = function (ev) {
        var data;
        try { data = JSON.parse(ev.data); } catch (_) { return; }
        var st = data.status || data.event_status;
        if (st === "done" || st === "completed") {
          finish((STAGE_LABELS[stage] || stage) + " completato.");
        } else if (st === "error" || st === "failed") {
          finish((STAGE_LABELS[stage] || stage) + " errore: " + (data.message || "fallito"));
        }
      };
      es.onerror = function () {
        finish((STAGE_LABELS[stage] || stage) + " in corso (SSE chiuso). Controlla /jobs.");
      };
    });
  }

  function syncOverviewGridColumns() {
    var wrap = $("biblio-overview-grid-wrap");
    var grid = $("biblio-overview-grid");
    if (!wrap || !grid) return;
    var style = window.getComputedStyle(wrap);
    var padX = (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
    var avail = Math.max(0, wrap.clientWidth - padX);
    var gap = 0.45 * 16;
    var col = 7.5 * 16;
    var fitted = Math.floor((avail + gap) / (col + gap));
    var cols = Math.max(5, Math.min(10, fitted || 5));
    grid.style.setProperty("--biblio-grid-cols", String(cols));
  }

  function bindStagesResizer() {
    var body = $("biblio-overview-view");
    var stages = $("biblio-overview-stages");
    var resizer = $("biblio-overview-resizer");
    if (!body || !stages || !resizer || resizer.dataset.bound === "1") return;
    resizer.dataset.bound = "1";
    var dragging = false;
    var startX = 0;
    var startWidth = 0;

    function minGridWidthForCols(cols) {
      var col = 7.5 * 16;
      var gap = 0.45 * 16;
      var wrapPad = 0.55 * 16 * 2;
      var wrapBorder = 2;
      var wrapMargin = 0.35 * 16;
      var resizer = 6 + 0.2 * 16 * 2;
      var vScrollSlack = 18;
      return cols * col + Math.max(0, cols - 1) * gap + wrapPad + wrapBorder + wrapMargin + resizer + vScrollSlack;
    }

    function clampStagesWidth(px) {
      var bodyStyle = window.getComputedStyle(body);
      var bodyPad = (parseFloat(bodyStyle.paddingLeft) || 0) + (parseFloat(bodyStyle.paddingRight) || 0);
      var contentW = Math.max(0, (body.clientWidth || 0) - bodyPad);
      var minStages = 16 * 16;
      var maxStages = Math.max(minStages, contentW - minGridWidthForCols(5));
      return Math.max(minStages, Math.min(maxStages, px));
    }

    function applyWidth(px) {
      var next = clampStagesWidth(px);
      stages.style.setProperty("--biblio-stages-width", (next / 16) + "rem");
      stages.style.width = next + "px";
      stages.style.flexBasis = next + "px";
      syncOverviewGridColumns();
    }

    function onMove(ev) {
      if (!dragging) return;
      var clientX = ev.touches && ev.touches[0] ? ev.touches[0].clientX : ev.clientX;
      var delta = startX - clientX;
      applyWidth(startWidth + delta);
      ev.preventDefault();
    }

    function onUp() {
      if (!dragging) return;
      dragging = false;
      resizer.classList.remove("is-dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }

    function onDown(ev) {
      if (window.matchMedia && window.matchMedia("(max-width: 900px)").matches) return;
      dragging = true;
      startX = ev.touches && ev.touches[0] ? ev.touches[0].clientX : ev.clientX;
      startWidth = stages.getBoundingClientRect().width;
      resizer.classList.add("is-dragging");
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      ev.preventDefault();
    }

    resizer.addEventListener("mousedown", onDown);
    resizer.addEventListener("touchstart", onDown, { passive: false });
    window.addEventListener("mousemove", onMove);
    window.addEventListener("touchmove", onMove, { passive: false });
    window.addEventListener("mouseup", onUp);
    window.addEventListener("touchend", onUp);
    resizer.addEventListener("keydown", function (ev) {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      var cur = stages.getBoundingClientRect().width;
      applyWidth(cur + (ev.key === "ArrowLeft" ? 24 : -24));
      ev.preventDefault();
    });
    window.addEventListener("resize", function () {
      applyWidth(stages.getBoundingClientRect().width);
    });
    syncOverviewGridColumns();
  }

  function bind() {
    document.querySelectorAll("[data-biblio-stage-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        toggleStageCard(btn.getAttribute("data-biblio-stage-toggle") || "");
      });
    });
    document.querySelectorAll("[data-biblio-stage]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        runStage(btn.getAttribute("data-biblio-stage") || "");
      });
    });
    var clearBtn = $("biblio-btn-clear-selection");
    if (clearBtn) clearBtn.addEventListener("click", clearPageSelection);
    bindStagesResizer();
  }

  function init(hostApi) {
    api = hostApi || null;
    bind();
    syncChrome();
  }

  global.BiblioBookOverlay = {
    init: init,
    showOverview: showOverview,
    showPage: showPage,
    handleEscape: handleEscape,
    currentMode: currentMode,
    renderGrid: renderGrid,
    clearSelection: resetOverlaySelection,
    getSelectedPages: getSelectedPages
  };
})(window);
