const MOCK_SHA = "a".repeat(64);
const FIXTURE_BASE = "/mockup/fixtures/";
const STORAGE_KEY = "librarainIngestDebugMock";

let enabled = false;
const transcriptOverrides = new Map();
const renderUrlCache = new Map();
let auditState = null;
let repairContext = null;

function jsonResponse(payload, status) {
  return new Response(JSON.stringify(payload), {
    status: status || 200,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

async function loadFixture(name) {
  if (window.__librarainMockFixtures && window.__librarainMockFixtures[name]) {
    return JSON.parse(JSON.stringify(window.__librarainMockFixtures[name]));
  }
  const res = await fetch(FIXTURE_BASE + name);
  if (!res.ok) {
    if (window.__librarainMockFixtures && window.__librarainMockFixtures[name]) {
      return JSON.parse(JSON.stringify(window.__librarainMockFixtures[name]));
    }
    throw new Error("Fixture " + name + " non trovato");
  }
  return res.json();
}

function pageSvgDataUrl(aligned) {
  if (renderUrlCache.has(aligned)) return renderUrlCache.get(aligned);
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="640" viewBox="0 0 480 640">' +
    '<rect width="480" height="640" fill="#f5f5f0"/>' +
    '<rect x="24" y="24" width="432" height="592" fill="#fff" stroke="#333" stroke-width="2"/>' +
    '<text x="240" y="120" text-anchor="middle" font-family="Georgia, serif" font-size="28" fill="#222">' +
    "Pagina mock " + aligned +
    "</text>" +
    '<text x="240" y="170" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#666">' +
    "librarAIn mock</text></svg>";
  const url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
  renderUrlCache.set(aligned, url);
  return url;
}

function transcriptForPage(aligned, transcripts) {
  if (transcriptOverrides.has(aligned)) {
    return transcriptOverrides.get(aligned);
  }
  const pages = transcripts.pages || {};
  if (Object.prototype.hasOwnProperty.call(pages, String(aligned))) {
    return pages[String(aligned)];
  }
  const fallback = transcripts.default || {};
  return fallback.text || "Testo mock pagina " + aligned + ".";
}

function parsePath(url) {
  try {
    return new URL(url, location.origin).pathname;
  } catch {
    return String(url || "").split("?")[0];
  }
}

function resetAuditState() {
  auditState = null;
  repairContext = null;
  transcriptOverrides.clear();
}

async function ensureAuditState() {
  if (!auditState) {
    auditState = await loadFixture("audit.json");
  }
  return auditState;
}

function findAuditBook(payload, sha) {
  const target = String(sha || MOCK_SHA).trim().toLowerCase();
  return (payload.books || []).find(function (book) {
    return String(book.source_sha256 || "").toLowerCase() === target;
  }) || null;
}

function syncBookStageCounts(book) {
  const expected = book.expected_page_count || (book.viewer_pages || []).length;
  Object.keys(book.stages || {}).forEach(function (key) {
    const stage = book.stages[key];
    const missing = Array.isArray(stage.missing) ? stage.missing : [];
    stage.missing = missing;
    stage.missing_count = missing.length;
    stage.present_count = Math.max(0, expected - missing.length);
  });
  book.complete = !(book.missing_pages || []).length;
}

function syncAuditSummary(payload) {
  const books = payload.books || [];
  let totalGaps = 0;
  let withGaps = 0;
  books.forEach(function (book) {
    const count = (book.missing_pages || []).length;
    totalGaps += count;
    if (count > 0) withGaps += 1;
  });
  payload.summary = Object.assign({}, payload.summary || {}, {
    book_count: books.length,
    books_with_gaps: withGaps,
    books_complete: books.length - withGaps,
    total_pages_with_gaps: totalGaps,
  });
}

function markPageRepaired(sha, aligned) {
  if (!auditState || typeof aligned !== "number") return;
  const book = findAuditBook(auditState, sha);
  if (!book) return;
  book.missing_pages = (book.missing_pages || []).filter(function (entry) {
    return entry.aligned !== aligned;
  });
  Object.keys(book.stages || {}).forEach(function (key) {
    const stage = book.stages[key];
    if (Array.isArray(stage.missing)) {
      stage.missing = stage.missing.filter(function (page) { return page !== aligned; });
    }
  });
  syncBookStageCounts(book);
  if ((book.pending_review_pages || []).indexOf(aligned) < 0) {
    book.pending_review_pages = (book.pending_review_pages || []).concat([aligned]).sort(function (a, b) {
      return a - b;
    });
  }
  syncAuditSummary(auditState);
}

function markPageReviewConfirmed(sha, aligned) {
  aligned = parseInt(aligned, 10);
  if (!auditState || !Number.isFinite(aligned) || aligned < 1) return;
  const book = findAuditBook(auditState, sha);
  if (!book) return;
  book.pending_review_pages = (book.pending_review_pages || []).filter(function (page) {
    return page !== aligned;
  });
  const output = book.stages && book.stages.output;
  if (output && Array.isArray(output.missing)) {
    output.missing = output.missing.filter(function (page) { return page !== aligned; });
  }
  syncBookStageCounts(book);
  syncAuditSummary(auditState);
}

function patchRepairEvents(events, alignedPage) {
  if (typeof alignedPage !== "number") return events;
  return events.map(function (ev) {
    if (ev.aligned_page == null) return ev;
    return Object.assign({}, ev, { aligned_page: alignedPage });
  });
}

function finishRepairContext() {
  if (!repairContext) return;
  if (repairContext.type === "page") {
    markPageRepaired(repairContext.sha, repairContext.aligned);
  } else if (repairContext.type === "all") {
    repairContext.alignedPages.forEach(function (aligned) {
      markPageRepaired(repairContext.sha, aligned);
    });
  }
  repairContext = null;
}

function isEnabled() {
  return enabled;
}

function setEnabled(next) {
  enabled = !!next;
  try {
    if (enabled) localStorage.setItem(STORAGE_KEY, "1");
    else localStorage.removeItem(STORAGE_KEY);
  } catch {}
  document.body.classList.toggle("ingest-mock-active", enabled);
  window.dispatchEvent(new CustomEvent("librarain-mock-mode", { detail: { enabled } }));
}

function loadSavedEnabled() {
  try {
    if (localStorage.getItem(STORAGE_KEY) === "1") setEnabled(true);
  } catch {}
  if (new URLSearchParams(location.search).get("mock") === "1") setEnabled(true);
}

function pageRenderUrl(alignedPage) {
  return pageSvgDataUrl(alignedPage);
}

async function apiFetch(url, options) {
  const path = parsePath(url);
  const method = ((options && options.method) || "GET").toUpperCase();

  if (path === "/api/admin/book-pages-audit" && method === "GET") {
    const payload = JSON.parse(JSON.stringify(await ensureAuditState()));
    const params = new URL(url, location.origin).searchParams;
    const shaFilter = (params.get("source_sha256") || "").trim().toLowerCase();
    if (shaFilter && payload.books) {
      return jsonResponse(Object.assign({}, payload, {
        books: payload.books.filter(function (book) {
          return String(book.source_sha256 || "").toLowerCase() === shaFilter;
        }),
      }));
    }
    return jsonResponse(payload);
  }

  if (path === "/api/admin/book-pages/transcript" && method === "GET") {
    const params = new URL(url, location.origin).searchParams;
    const aligned = parseInt(params.get("aligned_page") || "0", 10);
    const transcripts = await loadFixture("transcripts.json");
    const fallback = transcripts.default || {};
    const text = transcriptForPage(aligned, transcripts);
    return jsonResponse({
      ok: true,
      source_sha256: MOCK_SHA,
      aligned_page: aligned,
      stage: fallback.stage || "stage3Editor",
      text: text,
      producer_model: fallback.producer_model || "mock-model",
    });
  }

  if (path === "/api/admin/book-pages/transcript/confirm" && method === "POST") {
    const body = JSON.parse(String(options.body || "{}"));
    const aligned = parseInt(body.aligned_page, 10);
    if (!Number.isFinite(aligned) || aligned < 1) {
      return jsonResponse({ ok: false, error: "aligned_page must be a positive integer" }, 400);
    }
    const text = typeof body.text === "string" ? body.text : "";
    transcriptOverrides.set(aligned, text);
    markPageReviewConfirmed(body.source_sha256 || MOCK_SHA, aligned);
    return jsonResponse({ ok: true, result: { aligned_page: aligned } });
  }

  if (path === "/api/admin/book-pages/repair" && method === "POST") {
    const body = JSON.parse(String(options.body || "{}"));
    repairContext = {
      type: "page",
      sha: body.source_sha256 || MOCK_SHA,
      aligned: body.aligned_page,
    };
    return jsonResponse({
      ok: true,
      job_id: "mock-repair-page",
      events_url: "mock://fixture/sse-repair-page.json",
    }, 202);
  }

  if (path === "/api/admin/book-pages/repair-all" && method === "POST") {
    const body = JSON.parse(String(options.body || "{}"));
    repairContext = {
      type: "all",
      sha: body.source_sha256 || MOCK_SHA,
      alignedPages: (body.gap_pages || []).map(function (entry) {
        return entry.aligned;
      }).filter(function (aligned) {
        return typeof aligned === "number" && aligned > 0;
      }),
    };
    return jsonResponse({
      ok: true,
      job_id: "mock-repair-all",
      events_url: "mock://fixture/sse-repair-all.json",
    }, 202);
  }

  if (path === "/api/ingest/submit" && method === "POST") {
    return jsonResponse({
      ok: true,
      job_id: "mock-ingest",
      events_url: "mock://fixture/sse-ingest-partial.json",
    }, 202);
  }

  return jsonResponse({ ok: false, error: "Mock non implementato per " + path }, 501);
}

async function watchRepairSse(eventsUrl, onEvent) {
  if (!String(eventsUrl || "").startsWith("mock://fixture/")) {
    throw new Error("URL SSE mock non valido");
  }
  const name = eventsUrl.replace("mock://fixture/", "");
  let events = await loadFixture(name);
  if (repairContext && repairContext.type === "page") {
    events = patchRepairEvents(events, repairContext.aligned);
  }
  for (let i = 0; i < events.length; i++) {
    onEvent(events[i]);
    await new Promise(function (resolve) { setTimeout(resolve, 80); });
  }
  onEvent({ status: "done", result: { ok: true, mock: true } });
  finishRepairContext();
}

async function loadScenarioFixture(name) {
  return loadFixture(name);
}

window.__librarainMock = {
  isEnabled: isEnabled,
  setEnabled: setEnabled,
  loadSavedEnabled: loadSavedEnabled,
  resetAuditState: resetAuditState,
  pageRenderUrl: pageRenderUrl,
  apiFetch: apiFetch,
  watchRepairSse: watchRepairSse,
  loadScenarioFixture: loadScenarioFixture,
  MOCK_SHA: MOCK_SHA,
};
