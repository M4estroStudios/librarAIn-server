import { apiJson, articleUrl } from "./api.js";
import { renderPohCard } from "./poh-card.js";

let resultsEl;
let ctaEl;
let searchInput;
let layoutOpts = {};

function pickEl(opt, fallbackId) {
  if (opt instanceof Element) return opt;
  if (typeof opt === "string") return document.getElementById(opt);
  if (fallbackId) return document.getElementById(fallbackId);
  return null;
}

export function initGoogleSearch(options = {}) {
  layoutOpts = options;
  resultsEl = pickEl(options.resultsEl, "google-results");
  ctaEl = pickEl(options.ctaEl, "google-cta");
  searchInput = pickEl(options.searchInput, "google-q");
  const form = pickEl(options.form, "google-form");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = (searchInput && searchInput.value || "").trim();
      if (layoutOpts.getSearchMode?.() === "perplexity") {
        layoutOpts.onPerplexitySubmit?.(q);
        return;
      }
      const url = new URL(location.href);
      if (q) url.searchParams.set("q", q);
      else url.searchParams.delete("q");
      url.searchParams.delete("ai");
      history.pushState({}, "", url);
      runSearch(q);
    });
  }
  window.addEventListener("popstate", () => syncFromUrl());
  syncFromUrl();
}

function syncFromUrl() {
  const params = new URLSearchParams(location.search);
  const q = (params.get("q") || "").trim();
  if (searchInput) searchInput.value = q;
  if (layoutOpts.getSearchMode?.() === "perplexity") return;
  if (q.length >= 2) runSearch(q);
  else if (resultsEl) resultsEl.innerHTML = "";
}

function originUrl(path) {
  return window.location.origin + path;
}

export async function runSearch(q) {
  if (!resultsEl) return;
  if (q.length < 2) {
    resultsEl.innerHTML = "<p class='hint'>Digita almeno 2 caratteri.</p>";
    return;
  }
  if (layoutOpts.onSearchStart) layoutOpts.onSearchStart(q);
  const metaEl = pickEl(layoutOpts.metaEl, null);
  const emptyEl = pickEl(layoutOpts.emptyEl, null);
  if (metaEl) metaEl.textContent = "";
  if (emptyEl) emptyEl.classList.remove("show");
  resultsEl.innerHTML = "<p class='hint'>Ricerca…</p>";
  try {
    const data = await apiJson(
      `/api/research/search?q=${encodeURIComponent(q)}`
    );
    const results = data.results || [];
    resultsEl.innerHTML = "";
    if (!results.length) {
      if (emptyEl) {
        emptyEl.classList.add("show");
      } else {
        resultsEl.innerHTML =
          "<p class='hint'>Nessun risultato. Prova altri termini o fornisci una fonte via ingest.</p>";
      }
    } else {
      if (emptyEl) emptyEl.classList.remove("show");
      if (metaEl) {
        metaEl.textContent = `Circa ${results.length} risultati per "${q}"`;
      } else {
        const meta = document.createElement("p");
        meta.className = "hint";
        meta.textContent = `Circa ${results.length} risultati per "${q}"`;
        resultsEl.appendChild(meta);
      }
      results.forEach((r) => {
        const item = document.createElement("div");
        item.className = "result-item";
        if (r.has_article && r.url) {
          const href = articleUrl(r.poh_id, r.url);
          item.innerHTML =
            `<h3><a href="${href}">${escape(r.label || r.title)}</a></h3>` +
            `<div class="hint" style="color:#6a9955;">${escape(originUrl(href))}</div>` +
            (r.snippet ? `<p class="hint">${escape(r.snippet)}</p>` : "");
        } else {
          item.appendChild(renderPohCard(r));
        }
        resultsEl.appendChild(item);
      });
    }
    if (ctaEl) {
      ctaEl.classList.remove("hidden");
      ctaEl.onclick = () => {
        window.__librarainDashboard.openIngestWithContext(q);
      };
    }
  } catch (err) {
    resultsEl.innerHTML = `<p class='hint'>Errore: ${escape(String(err.message || err))}</p>`;
  }
}

function escape(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}
