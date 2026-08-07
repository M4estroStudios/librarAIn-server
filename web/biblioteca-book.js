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
    if (overview) overview.classList.toggle("hidden", !isOverview);
    if (pageView) pageView.classList.toggle("hidden", isOverview);
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
  }

  function disconnectObserver() {
    if (thumbObserver) {
      thumbObserver.disconnect();
      thumbObserver = null;
    }
  }

  function renderGrid() {
    var grid = $("biblio-overview-grid");
    if (!grid || !api) return;
    var book = api.getSelected();
    disconnectObserver();
    gridLoadGen += 1;
    var loadGen = gridLoadGen;
    if (!book) {
      grid.innerHTML = "<p class='hint'>Nessun libro selezionato.</p>";
      return;
    }
    var max = api.maxPage();
    var html = [];
    for (var page = 1; page <= max; page += 1) {
      html.push(
        "<button type='button' class='biblio-overview-thumb' data-page='" + page + "' title='Pagina " + page + "'>" +
        "<span class='biblio-overview-thumb-ph'>…</span>" +
        "<img alt='' data-src='" + esc(api.previewUrl(page)) + "'>" +
        "<span class='biblio-overview-thumb-num'>" + page + "</span>" +
        "</button>"
      );
    }
    grid.innerHTML = html.join("");
    grid.querySelectorAll(".biblio-overview-thumb").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var pageNum = parseInt(btn.getAttribute("data-page"), 10);
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
    if (mode === "page") {
      showOverview();
      return true;
    }
    return false;
  }

  function setButtonsBusy(busy) {
    document.querySelectorAll("[data-biblio-stage]").forEach(function (btn) {
      btn.disabled = !!busy;
    });
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

  function bind() {
    document.querySelectorAll("[data-biblio-stage]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        runStage(btn.getAttribute("data-biblio-stage") || "");
      });
    });
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
    renderGrid: renderGrid
  };
})(window);
