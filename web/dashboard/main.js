import { initGate } from "./gate.js";
import { initGoogleSearch } from "./search-google.js";
import { initPerplexitySearch } from "./search-perplexity.js";
import { initIngest, setContextNote } from "./ingest.js";
import { initAdmin } from "./admin-embed.js";
import { initMock } from "./mock.js";

function initSections() {
  document.querySelectorAll(".section").forEach((section) => {
    const btn = section.querySelector(".section-toggle");
    if (!btn) return;
    btn.addEventListener("click", () => {
      section.classList.toggle("open");
    });
  });
  const searchSection = document.getElementById("section-search");
  if (searchSection) searchSection.classList.add("open");
}

function initSearchModeToggle() {
  const googleBtn = document.getElementById("mode-google");
  const perplexityBtn = document.getElementById("mode-perplexity");
  const googlePanel = document.getElementById("panel-google");
  const perplexityPanel = document.getElementById("panel-perplexity");
  function setMode(mode) {
    const isGoogle = mode === "google";
    googleBtn?.classList.toggle("active", isGoogle);
    perplexityBtn?.classList.toggle("active", !isGoogle);
    googlePanel?.classList.toggle("hidden", !isGoogle);
    perplexityPanel?.classList.toggle("hidden", isGoogle);
  }
  googleBtn?.addEventListener("click", () => setMode("google"));
  perplexityBtn?.addEventListener("click", () => setMode("perplexity"));
  setMode("google");
}

export function openIngestWithContext(text) {
  const ingestSection = document.getElementById("section-ingest");
  const searchSection = document.getElementById("section-search");
  if (searchSection) searchSection.classList.remove("open");
  if (ingestSection) {
    ingestSection.classList.add("open");
    ingestSection.scrollIntoView({ behavior: "smooth" });
  }
  setContextNote(text);
  const note = document.getElementById("context-note");
  if (note && String(text || "").trim()) note.focus();
}

window.__librarainDashboard = { openIngestWithContext };

const PENDING_CONTEXT_KEY = "librarainPendingContextNote";

function consumePendingContextNote() {
  try {
    const pending = sessionStorage.getItem(PENDING_CONTEXT_KEY);
    if (!pending) return;
    sessionStorage.removeItem(PENDING_CONTEXT_KEY);
    openIngestWithContext(pending);
  } catch {}
}

initGate();
initSections();
initSearchModeToggle();
initGoogleSearch();
initPerplexitySearch();
initIngest();
initAdmin();
initMock();
consumePendingContextNote();
