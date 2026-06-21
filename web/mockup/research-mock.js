const FIXTURE_BASE = "/mockup/fixtures/";
const STORAGE_KEY = "librarainResearchDebugMock";

let enabled = false;
let catalogState = null;
let statusState = null;
let missingState = null;
const generateJobs = new Map();

function jsonResponse(payload, status) {
  return new Response(JSON.stringify(payload), {
    status: status || 200,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

function parsePath(url) {
  try {
    return new URL(url, location.origin).pathname;
  } catch {
    return String(url || "").split("?")[0];
  }
}

async function loadFixture(name) {
  if (window.__librarainMockFixtures && window.__librarainMockFixtures[name]) {
    return JSON.parse(JSON.stringify(window.__librarainMockFixtures[name]));
  }
  const res = await fetch(FIXTURE_BASE + name);
  if (!res.ok) throw new Error("Fixture " + name + " non trovato");
  return res.json();
}

function normalizeSearch(text) {
  return String(text || "")
    .trim()
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");
}

async function ensureCatalogState() {
  if (!catalogState) {
    catalogState = await loadFixture("research-catalog.json");
  }
  return catalogState;
}

async function ensureStatusState() {
  if (!statusState) {
    statusState = await loadFixture("research-status.json");
  }
  return statusState;
}

async function ensureMissingState() {
  if (!missingState) {
    missingState = await loadFixture("research-missing.json");
  }
  return missingState;
}

function syncStatusFromCatalog() {
  if (!statusState || !catalogState) return;
  const articles = catalogState.articles || {};
  const complete = Object.values(articles).filter(function (entry) {
    return entry && !entry.no_material;
  }).length;
  statusState.articles_count = complete;
  statusState.missing_count = Math.max(0, statusState.total_subjects - complete);
}

function articleHtml(title, body, noMaterial) {
  const notice = noMaterial
    ? '<p class="notice">Materiale insufficiente: nessuna fonte pertinente disponibile.</p>'
    : "";
  return (
    "<!DOCTYPE html><html lang=\"it\"><head><meta charset=\"utf-8\">" +
    "<title>" + title + " — librarAIn</title>" +
    "<style>body{font-family:system-ui,sans-serif;max-width:46rem;margin:0 auto;padding:1.5rem;" +
    "background:#1e1e1e;color:#d4d4d4;line-height:1.55;}" +
    "a{color:#4ec9b0;}.notice{color:#f0ad4e;}</style></head><body>" +
    "<p><a href=\"/ricerca.html\">← Ricerca</a></p>" +
    "<h1>" + title + "</h1>" + notice + body + "</body></html>"
  );
}

function mockArticleBody(pohId, label, noMaterial) {
  if (noMaterial) {
    return (
      "<p>La biblioteca indicizzata non contiene pagine candidate sufficienti " +
      "per rispondere alla query con fonti verificabili.</p>" +
      "<p><strong>Query:</strong> " + label + "</p>"
    );
  }
  return (
    "<p><strong>" + label + "</strong> — articolo mock generato per review UI.</p>" +
    "<p>Contenuto enciclopedico di esempio con citazioni simulate. " +
    "Nessuna pipeline LLM reale.</p>" +
    "<h2>Cronologia</h2><table border=\"1\" cellpadding=\"6\">" +
    "<tr><th>Periodo</th><th>Evento</th></tr>" +
    "<tr><td>1271</td><td>Evento mock per " + label + "</td></tr></table>"
  );
}

function publishMockArticle(pohId, label, noMaterial) {
  const displayTitle = noMaterial ? "Materiale insufficiente" : label;
  const snippetSource = noMaterial
    ? "Materiale insufficiente La biblioteca indicizzata non contiene pagine candidate sufficienti per rispondere alla query con fonti verificabili. Query: " + label
    : label + " " + label + " — articolo mock generato per review UI.";
  if (!catalogState.articles) catalogState.articles = {};
  catalogState.articles[pohId] = {
    poh_id: pohId,
    title: displayTitle,
    snippet: snippetSource.slice(0, 180),
    url: "/articolo/" + pohId + ".html",
    request_id: "mock-gen-" + pohId,
    skipped_llm: noMaterial,
    no_material: noMaterial,
    generated_at: new Date().toISOString(),
  };
  if (noMaterial) return;
  missingState.missing = (missingState.missing || []).filter(function (entry) {
    return entry.poh_id !== pohId;
  });
  missingState.count = missingState.missing.length;
  syncStatusFromCatalog();
}

function searchCatalog(query) {
  const q = normalizeSearch(query);
  const results = [];
  Object.keys(catalogState.articles || {}).forEach(function (pohId) {
    const meta = catalogState.articles[pohId];
    if (!meta || meta.no_material) return;
    const hay = normalizeSearch(
      (meta.title || "") + " " + (meta.snippet || "") + " " + pohId
    );
    if (q.length >= 2 && hay.indexOf(q) < 0) {
      const tokens = q.split(/\s+/).filter(function (token) { return token.length >= 2; });
      if (!tokens.length || !tokens.every(function (token) { return hay.indexOf(token) >= 0; })) {
        return;
      }
    }
    results.push({
      poh_id: pohId,
      title: meta.title,
      snippet: meta.snippet,
      url: meta.url,
    });
  });
  return results;
}

function startGenerateJob(targets) {
  const jobId = "mock-research-" + Math.random().toString(16).slice(2, 10);
  const total = targets.length;
  generateJobs.set(jobId, {
    jobId: jobId,
    targets: targets.slice(),
    done: 0,
    total: total,
    status: total ? "running" : "succeeded",
    errors: [],
    startedAt: Date.now(),
  });
  return jobId;
}

function advanceGenerateJob(jobId) {
  const job = generateJobs.get(jobId);
  if (!job || job.status !== "running") return job;
  const elapsed = Date.now() - job.startedAt;
  const expectedDone = Math.min(job.total, Math.floor(elapsed / 700) + 1);
  while (job.done < expectedDone && job.done < job.total) {
    const target = job.targets[job.done];
    const noMaterial = target.poh_id === "abruzzo" || target.poh_id === "accio";
    publishMockArticle(target.poh_id, target.label || target.poh_id, noMaterial);
    job.done += 1;
  }
  if (job.done >= job.total) job.status = "succeeded";
  return job;
}

export function resetResearchState() {
  catalogState = null;
  statusState = null;
  missingState = null;
  generateJobs.clear();
}

export function isEnabled() {
  return enabled;
}

export function setEnabled(next) {
  enabled = !!next;
  try {
    if (enabled) localStorage.setItem(STORAGE_KEY, "1");
    else localStorage.removeItem(STORAGE_KEY);
  } catch {}
  document.body.classList.toggle("research-mock-active", enabled);
  window.dispatchEvent(new CustomEvent("librarain-research-mock-mode", { detail: { enabled } }));
}

export function loadSavedEnabled() {
  try {
    if (localStorage.getItem(STORAGE_KEY) === "1") setEnabled(true);
  } catch {}
  if (new URLSearchParams(location.search).get("mock") === "1") setEnabled(true);
}

export async function apiFetch(url, options) {
  const path = parsePath(url);
  const method = ((options && options.method) || "GET").toUpperCase();
  const params = new URL(url, location.origin).searchParams;

  if (path === "/api/research/status" && method === "GET") {
    await ensureCatalogState();
    await ensureStatusState();
    syncStatusFromCatalog();
    return jsonResponse(Object.assign({ ok: true }, statusState));
  }

  if (path === "/api/research/books" && method === "GET") {
    const data = await loadFixture("research-books.json");
    return jsonResponse({ ok: true, books: data.books || [] });
  }

  if (path === "/api/research/missing" && method === "GET") {
    await ensureMissingState();
    return jsonResponse({
      ok: true,
      missing: missingState.missing || [],
      count: missingState.count || 0,
    });
  }

  if (path === "/api/research/search" && method === "GET") {
    await ensureCatalogState();
    const q = (params.get("q") || "").trim();
    if (q.length < 2) {
      return jsonResponse({ ok: false, error: "query must be at least 2 characters" }, 400);
    }
    const results = searchCatalog(q);
    return jsonResponse({ ok: true, query: q, results: results, count: results.length });
  }

  if (path === "/api/research/generate" && method === "POST") {
    await ensureMissingState();
    const body = JSON.parse(String((options && options.body) || "{}"));
    let targets = (missingState.missing || []).slice();
    if (Array.isArray(body.poh_ids) && body.poh_ids.length) {
      targets = body.poh_ids.map(function (pohId) {
        const found = (missingState.missing || []).find(function (entry) {
          return entry.poh_id === pohId;
        });
        return found || { poh_id: pohId, label: pohId };
      });
    } else if (body.book_sha) {
      targets = targets.slice(0, 2);
    }
    const jobId = startGenerateJob(targets);
    return jsonResponse({
      ok: true,
      job_id: jobId,
      total: targets.length,
      status_url: "/api/research/generate/status?job_id=" + encodeURIComponent(jobId),
    }, 202);
  }

  if (path === "/api/research/generate/status" && method === "GET") {
    const jobId = params.get("job_id") || "";
    let job = generateJobs.get(jobId);
    if (!job) {
      return jsonResponse({ ok: false, error: "job not found" }, 404);
    }
    job = advanceGenerateJob(jobId);
    return jsonResponse({
      ok: true,
      job_id: job.jobId,
      done: job.done,
      total: job.total,
      status: job.status,
      errors: job.errors,
      generated: [],
      request_ids: [],
    });
  }

  if (path.indexOf("/articolo/") === 0 && path.endsWith(".html") && method === "GET") {
    await ensureCatalogState();
    const pohId = path.slice("/articolo/".length, -".html".length);
    const meta = (catalogState.articles || {})[pohId];
    if (!meta) {
      return new Response("Not Found", { status: 404 });
    }
    const html = articleHtml(
      meta.title,
      mockArticleBody(pohId, meta.title, !!meta.no_material),
      !!meta.no_material
    );
    return new Response(html, {
      status: 200,
      headers: { "Content-Type": "text/html; charset=utf-8" },
    });
  }

  return jsonResponse({ ok: false, error: "Research mock non implementato per " + path }, 501);
}

export async function loadScenarioFixture(name) {
  return loadFixture(name);
}
