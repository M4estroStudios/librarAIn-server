const mock = window.__librarainMock;
const EVENT_MS = 70;
const LAB_WAIT_MS = 8000;

let labApi = null;
let running = false;

function sleep(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

function labLog(message) {
  if (labApi && labApi.labLog) {
    labApi.labLog(message);
    return;
  }
  if (window.LibrarAInLog) window.LibrarAInLog.info(message, { sink: "lab" });
}

function waitForLab() {
  if (labApi) return Promise.resolve(labApi);
  if (window.__librarainIngestLab) {
    labApi = window.__librarainIngestLab;
    return Promise.resolve(labApi);
  }
  return new Promise(function (resolve, reject) {
    const timer = setTimeout(function () {
      window.removeEventListener("librarain-ingest-ready", onReady);
      reject(new Error("UI ingest non pronta (ricarica la pagina)"));
    }, LAB_WAIT_MS);
    function onReady() {
      clearTimeout(timer);
      labApi = window.__librarainIngestLab || null;
      if (!labApi) {
        reject(new Error("API Lab mancante"));
        return;
      }
      resolve(labApi);
    }
    window.addEventListener("librarain-ingest-ready", onReady, { once: true });
  });
}

function syncMockCheckbox() {
  const mockInput = document.getElementById("ingest-debug-mock");
  if (mockInput) mockInput.checked = mock.isEnabled();
}

function syncSubmitButton() {
  const submit = document.getElementById("submit-btn");
  const hint = document.getElementById("ingest-debug-mock-hint");
  if (!submit) return;
  if (mock.isEnabled()) {
    submit.disabled = true;
    submit.title = "Disabilitato in modalità mock — usa i comandi Lab";
    if (hint) hint.textContent = "Mock attivo: API finte e ingest reale disabilitato.";
  } else {
    submit.disabled = false;
    submit.title = "";
    if (hint) hint.textContent = "I comandi Lab attivano mock automaticamente. Spunta mock solo per review/repair manuali.";
  }
}

function ensureMockEnabled() {
  if (!mock.isEnabled()) {
    mock.setEnabled(true);
    syncMockCheckbox();
    syncSubmitButton();
    labLog("Mock attivato automaticamente.");
  }
}

function setLabRunning(active) {
  running = active;
  const actions = document.getElementById("ingest-debug-lab-actions");
  if (!actions) return;
  actions.querySelectorAll("button").forEach(function (btn) {
    btn.disabled = active;
  });
}

async function playEvents(lab, fixtureName, label, finishUi) {
  labLog("Avvio scenario: " + label + "…");
  const events = await mock.loadScenarioFixture(fixtureName);
  lab.startProgress();
  if (lab.clearProgressAlert) lab.clearProgressAlert();
  for (let i = 0; i < events.length; i++) {
    lab.handleEvent(events[i]);
    await sleep(EVENT_MS);
  }
  if (finishUi) {
    await lab.refreshAudit();
    if (lab.applyMockAuditPagesState) lab.applyMockAuditPagesState();
    if (lab.finishIngestUi) lab.finishIngestUi();
  }
  labLog("Scenario completato: " + label + ".");
}

async function runAuditOnly(lab) {
  labLog("Avvio scenario: solo lacune audit…");
  await playEvents(lab, "sse-ingest-done.json", "solo lacune audit", false);
  await lab.refreshAudit();
  if (lab.applyMockAuditPagesState) lab.applyMockAuditPagesState();
  if (lab.finishIngestUi) lab.finishIngestUi();
  labLog("Scenario completato: lacune audit caricate.");
}

async function runScenario(scenario) {
  ensureMockEnabled();
  setLabRunning(true);
  try {
    const lab = await waitForLab();
    if (scenario === "partial") {
      await playEvents(lab, "sse-ingest-partial.json", "ingest parziale", false);
      return;
    }
    if (scenario === "done") {
      if (mock.resetAuditState) mock.resetAuditState();
      await playEvents(lab, "sse-ingest-done.json", "ingest completo", true);
      return;
    }
    if (scenario === "audit") {
      if (mock.resetAuditState) mock.resetAuditState();
      await runAuditOnly(lab);
      return;
    }
    if (scenario === "reset") {
      if (mock.resetAuditState) mock.resetAuditState();
      lab.resetProgress();
      if (lab.clearProgressAlert) lab.clearProgressAlert();
      labLog("Progresso azzerato.");
    }
  } finally {
    setLabRunning(false);
  }
}

function initPanel() {
  const panel = document.getElementById("ingest-debug-panel");
  const toggle = document.getElementById("ingest-debug-panel-toggle");
  const body = document.getElementById("ingest-debug-panel-body");
  const mockInput = document.getElementById("ingest-debug-mock");
  const actions = document.getElementById("ingest-debug-lab-actions");
  if (!panel || !toggle || !body || !mockInput || !actions) return;

  mock.loadSavedEnabled();
  syncMockCheckbox();
  syncSubmitButton();
  labApi = window.__librarainIngestLab || null;

  if (new URLSearchParams(location.search).get("mock") === "1") {
    if (labApi && labApi.openLabPanel) labApi.openLabPanel();
    mock.setEnabled(true);
    syncMockCheckbox();
    syncSubmitButton();
    labLog("Pannello Lab aperto (mock=1).");
  }

  mockInput.addEventListener("change", function () {
    mock.setEnabled(mockInput.checked);
    syncSubmitButton();
    labLog(mockInput.checked ? "Mock attivato manualmente." : "Mock disattivato.");
  });

  window.addEventListener("librarain-mock-mode", function () {
    syncMockCheckbox();
    syncSubmitButton();
  });

  actions.addEventListener("click", function (event) {
    const btn = event.target.closest("button[data-scenario]");
    if (!btn || running) return;
    const scenario = btn.getAttribute("data-scenario");
    labLog("Comando: " + btn.textContent.trim() + "…");
    runScenario(scenario).catch(function (err) {
      if (labApi && labApi.labLogError) labApi.labLogError(String(err && err.message ? err.message : err));
      else labLog("Errore: " + String(err && err.message ? err.message : err));
      if (window.LibrarAInLog) window.LibrarAInLog.reportError("lab scenario failed", err);
    });
  });

  labApi = window.__librarainIngestLab || null;
  if (labApi) labLog("Lab pronto (inline).");
}

window.addEventListener("librarain-ingest-ready", function () {
  labApi = window.__librarainIngestLab || null;
  labLog("UI ingest collegata al Lab (inline).");
}, { once: true });

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initPanel);
} else {
  initPanel();
}
