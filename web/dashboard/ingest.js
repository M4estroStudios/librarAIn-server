import { apiJson, preflightOrBlock, articleUrl } from "./api.js";
import { renderPohCard } from "./poh-card.js";
import { initEmbedFrameListener } from "./admin-embed.js";
import { trackJob } from "./jobs.js";

let contextNoteEl;
let pohListEl;
let overlapNoticeEl;
let generateCheckbox;

export function initIngest() {
  contextNoteEl = document.getElementById("context-note");
  pohListEl = document.getElementById("ingest-poh-list");
  overlapNoticeEl = document.getElementById("ingest-poh-overlap-notice");
  generateCheckbox = document.getElementById("generate-articles-checkbox");
  const copyBtn = document.getElementById("context-note-copy");
  if (copyBtn && contextNoteEl) {
    copyBtn.addEventListener("click", () => {
      navigator.clipboard.writeText(contextNoteEl.value || "");
    });
  }
  window.addEventListener("message", (event) => {
    const data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.type === "librarain-ingest-started" && data.job_id) {
      trackJob(data.job_id, { job_kind: "ingest" });
      return;
    }
    if (data.type !== "librarain-ingest-done") return;
    if (generateCheckbox && generateCheckbox.checked && data.source_sha256) {
      postIngestPohFlow(data.source_sha256);
    }
  });
  syncApiTokenToIframe();
  initEmbedFrameListener("ingest-frame", "ingest");
  const frame = document.getElementById("ingest-frame");
  if (frame) {
    frame.addEventListener("load", syncApiTokenToIframe);
  }
}

function syncApiTokenToIframe() {
  try {
    const token = localStorage.getItem("librarainApiToken") || sessionStorage.getItem("librarainApiToken") || "";
    if (token) localStorage.setItem("librarainApiToken", token);
  } catch {}
}

export function setContextNote(text) {
  const value = String(text || "");
  if (contextNoteEl) contextNoteEl.value = value;
  const block = document.getElementById("context-note-block");
  if (block) block.classList.toggle("hidden", !value.trim());
}

function contextNote() {
  return (contextNoteEl && contextNoteEl.value || "").trim();
}

async function postIngestPohFlow(bookSha) {
  if (!pohListEl) return;
  pohListEl.innerHTML = "<p class='hint'>Analisi POH del libro…</p>";
  if (overlapNoticeEl) overlapNoticeEl.classList.add("hidden");
  try {
    const overlaps = await apiJson(
      `/api/research/poh-overlaps?book_sha=${encodeURIComponent(bookSha)}`
    );
    const missing = await apiJson(
      `/api/research/missing?book_sha=${encodeURIComponent(bookSha)}`
    );
    const overlapItems = overlaps.overlaps || [];
    const missingItems = missing.missing || [];
    if (overlapItems.length && overlapNoticeEl) {
      overlapNoticeEl.textContent =
        `Trovati ${overlapItems.length} POH del nuovo libro simili ad articoli esistenti. Scegli per ogni voce se creare un articolo nuovo o unire al POH esistente.`;
      overlapNoticeEl.classList.remove("hidden");
    }
    pohListEl.innerHTML = "";
    overlapItems.forEach((item) => {
      const block = document.createElement("div");
      block.className = "poh-card";
      block.innerHTML = `<strong>${escape(item.label)}</strong> <code>${escape(item.poh_id)}</code>`;
      const best = item.similar_to && item.similar_to[0];
      if (best) {
        const p = document.createElement("p");
        p.className = "hint";
        p.textContent = `Simile a «${best.label}» (${Math.round(best.similarity * 100)}%)`;
        block.appendChild(p);
        const actions = document.createElement("div");
        actions.style.marginTop = "0.5rem";
        const mergeBtn = document.createElement("button");
        mergeBtn.type = "button";
        mergeBtn.textContent = `Unisci a ${best.label}`;
        mergeBtn.addEventListener("click", () => mergePohIntoExisting(item, best, bookSha));
        const newBtn = document.createElement("button");
        newBtn.type = "button";
        newBtn.className = "secondary";
        newBtn.textContent = "Nuovo articolo";
        newBtn.addEventListener("click", () => generateNewArticle(item));
        actions.appendChild(mergeBtn);
        actions.appendChild(document.createTextNode(" "));
        actions.appendChild(newBtn);
        block.appendChild(actions);
      }
      pohListEl.appendChild(block);
    });
    missingItems.forEach((poh) => {
      if (overlapItems.some((o) => o.poh_id === poh.poh_id)) return;
      pohListEl.appendChild(
        renderPohCard({
          poh_id: poh.poh_id,
          label: poh.label,
          has_article: false,
        })
      );
    });
    if (!overlapItems.length && !missingItems.length) {
      pohListEl.innerHTML = "<p class='hint'>Nessun POH da elaborare per questo libro.</p>";
    }
  } catch (err) {
    pohListEl.innerHTML = `<p class='hint'>${escape(String(err.message || err))}</p>`;
  }
}

async function mergePohIntoExisting(source, target, bookSha) {
  if (!(await preflightOrBlock("research-merge"))) return;
  if (!confirm(`Unire ${source.label} → ${target.label} e aggiornare l'articolo?`)) return;
  await apiJson("/api/admin/subjects/merge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_id: target.poh_id, source_ids: [source.poh_id] }),
  });
  await apiJson("/api/research/merge-article", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      target_poh_id: target.poh_id,
      operator_notes: contextNote(),
      context_note: contextNote(),
      book_sha: bookSha,
    }),
  });
  alert(`Articolo aggiornato: ${articleUrl(target.poh_id)}`);
}

async function generateNewArticle(poh) {
  if (!(await preflightOrBlock("research"))) return;
  const data = await apiJson("/api/research/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query: poh.label,
      poh: { id: poh.poh_id, label: poh.label },
      options: { dedup: true },
    }),
  });
  const jobId = data.request_id || data.job_id;
  if (jobId) {
    trackJob(jobId, {
      job_kind: "research",
      poh_id: poh.poh_id,
      poh_label: poh.label,
    });
  }
}

function escape(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}
