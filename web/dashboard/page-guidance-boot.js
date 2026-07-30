import { createPageGuidanceController } from "/dashboard/page-guidance.js";

function wireCounsel(bridge, getAnnotations) {
  const guidanceField = document.querySelector('[name="ai_page_guidance"]');
  const counselBtn = document.getElementById("ai-counsel-btn");
  const counselStatus = document.getElementById("ai-counsel-status");
  if (!counselBtn) return;
  counselBtn.addEventListener("click", async function () {
    if (window.__librarainMock && window.__librarainMock.isEnabled()) {
      if (counselStatus) counselStatus.textContent = "Disabilitato in modalità mock.";
      return;
    }
    const file = bridge.getPdfFile();
    if (!file || !file.size) {
      if (counselStatus) counselStatus.textContent = "Carica prima un PDF.";
      return;
    }
    counselBtn.disabled = true;
    if (counselStatus) counselStatus.textContent = "Generazione consiglio AI…";
    try {
      const fd = new FormData();
      fd.append("pdf_file", file, file.name || "upload.pdf");
      fd.append("compute_mode", (document.getElementById("compute-mode") || {}).value || "local");
      fd.append("notes", (document.querySelector('[name="notes"]') || {}).value || "");
      fd.append("index_notes", (document.querySelector('[name="index_notes"]') || {}).value || "");
      fd.append("page_notes", (document.querySelector('[name="page_notes"]') || {}).value || "");
      fd.append("annotations_json", JSON.stringify(getAnnotations()));
      const res = await fetch("/api/ingest/page-guidance-suggest", { method: "POST", body: fd });
      const data = await res.json().catch(function () {
        return {};
      });
      if (!res.ok || !data.ok) throw new Error(data.error || data.message || ("HTTP " + res.status));
      if (guidanceField) guidanceField.value = data.guidance || "";
      if (counselStatus) {
        const samples = (data.sample_pages || []).join(", ") || "—";
        counselStatus.textContent = "Consiglio generato (sample: " + samples + ").";
      }
    } catch (err) {
      if (counselStatus) counselStatus.textContent = String(err && err.message ? err.message : err);
    } finally {
      counselBtn.disabled = false;
    }
  });
}

export async function bootPageGuidance(bridge) {
  const controller = createPageGuidanceController(bridge);
  if (!controller) return null;
  window.__librarainPageGuidance = controller;
  wireCounsel(bridge, controller.getAnnotations);
  try {
    const mentions = await import("/dashboard/page-guidance-mentions.js");
    if (mentions && typeof mentions.bootPageGuidanceMentions === "function") {
      const ui = mentions.bootPageGuidanceMentions(bridge, controller.getAnnotations);
      controller.onAnnotationsChange(function () {
        if (ui && typeof ui.refresh === "function") ui.refresh();
      });
      if (ui && typeof ui.refresh === "function") ui.refresh();
    }
  } catch (_) {}
  return controller;
}
