import { apiJson, articleUrl } from "./api.js";
import { trackJob } from "./jobs.js";

const ISSUE_LABELS = {
  missing: "Mancante",
  no_material: "Materiale insufficiente",
  damaged: "Danneggiato",
  empty_file: "File vuoto",
  markdown_missing: "Markdown assente",
  content_mismatch: "Contenuto incoerente",
  incomplete: "Contenuto incompleto",
  orphan_catalog: "Catalogo incompleto",
  orphan_file: "File orfano",
  unknown_subject: "POH sconosciuto",
};

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderSummary(audit) {
  const parts = [
    (audit.generated_count != null ? audit.generated_count : (audit.generated || []).length) + " generati",
    audit.complete_count + "/" + audit.total_subjects + " completi",
  ];
  if (audit.issues_count) {
    parts.push(audit.issues_count + " problemi");
    parts.push(audit.affected_poh_count + " POH coinvolti");
  }
  if (audit.missing_count) parts.push(audit.missing_count + " da generare");
  return parts.join(" · ");
}

function renderGeneratedRow(item) {
  const href = articleUrl(item.poh_id, item.url);
  let status = item.ok ? "Completo" : item.no_material ? "Materiale insufficiente" : "Con problemi";
  let html =
    '<div class="article-audit-row">' +
    '<div class="article-audit-row-title">' +
    escapeHtml(item.label || item.poh_id) +
    " <code>" +
    escapeHtml(item.poh_id) +
    "</code></div>" +
    '<div class="article-audit-row-detail">' +
    escapeHtml(status) +
    "</div>" +
    '<div class="article-audit-row-actions">' +
    '<a href="' +
    escapeHtml(href) +
    '" target="_blank" rel="noopener">Apri</a>' +
    "</div></div>";
  return html;
}

function renderGeneratedList(generated) {
  if (!generated || !generated.length) {
    return '<div class="active-jobs-empty">Nessun articolo generato con HTML e markdown.</div>';
  }
  let html =
    '<div class="article-audit-group">' +
    '<div class="article-audit-group-title">Generati (' +
    generated.length +
    ")</div>";
  generated.forEach((item) => {
    html += renderGeneratedRow(item);
  });
  html += "</div>";
  return html;
}

function groupIssues(issues) {
  const groups = new Map();
  issues.forEach((item) => {
    const key = item.issue || "other";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(item);
  });
  return groups;
}

function renderIssueRow(item) {
  const href = item.url ? articleUrl(item.poh_id, item.url) : articleUrl(item.poh_id);
  let html =
    '<div class="article-audit-row">' +
    '<div class="article-audit-row-title">' +
    escapeHtml(item.label || item.poh_id) +
    " <code>" +
    escapeHtml(item.poh_id) +
    "</code></div>" +
    '<div class="article-audit-row-detail">' +
    escapeHtml(item.detail || "") +
    "</div>" +
    '<div class="article-audit-row-actions">';
  if (item.url || item.issue !== "orphan_file") {
    html +=
      '<a href="' +
      escapeHtml(href) +
      '" target="_blank" rel="noopener">Apri</a> ';
  }
  if (item.issue === "missing" || item.issue === "no_material" || item.issue === "damaged" || item.issue === "incomplete" || item.issue === "content_mismatch" || item.issue === "markdown_missing" || item.issue === "empty_file") {
    html +=
      '<button type="button" class="secondary article-audit-regen" data-poh-id="' +
      escapeHtml(item.poh_id) +
      '" data-label="' +
      escapeHtml(item.label || item.poh_id) +
      '">Rigenera</button>';
  }
  html += "</div></div>";
  return html;
}

function renderAudit(audit) {
  const root = document.getElementById("article-audit-root");
  if (!root) return;
  const generated = audit.generated || [];
  let html =
    '<div class="article-audit-summary">' + escapeHtml(renderSummary(audit)) + "</div>" +
    renderGeneratedList(generated);
  if (audit.issues_count) {
    const groups = groupIssues(audit.issues || []);
    groups.forEach((items, issue) => {
      const label = ISSUE_LABELS[issue] || issue;
      html +=
        '<div class="article-audit-group">' +
        '<div class="article-audit-group-title">' +
        escapeHtml(label) +
        " (" +
        items.length +
        ")</div>";
      items.forEach((item) => {
        html += renderIssueRow(item);
      });
      html += "</div>";
    });
  } else if (generated.length) {
    html += '<div class="article-audit-ok">Nessun problema tra gli articoli generati.</div>';
  }
  root.innerHTML = html;
  root.querySelectorAll(".article-audit-regen").forEach((btn) => {
    btn.addEventListener("click", () => regenerateArticle(btn));
  });
  if (typeof window.reportEmbedHeight === "function") window.reportEmbedHeight();
}

async function regenerateArticle(btn) {
  const pohId = btn.getAttribute("data-poh-id");
  const label = btn.getAttribute("data-label") || pohId;
  if (!pohId) return;
  btn.disabled = true;
  try {
    const data = await apiJson("/api/research/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query: label,
        poh: { id: pohId, label },
        options: { dedup: false },
      }),
    });
    const jobId = data.request_id || data.job_id;
    if (jobId) {
      trackJob(jobId, { job_kind: "research", poh_id: pohId, poh_label: label });
    }
    btn.textContent = "Avviato";
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "Errore";
    if (window.LibrarAInLog) window.LibrarAInLog.reportError("article regen failed", err);
  }
}

async function runArticleAudit() {
  const root = document.getElementById("article-audit-root");
  const btn = document.getElementById("article-audit-run");
  if (root) root.innerHTML = '<div class="active-jobs-empty">Verifica in corso…</div>';
  if (btn) btn.disabled = true;
  try {
    const audit = await apiJson("/api/research/articles/audit");
    renderAudit(audit);
  } catch (err) {
    if (root) {
      root.innerHTML =
        '<div class="active-jobs-empty">Verifica non disponibile: ' +
        escapeHtml(err.message) +
        "</div>";
    }
  } finally {
    if (btn) btn.disabled = false;
    if (typeof window.reportEmbedHeight === "function") window.reportEmbedHeight();
  }
}

export function initArticleAudit() {
  const btn = document.getElementById("article-audit-run");
  btn?.addEventListener("click", () => runArticleAudit());
  runArticleAudit();
}
