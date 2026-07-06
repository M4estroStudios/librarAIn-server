import { apiJson, apiToken, articleUrl } from "./api.js";

const WATCHED_KEY = "librarainDashboardWatchedJobs";
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
  stage1_glm_ocr: "Stage 1 — GLM OCR",
  stage2_vision: "Stage 2 Vision",
  stage3_editor: "Stage 3 Editor",
  polyindex_toc: "Polyindex TOC.json",
  polyindex_index: "Polyindex INDEX.json",
  time_index: "Polyindex TIME_INDEX.json",
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
let jobsApiOptions = {};

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function urlWithToken(url) {
  const token = apiToken();
  if (!token || !url) return url;
  return url + (url.indexOf("?") >= 0 ? "&" : "?") + "token=" + encodeURIComponent(token);
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

export function trackJob(jobId, meta = {}) {
  if (!jobId) return;
  const watched = loadWatched();
  if (!watched.some((item) => item.job_id === jobId)) {
    watched.unshift({ job_id: jobId, ...meta, tracked_at: Date.now() });
    saveWatched(watched);
  }
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
  try {
    const data = await apiJson("/api/system/jobs/" + encodeURIComponent(jobId), jobsApiOptions);
    return data.job || null;
  } catch {
    return null;
  }
}

function ensureJobWatched(jobId) {
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
  const es = new EventSource(urlWithToken(eventsUrl));
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

function resolveJobPhases(job) {
  if (isResearchArticleJob(job)) {
    return resolveResearchPhases(job);
  }
  const phases = normalizeJobPhases(job);
  return phases.filter(function (phase, index) {
    if (phase.status !== "pending") return true;
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
  if (phases.some((p) => p.phase === "stage1_glm_ocr")) return true;
  const renderDone = phases.some((p) => p.phase === "render" && p.status === "done");
  const classicPending = phases.some(
    (p) =>
      (p.phase === "stage1_ocr" || p.phase === "stage2_vision" || p.phase === "stage3_editor") &&
      p.status === "pending" &&
      (p.done || 0) === 0
  );
  return renderDone && classicPending && job.is_active;
}

function normalizeJobPhases(job) {
  let phases = Array.isArray(job.phases) ? job.phases.slice() : [];
  if (!jobUsesGlmOcrPipeline(job)) return phases;
  phases = phases.filter(
    (p) => !["stage1_ocr", "stage2_vision", "stage3_editor"].includes(p.phase)
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
    else phases.push(glmPhase);
  }
  return phases;
}

function renderStageSegmentsBar(segments) {
  if (!Array.isArray(segments) || !segments.length) return "";
  const parts = segments.map(function (segment) {
    const status = segment.status || "pending";
    return '<span class="stage-segment stage-segment-' + escapeHtml(status) + '" title="' + escapeHtml(jobPhaseLabel(segment.phase, segment.phase)) + '"></span>';
  });
  const done = segments.filter(function (s) { return s.status === "done"; }).length;
  return (
    '<div class="stage-segments-bar" aria-label="Progresso stage ' + done + '/' + segments.length + '">' +
    parts.join("") +
    '<span class="stage-segments-label">' + done + "/" + segments.length + " stage</span>" +
    "</div>"
  );
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

function collectBatchChildIds() {
  const childIds = new Set();
  jobsById.forEach(function (job) {
    if (job.job_kind !== "research_batch" || !Array.isArray(job.request_ids)) return;
    job.request_ids.forEach(function (id) {
      childIds.add(id);
    });
  });
  return childIds;
}

function renderActiveJobCard(job, options) {
  const nested = options && options.nested;
  const isBatch = !nested && job.job_kind === "research_batch";
  const kind = JOB_KIND_LABELS[job.job_kind] || job.job_kind || "Job";
  const globalStep = typeof job.global_step === "number" ? job.global_step : 0;
  const globalTotal = typeof job.global_total === "number" ? job.global_total : 0;
  const visiblePhases = resolveJobPhases(job);
  const stageSegments = Array.isArray(job.stage_segments) ? job.stage_segments : [];
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
  if (isBatch && stageSegments.length) {
    inner += renderStageSegmentsBar(stageSegments);
  } else if (globalTotal > 0 && !isBatch) {
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
      '<div class="active-job-actions"><a href="' +
      escapeHtml(articleHref) +
      '" target="_blank" rel="noopener">Apri articolo</a></div>';
  }

  if (isBatch) {
    return (
      '<details class="batch-job-details' + (job.is_active ? "" : " batch-job-finished") + '" data-job-id="' + escapeHtml(job.job_id || "") + '">' +
      "<summary>" + inner + "</summary>" +
      (job.request_ids && job.request_ids.length
        ? '<div class="batch-job-expanded">' +
          job.request_ids.map(function (childId) {
            const child = jobsById.get(childId);
            return child ? renderActiveJobCard(child, { nested: true }) : "";
          }).join("") +
          "</div>"
        : "") +
      "</details>"
    );
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
  const batchChildIds = collectBatchChildIds();
  const jobs = Array.from(jobsById.values())
    .filter(function (job) {
      return !batchChildIds.has(job.job_id);
    })
    .sort(function (a, b) {
    const aActive = a.is_active ? 0 : 1;
    const bActive = b.is_active ? 0 : 1;
    if (aActive !== bActive) return aActive - bActive;
    return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
  });
  if (!jobs.length) {
    root.innerHTML = '<div class="active-jobs-empty">Nessun job in questa sessione.</div>';
  } else {
    root.innerHTML = jobs.map(renderActiveJobCard).join("");
  }
  updateJobsHeading(countActiveJobs());
  notifyJobsRefresh();
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
    const data = await apiJson("/api/system/jobs?limit=30&include_finished=1", jobsApiOptions);
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
      const childIds = job.request_ids || [];
      childIds.forEach((id) => {
        watchedIds.add(id);
        if (!jobsById.has(id)) ensureJobWatched(id);
      });
    }

    for (const item of watched) {
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
  if (!getJobsRoot()) return;
  jobsApiOptions = options.noAuthPrompt ? { noAuthPrompt: true } : {};
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
