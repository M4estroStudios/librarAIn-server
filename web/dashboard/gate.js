import { apiJson } from "./api.js";

let overlay;
let messageEl;
let pendingOperation = null;

export function initGate() {
  overlay = document.getElementById("gate-overlay");
  messageEl = document.getElementById("gate-message");
  const retryBtn = document.getElementById("gate-retry");
  if (retryBtn) {
    retryBtn.addEventListener("click", async () => {
      if (!pendingOperation) return;
      try {
        const data = await apiJson(
          `/api/system/preflight?operation=${encodeURIComponent(pendingOperation)}`
        );
        if (data.ok) hide();
      } catch (err) {
        if (messageEl) messageEl.textContent = String(err.message || err);
      }
    });
  }
  window.__librarainGate = { show, hide };
}

export function show(operation, message) {
  pendingOperation = operation;
  if (messageEl) messageEl.textContent = message || "Risorse GPU/modello non sufficienti.";
  if (overlay) overlay.classList.add("show");
}

export function hide() {
  pendingOperation = null;
  if (overlay) overlay.classList.remove("show");
}
