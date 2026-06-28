import { apiJson, preflightOrBlock, articleUrl } from "./api.js";
import { trackJob } from "./jobs.js";

export function renderPohCard(poh, options = {}) {
  const div = document.createElement("div");
  div.className = "poh-card";
  const hasArticle = poh.has_article || poh.url;
  const label = poh.label || poh.title || poh.poh_id;
  div.innerHTML =
    `<div><strong>${escapeHtml(label)}</strong> <code>${escapeHtml(poh.poh_id)}</code></div>` +
  (poh.snippet ? `<div class="hint">${escapeHtml(poh.snippet)}</div>` : "");
  const actions = document.createElement("div");
  actions.style.marginTop = "0.5rem";
  if (hasArticle && poh.url) {
    const link = document.createElement("a");
    link.href = articleUrl(poh.poh_id, poh.url);
    link.textContent = "Apri articolo";
    link.target = "_blank";
    actions.appendChild(link);
  } else {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = "Articolo mancante";
    actions.appendChild(badge);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Genera articolo";
    btn.addEventListener("click", () => generateArticle(poh.poh_id, label));
    actions.appendChild(document.createTextNode(" "));
    actions.appendChild(btn);
  }
  div.appendChild(actions);
  if (options.onMerge && poh.similar_to) {
    const mergeBtn = document.createElement("button");
    mergeBtn.type = "button";
    mergeBtn.className = "secondary";
    mergeBtn.textContent = `Unisci a ${poh.similar_to.label}`;
    mergeBtn.addEventListener("click", () => options.onMerge(poh, poh.similar_to));
    actions.appendChild(document.createTextNode(" "));
    actions.appendChild(mergeBtn);
    const newBtn = document.createElement("button");
    newBtn.type = "button";
    newBtn.className = "secondary";
    newBtn.textContent = "Nuovo articolo";
    newBtn.addEventListener("click", () => options.onNew(poh));
    actions.appendChild(document.createTextNode(" "));
    actions.appendChild(newBtn);
  }
  return div;
}

export async function generateArticle(pohId, label) {
  if (!(await preflightOrBlock("research"))) return;
  const query = label || pohId;
  const data = await apiJson("/api/research/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      query,
      poh: { id: pohId, label: query },
      options: { dedup: true },
    }),
  });
  const jobId = data.request_id || data.job_id;
  if (jobId) trackJob(jobId, { job_kind: "research", poh_id: pohId, poh_label: query });
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
