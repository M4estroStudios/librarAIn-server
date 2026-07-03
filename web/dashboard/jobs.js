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
  research_prefilter: "Prefiltro research",
  research_article: "Articolo",
  research_poh_links: "Link POH",
  research_timeline: "Timeline",
  research_postprocess: "Post-process",
  research: "Research",
  research_batch: "Generazione articoli",
};

const JOB_KIND_LABELS = {
  ingest: "Ingest",
  research: "Research",
  research_batch: "Batch articoli",
};

const PREFILTER_STEP_LABELS = {
  subject_match: "Match INDEX",
  toc_expansion: "Espansione TOC",
  time_index: "TIME_INDEX",
  merge_candidates: "Unione candidati",
  load_pages: "Caricamento testi",
  relevance_filter: "Filtro rilevanza",
};

const jobsById = new Map();
const sseConnections = new Map();
const refetchTimers = new Map();

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
    const data = await apiJson("/api/system/jobs/" + encodeURIComponent(jobId));
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

function formatPrefilterMatchDetail(step) {
  const matches = Array.isArray(step.matches) ? step.matches : [];
  if (!matches.length) return "nessun soggetto matchato";
  return matches
    .map(function (m) {
      let line = m.canonical_label || m.canonical_id || "?";
      if (m.method) line += " (" + m.method + ")";
      if (m.similarity != null) line += " " + Math.round(Number(m.similarity) * 100) + "%";
      return line;
    })
    .join(", ");
}

function formatPrefilterStepDetail(step) {
  const id = step.step || step.prefilter_step || "";
  switch (id) {
    case "subject_match": {
      const degraded = step.degraded ? " · ricerca degradata" : "";
      const ai = step.ai_used ? " · AI" : "";
      return (
        (step.subject_pages || 0) +
        " pag. in " +
        (step.subject_books || 0) +
        " libri" +
        ai +
        degraded +
        " · " +
        formatPrefilterMatchDetail(step)
      );
    }
    case "toc_expansion": {
      let toc =
        "+" +
        (step.pages_added || 0) +
        " pag. (" +
        (step.pages_before || 0) +
        "→" +
        (step.pages_after || 0) +
        ")";
      if (step.expanded_chapters) toc += " · " + step.expanded_chapters + " capitoli espansi";
      if (step.books_dropped) toc += " · " + step.books_dropped + " libri scartati";
      return toc;
    }
    case "time_index": {
      if (!step.pages_added && !step.matched_labels) return "nessun arricchimento temporale";
      let time =
        "+" +
        (step.pages_added || 0) +
        " pag. (" +
        (step.pages_before || 0) +
        "→" +
        (step.pages_after || 0) +
        ")";
      if (step.matched_labels) time += " · " + step.matched_labels + " label temporali";
      if (step.fallback_labels) time += " · " + step.fallback_labels + " fallback";
      return time;
    }
    case "merge_candidates":
      if (!step.pages_added) {
        return (step.pages_after || 0) + " pag. candidate (nessuna nuova dall'unione)";
      }
      return (
        "+" +
        (step.pages_added || 0) +
        " pag. (" +
        (step.pages_before || 0) +
        "→" +
        (step.pages_after || 0) +
        ")"
      );
    case "load_pages":
      return (
        (step.loaded_pages || 0) +
        "/" +
        (step.candidate_pages || 0) +
        " pag. caricati · " +
        (step.loaded_books || 0) +
        " libri"
      );
    case "relevance_filter":
      return (
        (step.kept_pages || 0) +
        " tenute · " +
        (step.dropped_pages || 0) +
        " scartate (su " +
        (step.input_pages || 0) +
        ")"
      );
    default:
      return step.message || "";
  }
}

function renderPrefilterSteps(steps) {
  if (!steps || !steps.length) return "";
  let html = '<div class="active-job-prefilter-steps">';
  steps.forEach(function (step) {
    const label = PREFILTER_STEP_LABELS[step.step] || step.step || "Step";
    const detail =
      formatPrefilterStepDetail(step) ||
      (step.status === "active" ? "in corso…" : step.status === "pending" ? "—" : "");
    const statusClass =
      step.status === "done" ? " done" : step.status === "active" ? " active" : "";
    html +=
      '<div class="prefilter-step' +
      statusClass +
      '">' +
      '<span class="prefilter-step-label">' +
      escapeHtml(label) +
      "</span>" +
      '<span class="prefilter-step-detail">' +
      escapeHtml(detail) +
      "</span>" +
      "</div>";
  });
  html += "</div>";
  return html;
}

function jobPhaseLabel(phase, fallback) {
  return JOB_PHASE_LABELS[phase] || fallback || phase || "Fase";
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
  if (phase.detail) {
    html +=
      '<div class="job-phase-detail">' + escapeHtml(phase.detail) + "</div>";
  }
  return html;
}

function renderActiveJobCard(job) {
  const kind = JOB_KIND_LABELS[job.job_kind] || job.job_kind || "Job";
  const globalStep = typeof job.global_step === "number" ? job.global_step : 0;
  const globalTotal = typeof job.global_total === "number" ? job.global_total : 0;
  const phases = normalizeJobPhases(job);
  const visiblePhases = phases.filter(function (phase, index) {
    if (phase.status !== "pending") return true;
    const prev = phases[index - 1];
    return prev && (prev.status === "active" || prev.status === "done");
  });
  const cardClass =
    "active-job-card" + (job.is_active ? "" : " job-finished") + (job.error ? " job-failed" : "");
  let html =
    '<div class="' +
    cardClass +
    '" data-job-id="' +
    escapeHtml(job.job_id || "") +
    '">' +
    '<div class="active-job-header">' +
    '<span class="active-job-title">' +
    escapeHtml(job.title || kind) +
    "</span>" +
    '<span class="active-job-meta">' +
    escapeHtml(kind) +
    " · " +
    escapeHtml(job.status || "running") +
  (job.is_active ? " · live" : "") +
    "</span>" +
    "</div>";
  if (job.subtitle) {
    html += '<div class="active-job-subtitle">' + escapeHtml(job.subtitle) + "</div>";
  }
  const detail =
    job.detail ||
    jobPhaseLabel(job.current_phase, job.current_phase_label) ||
    job.current_phase_label;
  if (detail) {
    html += '<div class="active-job-detail">' + escapeHtml(detail) + "</div>";
  }
  if (job.error) {
    html += '<div class="active-job-error">' + escapeHtml(job.error) + "</div>";
  }
  if (globalTotal > 0) {
    html += renderJobProgressRow("Totale", globalStep, globalTotal, job.is_active ? "active" : "done", true);
  }
  if (visiblePhases.length) {
    html += '<div class="active-job-phases">';
    visiblePhases.forEach(function (phase) {
      html += renderPhaseBlock(phase);
    });
    html += "</div>";
  }
  const prefilterSteps =
    job.prefilter_steps ||
    (function () {
      const pf = phases.find(function (p) {
        return p.phase === "research_prefilter";
      });
      return pf && pf.steps ? pf.steps : null;
    })();
  if (prefilterSteps && prefilterSteps.length) {
    html += renderPrefilterSteps(prefilterSteps);
  }
  const articleHref =
    job.article_url ||
    (job.poh_id && job.status === "succeeded" ? articleUrl(job.poh_id) : null);
  if (articleHref) {
    html +=
      '<div class="active-job-actions"><a href="' +
      escapeHtml(articleHref) +
      '" target="_blank" rel="noopener">Apri articolo</a></div>';
  }
  if (job.job_kind === "research_batch" && job.request_ids && job.request_ids.length) {
    html +=
      '<div class="active-job-batch-children hint">' +
      escapeHtml(job.request_ids.length + " sotto-job research tracciati") +
      "</div>";
  }
  html += "</div>";
  return html;
}

function countActiveJobs() {
  let n = 0;
  jobsById.forEach((job) => {
    if (job.is_active) n += 1;
  });
  return n;
}

function renderJobs() {
  const root = getJobsRoot();
  if (!root) return;
  const jobs = Array.from(jobsById.values()).sort(function (a, b) {
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
  heading.textContent = "Job" + suffix;
}

async function refreshJobsList() {
  const root = getJobsRoot();
  if (!root) return;
  const watched = loadWatched();
  const watchedIds = new Set(watched.map((item) => item.job_id));
  try {
    const data = await apiJson("/api/system/jobs?limit=30");
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

export function initActiveJobs() {
  if (!getJobsRoot()) return;
  window.addEventListener("message", (event) => {
    if (event.data && event.data.type === "librarain-jobs-refresh") refreshJobsList();
  });
  const watched = loadWatched();
  watched.forEach((item) => ensureJobWatched(item.job_id));
  refreshJobsList();
  window.setInterval(refreshJobsList, 8000);
}
