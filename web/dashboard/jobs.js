import { apiJson, articleUrl } from "./api.js";

const WATCHED_KEY = "librarainDashboardWatchedJobs";
const MISSING_KEY = "librarainDashboardMissingJobs";
const TERMINAL = new Set(["done", "error", "succeeded", "failed"]);
const SSE_EVENT_TYPES = [
  "started",
  "progress",
  "page_progress",
  "page_skipped",
  "page_failed",
  "completed",
  "failed",
  "pipeline_total",
  "waiting",
  "plan",
  "info",
];

const JOB_PHASE_LABELS = {
  validation: "Validazione",
  gate_hash: "Hash gate",
  pdf_alignment: "Allineamento PDF",
  page_enumeration: "Enumerazione pagine",
  render: "Render PDF",
  stage1_ocr: "Stage 1 OCR",
  stage1_glm_ocr: "Stage 1+2 — OCR + Vision",
  stage2_vision: "Stage 2 Vision",
  stage3_editor: "Stage 3 Editor",
  polyindex_toc: "Polyindex TOC.json",
  polyindex_index: "Polyindex INDEX.json",
  time_index: "Polyindex TIME_INDEX.json",
  polyindex_biblio: "Polyindex BIBLIO.json",
  page_repair: "Preparazione riparazione",
  gaps_repair: "Riparazione lacune",
  queue: "In coda",
  research_collect: "Raccolta fonti",
  research_filter: "Sfoltimento fonti",
  research_article: "Generazione bozza",
  research_poh_links: "Collegamenti POH",
  research_timeline: "Cronologia",
  research_verify: "Verifica",
  research_prefilter: "Prefiltro research",
  research_postprocess: "Post-process",
  research_finalize: "Revisione finale",
  research: "Research",
  research_batch: "Generazione articoli",
};

const JOB_KIND_LABELS = {
  ingest: "Ingest",
  repair: "Riparazione",
  biblio: "Bibliografia",
  research: "Research",
  research_batch: "Batch articoli",
};

const RESEARCH_DISPLAY_PHASES = [
  "research_collect",
  "research_filter",
  "research_article",
  "research_poh_links",
  "research_timeline",
  "research_verify",
];

const jobsById = new Map();
const sseConnections = new Map();
const refetchTimers = new Map();
const expandedBatchIds = new Set();
const missingJobIds = new Set(loadMissingJobIds());
let jobsApiOptions = {};
let jobsUiBound = false;

function loadMissingJobIds() {
  try {
    const raw = sessionStorage.getItem(MISSING_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
  } catch {
    return [];
  }
}

function saveMissingJobIds() {
  try {
    sessionStorage.setItem(MISSING_KEY, JSON.stringify(Array.from(missingJobIds).slice(0, 200)));
  } catch {}
}

function rememberMissingJob(jobId) {
  if (!jobId || missingJobIds.has(jobId)) return;
  missingJobIds.add(jobId);
  saveMissingJobIds();
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function loadWatched() {
  try {
    const raw = sessionStorage.getItem(WATCHED_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveWatched(items) {
  try {
    sessionStorage.setItem(WATCHED_KEY, JSON.stringify(items.slice(0, 40)));
  } catch {}
}

function forgetWatchedJob(jobId) {
  if (!jobId) return;
  const watched = loadWatched().filter(function (item) {
    return item.job_id !== jobId;
  });
  saveWatched(watched);
  jobsById.delete(jobId);
  disconnectJobSSE(jobId);
}

function rememberWatchedJob(jobId, meta) {
  if (!jobId) return;
  const watched = loadWatched();
  if (watched.some((item) => item.job_id === jobId)) return;
  watched.unshift({ job_id: jobId, ...meta, tracked_at: Date.now() });
  saveWatched(watched);
}

export function trackJob(jobId, meta = {}) {
  rememberWatchedJob(jobId, meta);
  ensureJobWatched(jobId);
  notifyJobsRefresh();
}

function getJobsRoot() {
  return document.getElementById("active-jobs-root");
}

function notifyJobsRefresh() {
  if (typeof window.reportEmbedHeight === "function") window.reportEmbedHeight();
  try {
    window.parent.postMessage({ type: "librarain-jobs-refresh" }, "*");
    const adminFrame = document.getElementById("admin-frame");
    adminFrame?.contentWindow?.postMessage({ type: "librarain-jobs-refresh" }, "*");
  } catch {}
}

async function fetchJobSummary(jobId) {
  if (!jobId || missingJobIds.has(jobId)) return null;
  try {
    const data = await apiJson("/api/system/jobs/" + encodeURIComponent(jobId), jobsApiOptions);
    return data.job || null;
  } catch {
    rememberMissingJob(jobId);
    forgetWatchedJob(jobId);
    return null;
  }
}

function ensureJobWatched(jobId) {
  if (!jobId || missingJobIds.has(jobId)) return;
  fetchJobSummary(jobId).then((job) => {
    if (!job) return;
    jobsById.set(jobId, job);
    renderJobs();
    if (job.is_active) connectJobSSE(job);
  });
}

function scheduleRefetch(jobId) {
  if (refetchTimers.has(jobId)) return;
  const timer = window.setTimeout(async () => {
    refetchTimers.delete(jobId);
    const job = await fetchJobSummary(jobId);
    if (job) {
      jobsById.set(jobId, job);
      renderJobs();
      if (!job.is_active) disconnectJobSSE(jobId);
    }
  }, 250);
  refetchTimers.set(jobId, timer);
}

function connectJobSSE(job) {
  const jobId = job.job_id;
  if (sseConnections.has(jobId) || TERMINAL.has(job.status)) return;
  const eventsUrl = job.system_events_url || job.events_url;
  if (!eventsUrl) return;
  const es = new EventSource(eventsUrl);
  sseConnections.set(jobId, es);
  const handler = () => scheduleRefetch(jobId);
  SSE_EVENT_TYPES.forEach((type) => es.addEventListener(type, handler));
  es.onmessage = handler;
  es.addEventListener("done", handler);
  es.addEventListener("succeeded", handler);
  es.addEventListener("failed", handler);
  es.addEventListener("error", (event) => {
    if (event.data) handler();
  });
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) disconnectJobSSE(jobId);
  };
}

function disconnectJobSSE(jobId) {
  const es = sseConnections.get(jobId);
  if (es) {
    es.close();
    sseConnections.delete(jobId);
  }
}

function formatJobPercent(done, total) {
  if (!total) return 0;
  return Math.min(100, Math.round((done / total) * 100));
}

function formatJobStatsLine(done, total) {
  return formatJobPercent(done, total) + "% (" + done + "/" + total + ")";
}

function jobPhaseLabel(phase, fallback) {
  return JOB_PHASE_LABELS[phase] || fallback || phase || "Fase";
}

function isResearchArticleJob(job) {
  if (job.job_kind === "research") return true;
  const phases = Array.isArray(job.phases) ? job.phases : [];
  return phases.some(function (phase) {
    return RESEARCH_DISPLAY_PHASES.indexOf(phase.phase) >= 0;
  });
}

function resolveResearchPhases(job) {
  const phases = normalizeJobPhases(job);
  const byPhase = new Map(phases.map(function (phase) {
    return [phase.phase, phase];
  }));
  const planTotals = job.research_phase_totals || {};
  return RESEARCH_DISPLAY_PHASES.map(function (phaseId) {
    const existing = byPhase.get(phaseId);
    if (existing) {
      if (planTotals[phaseId] && (!existing.total || existing.total === 1)) {
        return Object.assign({}, existing, { total: planTotals[phaseId] });
      }
      return existing;
    }
    return {
      phase: phaseId,
      status: "pending",
      done: 0,
      total: planTotals[phaseId] || 1,
    };
  });
}

const POLYINDEX_PHASES = new Set([
  "polyindex_toc",
  "polyindex_index",
  "time_index",
  "polyindex_biblio",
]);

function resolveJobPhases(job) {
  if (isResearchArticleJob(job)) {
    return resolveResearchPhases(job);
  }
  const phases = normalizeJobPhases(job);
  const stage3 = phases.find(function (phase) {
    return phase.phase === "stage3_editor";
  });
  const showAllPolyindex =
    !!stage3 && (stage3.status === "active" || stage3.status === "done");
  return phases.filter(function (phase, index) {
    if (phase.status !== "pending") return true;
    if (showAllPolyindex && POLYINDEX_PHASES.has(phase.phase)) return true;
    const prev = phases[index - 1];
    return prev && (prev.status === "active" || prev.status === "done");
  });
}

function resolveGlmOcrPhase(job) {
  const phases = Array.isArray(job.phases) ? job.phases : [];
  const renderPhase = phases.find((p) => p.phase === "render");
  const enumPhase = phases.find((p) => p.phase === "page_enumeration");
  let pageTotal = renderPhase?.total || renderPhase?.done || 0;
  if (!pageTotal && enumPhase?.detail) {
    const match = String(enumPhase.detail).match(/(\d+)/);
    if (match) pageTotal = parseInt(match[1], 10);
  }
  pageTotal = Math.max(1, pageTotal || 1);
  const globalStep = job.global_step || 0;
  const globalTotal = job.global_total || 0;
  const pageStages =
    globalTotal > 0 && pageTotal > 0
      ? Math.max(1, Math.round((globalTotal - 1) / pageTotal))
      : 2;
  const prefix = Math.max(0, globalTotal - pageTotal * pageStages);
  const done = Math.min(
    pageTotal,
    Math.max(0, Math.floor((globalStep - prefix) / pageStages))
  );
  return {
    phase: "stage1_glm_ocr",
    status: job.is_active ? "active" : "done",
    done,
    total: pageTotal,
    detail: job.detail || null,
  };
}

function jobUsesGlmOcrPipeline(job) {
  if (job.current_phase === "stage1_glm_ocr") return true;
  const phases = Array.isArray(job.phases) ? job.phases : [];
  return phases.some((p) => p.phase === "stage1_glm_ocr");
}

function normalizeJobPhases(job) {
  let phases = Array.isArray(job.phases) ? job.phases.slice() : [];
  if (!jobUsesGlmOcrPipeline(job)) return phases;
  phases = phases.filter(
    (p) => !["stage1_ocr", "stage2_vision"].includes(p.phase)
  );
  const glmPhase = resolveGlmOcrPhase(job);
  const existing = phases.find((p) => p.phase === "stage1_glm_ocr");
  if (existing) {
    if ((existing.done || 0) === 0 && existing.status === "pending") {
      Object.assign(existing, glmPhase);
    }
  } else {
    const insertAt = phases.findIndex((p) => p.phase === "render");
    if (insertAt >= 0) phases.splice(insertAt + 1, 0, glmPhase);
    else {
      const editorAt = phases.findIndex((p) => p.phase === "stage3_editor");
      if (editorAt >= 0) phases.splice(editorAt, 0, glmPhase);
      else phases.push(glmPhase);
    }
  }
  return phases;
}

function renderJobProgressRow(label, done, total, status, globalRow) {
  const max = Math.max(1, total || 1);
  const value = Math.min(done || 0, max);
  const statusClass =
    status === "active" ? " active" : status === "done" ? " done" : status === "failed" ? " failed" : "";
  const rowClass = "job-progress-row" + (globalRow ? " global-row" : "");
  return (
    '<div class="' +
    rowClass +
    '">' +
    '<span class="job-progress-label' +
    statusClass +
    '">' +
    escapeHtml(label) +
    "</span>" +
    '<progress value="' +
    value +
    '" max="' +
    max +
    '"></progress>' +
    '<span class="job-progress-stats">' +
    formatJobStatsLine(value, max) +
    "</span>" +
    "</div>"
  );
}

function renderPhaseBlock(phase) {
  const label = jobPhaseLabel(phase.phase, phase.phase);
  let html = renderJobProgressRow(label, phase.done, phase.total, phase.status, false);
  const detail =
    phase.detail ||
    (phase.status === "active" ? "in corso…" : "");
  if (detail) {
    html +=
      '<div class="job-phase-detail">' + escapeHtml(detail) + "</div>";
  }
  return html;
}

function isBatchChildJob(jobId) {
  if (!jobId) return false;
  let found = false;
  jobsById.forEach(function (job) {
    if (job.job_kind !== "research_batch") return;
    if (job.current_request_id === jobId) found = true;
    (job.request_ids || []).forEach(function (id) {
      if (id === jobId) found = true;
    });
  });
  return found;
}

function collectBatchChildIds() {
  const childIds = new Set();
  jobsById.forEach(function (job) {
    if (job.job_kind !== "research_batch") return;
    (job.request_ids || []).forEach(function (id) {
      childIds.add(id);
    });
    if (job.current_request_id) childIds.add(job.current_request_id);
  });
  return childIds;
}

function toggleBatchExpanded(jobId) {
  if (!jobId) return;
  if (expandedBatchIds.has(jobId)) expandedBatchIds.delete(jobId);
  else expandedBatchIds.add(jobId);
  renderJobs();
}

async function resumeBatch(jobId) {
  if (!jobId) return;
  await apiJson("/api/research/generate/resume", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId }),
  });
  trackJob(jobId, { job_kind: "research_batch" });
  expandedBatchIds.add(jobId);
  await refreshJobsList();
}

async function abortBatch(jobId) {
  if (!jobId) return;
  if (!window.confirm("Annullare questo batch in sospeso? Non potrai riprenderlo.")) return;
  const job = jobsById.get(jobId);
  if (job) {
    (job.request_ids || []).forEach(function (id) {
      rememberMissingJob(id);
    });
    if (job.current_request_id) rememberMissingJob(job.current_request_id);
  }
  await apiJson("/api/research/generate/abort", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId }),
  });
  forgetWatchedJob(jobId);
  expandedBatchIds.delete(jobId);
  jobsById.delete(jobId);
  disconnectJobSSE(jobId);
  await refreshJobsList();
}

function handleJobsRootClick(event) {
  const root = getJobsRoot();
  if (!root || !root.contains(event.target)) return;
  const resumeBtn = event.target.closest("[data-batch-resume]");
  if (resumeBtn) {
    event.preventDefault();
    event.stopPropagation();
    resumeBatch(resumeBtn.getAttribute("data-batch-resume")).catch(function () {});
    return;
  }
  const abortBtn = event.target.closest("[data-batch-abort]");
  if (abortBtn) {
    event.preventDefault();
    event.stopPropagation();
    abortBatch(abortBtn.getAttribute("data-batch-abort")).catch(function () {});
    return;
  }
  const toggle = event.target.closest("[data-batch-toggle]");
  if (toggle) {
    event.preventDefault();
    event.stopPropagation();
    toggleBatchExpanded(toggle.getAttribute("data-batch-toggle"));
    return;
  }
  const summary = event.target.closest(".batch-job-summary");
  if (summary && !event.target.closest(".active-job-open")) {
    event.preventDefault();
    toggleBatchExpanded(summary.getAttribute("data-batch-id"));
    return;
  }
  const btn = event.target.closest(".active-job-open");
  if (!btn) return;
  const href = btn.getAttribute("data-href");
  if (href) window.open(href, "_blank", "noopener,noreferrer");
}

function resolveBatchCurrentChild(job) {
  const currentId = job.current_request_id;
  if (currentId) {
    const child = jobsById.get(currentId);
    if (child) return child;
  }
  const requestIds = job.request_ids || [];
  for (let i = requestIds.length - 1; i >= 0; i -= 1) {
    const child = jobsById.get(requestIds[i]);
    if (child && child.is_active) return child;
  }
  return null;
}

function renderBatchChildCompact(job) {
  const title = job.title || job.poh_label || job.poh_id || "Articolo";
  const statusLabel = job.display_status_label || job.status || "completato";
  const statusClass = job.error ? " failed" : " done";
  let html =
    '<div class="batch-child-compact' +
    statusClass +
    '" data-job-id="' +
    escapeHtml(job.job_id || "") +
    '">' +
    '<span class="batch-child-compact-title">' +
    escapeHtml(title) +
    "</span>" +
    '<span class="batch-child-compact-meta">' +
    escapeHtml(statusLabel) +
    "</span>";
  const articleHref =
    job.article_url ||
    (job.poh_id && (job.status === "succeeded" || job.status === "done") ? articleUrl(job.poh_id) : null);
  if (articleHref) {
    html +=
      '<button type="button" class="secondary active-job-open batch-child-compact-open" data-href="' +
      escapeHtml(articleHref) +
      '">Apri</button>';
  }
  if (job.error) {
    html += '<div class="batch-child-compact-error">' + escapeHtml(job.error) + "</div>";
  }
  html += "</div>";
  return html;
}

function renderBatchSummaryContent(job) {
  const kind = JOB_KIND_LABELS[job.job_kind] || job.job_kind || "Job";
  const globalStep = typeof job.global_step === "number" ? job.global_step : 0;
  const globalTotal = typeof job.global_total === "number" ? job.global_total : 0;
  const currentLabel = job.poh_label || job.title || "";
  let inner =
    '<div class="active-job-header">' +
    '<span class="active-job-title">' +
    escapeHtml(kind) +
    "</span>" +
    '<span class="active-job-meta">' +
    escapeHtml(job.display_status_label || job.status || "running") +
    (job.is_active ? " · live" : "") +
    "</span>" +
    "</div>";
  if (currentLabel) {
    inner +=
      '<div class="active-job-batch-current">' +
      '<span class="active-job-batch-current-label">In corso</span> ' +
      escapeHtml(currentLabel.replace(/^Articolo:\s*/i, "")) +
      "</div>";
  }
  if (job.subtitle) {
    inner += '<div class="active-job-subtitle">' + escapeHtml(job.subtitle) + "</div>";
  }
  const detail =
    job.detail ||
    jobPhaseLabel(job.current_phase, job.current_phase_label) ||
    job.current_phase_label;
  if (detail) {
    inner += '<div class="active-job-detail">' + escapeHtml(detail) + "</div>";
  }
  if (globalTotal > 0) {
    inner += renderJobProgressRow("Articoli", globalStep, globalTotal, job.is_active ? "active" : "done", true);
  }
  if (job.resumable || job.status === "interrupted") {
    inner +=
      '<div class="active-job-actions">' +
      '<button type="button" class="secondary batch-job-resume" data-batch-resume="' +
      escapeHtml(job.job_id || "") +
      '">Riprendi batch</button>' +
      '<button type="button" class="secondary batch-job-abort" data-batch-abort="' +
      escapeHtml(job.job_id || "") +
      '">Annulla batch</button>' +
      "</div>";
  }
  return inner;
}

function renderBatchCurrentPlaceholder(job) {
  const label = (job.poh_label || job.title || "Articolo").replace(/^Articolo:\s*/i, "");
  let html =
    '<div class="active-job-card active-job-card-nested batch-job-current-placeholder">' +
    '<div class="active-job-header">' +
    '<span class="active-job-title">' +
    escapeHtml(label) +
    "</span>" +
    '<span class="active-job-meta">Caricamento fasi…</span>' +
    "</div>";
  if (job.detail) {
    html += '<div class="active-job-detail">' + escapeHtml(job.detail) + "</div>";
  }
  html += "</div>";
  return html;
}

function renderBatchExpandedContent(job) {
  const requestIds = job.request_ids || [];
  const currentId = job.current_request_id || null;
  const completedIds = requestIds.filter(function (id) {
    return id !== currentId;
  });
  let html = "";
  const currentChild = resolveBatchCurrentChild(job);
  if (currentChild && job.is_active) {
    html += '<div class="batch-job-current">' + renderActiveJobCard(currentChild, { nested: true }) + "</div>";
  } else if (job.is_active) {
    if (job.current_request_id && !jobsById.has(job.current_request_id)) {
      ensureJobWatched(job.current_request_id);
    }
    html += '<div class="batch-job-current">' + renderBatchCurrentPlaceholder(job) + "</div>";
  }
  if (completedIds.length) {
    html += '<div class="batch-job-completed">';
    completedIds.forEach(function (childId) {
      const child = jobsById.get(childId);
      if (child) html += renderBatchChildCompact(child);
    });
    html += "</div>";
  }
  if (!html) {
    html = '<div class="active-jobs-empty">Nessun articolo nel batch ancora.</div>';
  }
  return html;
}

function renderBatchJobCard(job) {
  const expanded = expandedBatchIds.has(job.job_id);
  return (
    '<div class="batch-job-details' +
    (expanded ? " batch-job-open" : "") +
    (job.is_active ? "" : " batch-job-finished") +
    '" data-job-id="' +
    escapeHtml(job.job_id || "") +
    '">' +
    '<div class="batch-job-summary" data-batch-id="' +
    escapeHtml(job.job_id || "") +
    '">' +
    '<button type="button" class="secondary batch-job-toggle" data-batch-toggle="' +
    escapeHtml(job.job_id || "") +
    '" aria-expanded="' +
    (expanded ? "true" : "false") +
    '" aria-label="' +
    (expanded ? "Comprimi batch" : "Espandi batch") +
    '">' +
    (expanded ? "▼" : "▶") +
    "</button>" +
    '<div class="batch-job-summary-body">' +
    renderBatchSummaryContent(job) +
    "</div>" +
    "</div>" +
    '<div class="batch-job-expanded">' +
    renderBatchExpandedContent(job) +
    "</div>" +
    "</div>"
  );
}

function renderActiveJobCard(job, options) {
  const nested = options && options.nested;
  const isBatch = !nested && job.job_kind === "research_batch";
  const kind = JOB_KIND_LABELS[job.job_kind] || job.job_kind || "Job";
  const globalStep = typeof job.global_step === "number" ? job.global_step : 0;
  const globalTotal = typeof job.global_total === "number" ? job.global_total : 0;
  const visiblePhases = resolveJobPhases(job);
  const cardClass =
    "active-job-card" +
    (nested ? " active-job-card-nested" : "") +
    (isBatch ? " active-job-card-batch" : "") +
    (job.is_active ? "" : " job-finished") +
    (job.error ? " job-failed" : "");
  let inner =
    '<div class="active-job-header">' +
    '<span class="active-job-title">' +
    escapeHtml(job.title || kind) +
    "</span>" +
    '<span class="active-job-meta">' +
    escapeHtml(kind) +
    " · " +
    escapeHtml(job.display_status_label || job.status || "running") +
    (job.is_active ? " · live" : "") +
    "</span>" +
    "</div>";
  if (job.subtitle) {
    inner += '<div class="active-job-subtitle">' + escapeHtml(job.subtitle) + "</div>";
  }
  const detail =
    job.detail ||
    jobPhaseLabel(job.current_phase, job.current_phase_label) ||
    job.current_phase_label;
  if (detail && !isBatch) {
    inner += '<div class="active-job-detail">' + escapeHtml(detail) + "</div>";
  }
  if (job.error) {
    inner += '<div class="active-job-error">' + escapeHtml(job.error) + "</div>";
  }
  if (globalTotal > 0) {
    inner += renderJobProgressRow("Totale", globalStep, globalTotal, job.is_active ? "active" : "done", true);
  }
  if (visiblePhases.length && !isBatch) {
    inner += '<div class="active-job-phases">';
    visiblePhases.forEach(function (phase) {
      inner += renderPhaseBlock(phase);
    });
    inner += "</div>";
  }
  const articleHref =
    job.article_url ||
    (job.poh_id && job.status === "succeeded" ? articleUrl(job.poh_id) : null);
  if (articleHref) {
    inner +=
      '<div class="active-job-actions"><button type="button" class="secondary active-job-open" data-href="' +
      escapeHtml(articleHref) +
      '">Apri articolo</button></div>';
  }

  if (isBatch) {
    return renderBatchJobCard(job);
  }

  return (
    '<div class="' +
    cardClass +
    '" data-job-id="' +
    escapeHtml(job.job_id || "") +
    '">' +
    inner +
    "</div>"
  );
}

function countActiveJobs() {
  const batchChildIds = collectBatchChildIds();
  let n = 0;
  jobsById.forEach(function (job) {
    if (job.is_active && !batchChildIds.has(job.job_id)) n += 1;
  });
  return n;
}

function renderJobs() {
  const root = getJobsRoot();
  if (!root) return;
  const jobs = Array.from(jobsById.values())
    .filter(function (job) {
      if (job.job_kind === "research_batch") {
        return job.is_active || job.status === "interrupted" || job.resumable;
      }
      if (!job.is_active) return false;
      return !isBatchChildJob(job.job_id);
    })
    .sort(function (a, b) {
    return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
  });
  if (!jobs.length) {
    root.innerHTML = '<div class="active-jobs-empty">Nessun job attivo in questa sessione.</div>';
  } else {
    try {
      root.innerHTML = jobs.map(renderActiveJobCard).join("");
    } catch (err) {
      root.innerHTML =
        '<div class="active-jobs-empty">Errore rendering job: ' +
        escapeHtml(err && err.message ? err.message : err) +
        "</div>";
    }
  }
  updateJobsHeading(countActiveJobs());
  if (typeof window.reportEmbedHeight === "function") window.reportEmbedHeight();
}

function updateJobsHeading(activeCount) {
  const heading = document.getElementById("active-jobs-heading");
  if (!heading) return;
  const total = jobsById.size;
  let suffix = "";
  if (activeCount > 0) suffix = " (" + activeCount + " attivi)";
  else if (total > 0) suffix = " (" + total + ")";
  const base = heading.dataset.baseTitle || "Job";
  heading.textContent = base + suffix;
}

async function refreshJobsList() {
  const root = getJobsRoot();
  if (!root) return;
  const watched = loadWatched();
  const watchedIds = new Set(watched.map((item) => item.job_id));
  try {
    const data = await apiJson("/api/system/jobs?limit=30&include_finished=0", jobsApiOptions);
    const activeJobs = data.jobs || [];
    const keepIds = new Set(watchedIds);
    activeJobs.forEach((job) => keepIds.add(job.job_id));

    for (const jobId of jobsById.keys()) {
      if (!keepIds.has(jobId)) {
        jobsById.delete(jobId);
        disconnectJobSSE(jobId);
      }
    }

    for (const job of activeJobs) {
      jobsById.set(job.job_id, job);
      if (job.is_active) connectJobSSE(job);
      if (job.job_kind === "research_batch" && (job.is_active || job.status === "interrupted")) {
        rememberWatchedJob(job.job_id, { job_kind: "research_batch" });
      }
      if (job.job_kind === "research_batch" && job.is_active) {
        const childIds = (job.request_ids || []).slice();
        if (job.current_request_id) childIds.push(job.current_request_id);
        childIds.forEach((id) => {
          if (missingJobIds.has(id)) return;
          if (!jobsById.has(id)) ensureJobWatched(id);
        });
      }
    }

    for (const item of watched) {
      if (missingJobIds.has(item.job_id)) continue;
      if (!jobsById.has(item.job_id)) ensureJobWatched(item.job_id);
    }
    renderJobs();
  } catch (err) {
    root.innerHTML =
      '<div class="active-jobs-empty">Job non disponibili: ' + escapeHtml(err.message) + "</div>";
    notifyJobsRefresh();
  }
}

export function initActiveJobs(options = {}) {
  const root = getJobsRoot();
  if (!root) return;
  jobsApiOptions = options.noAuthPrompt ? { noAuthPrompt: true } : {};
  if (!jobsUiBound) {
    jobsUiBound = true;
    document.addEventListener("click", handleJobsRootClick, true);
  }
  window.addEventListener("message", (event) => {
    if (event.data && event.data.type === "librarain-jobs-refresh") refreshJobsList();
  });
  const watched = loadWatched();
  watched.forEach((item) => ensureJobWatched(item.job_id));
  refreshJobsList();
  window.setInterval(function () {
    refreshJobsList();
  }, 3000);
}
