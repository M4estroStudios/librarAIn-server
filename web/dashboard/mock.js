import {
  restoreMockState,
  setMockEnabled,
  setMockScenario,
  isMockEnabled,
  getMockScenario,
} from "./api.js";

const SCENARIOS = [
  "ricerca-google-hit",
  "ricerca-google-empty",
  "ricerca-perplexity-stream",
  "preflight-blocked",
  "preflight-ok",
  "ingest-sse-progress",
  "ingest-done-poh-list",
  "genera-articoli-skip-simile",
  "admin-poh-duplicati",
  "admin-repair-sse",
];

const FIXTURE_BASE = "/mockup/fixtures/";

export function initMock() {
  restoreMockState();
  const toggle = document.getElementById("mock-enabled");
  const select = document.getElementById("mock-scenario");
  if (select) {
    SCENARIOS.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = s;
      select.appendChild(opt);
    });
    if (getMockScenario()) select.value = getMockScenario();
  }
  if (toggle) {
    toggle.checked = isMockEnabled();
    toggle.addEventListener("change", () => {
      setMockEnabled(toggle.checked);
      installFetchHook();
    });
  }
  if (select) {
    select.addEventListener("change", () => {
      setMockScenario(select.value);
      installFetchHook();
    });
  }
  installFetchHook();
}

function installFetchHook() {
  if (!isMockEnabled() || !getMockScenario()) {
    if (window.__librarainDashboardFetchRestore) {
      window.fetch = window.__librarainDashboardFetchRestore;
      window.__librarainDashboardFetchRestore = null;
    }
    return;
  }
  if (!window.__librarainDashboardFetchRestore) {
    window.__librarainDashboardFetchRestore = window.fetch;
  }
  const native = window.__librarainDashboardFetchRestore;
  window.fetch = async function hookedFetch(url, options) {
    const scenario = getMockScenario();
    if (!scenario) return native(url, options);
    const path = String(url).split("?")[0];
    if (path.includes("/api/system/preflight")) {
      return mockJsonFixture(mapFixtureName(scenario, "json"), scenario);
    }
    if (path.includes("/api/research/search")) {
      return mockJsonFixture(mapFixtureName(scenario, "json"), scenario);
    }
    if (path.includes("/api/chat/completions")) {
      return mockStreamFixture(`dashboard-${scenario}-chat.json`, scenario);
    }
    if (path.includes("/api/ingest/") && path.endsWith("/events")) {
      return mockSseFixture(`dashboard-${scenario}-sse.json`, scenario);
    }
    if (path.includes("/api/research/missing")) {
      return mockJsonFixture(mapFixtureName(scenario, "missing"), scenario);
    }
    if (path.includes("/api/research/poh-overlaps")) {
      return mockJsonFixture(mapFixtureName(scenario, "overlaps"), scenario);
    }
    if (path.includes("/api/admin/subjects")) {
      return mockJsonFixture("dashboard-admin-poh-duplicati.json", "admin-poh-duplicati");
    }
    return native(url, options);
  };
}

async function mockJsonFixture(name, scenario) {
  const file = mapFixtureName(scenario, "json");
  try {
    const res = await window.__librarainDashboardFetchRestore(FIXTURE_BASE + file);
    if (res.ok) return res;
  } catch {}
  return new Response(JSON.stringify({ ok: true, mock: scenario }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

async function mockStreamFixture(name, scenario) {
  const file = mapFixtureName(scenario, "chat");
  try {
    const res = await window.__librarainDashboardFetchRestore(FIXTURE_BASE + file);
    if (res.ok) return res;
  } catch {}
  const body =
    "data: {\"choices\":[{\"delta\":{\"content\":\"Risposta mock Perplexity con citazione \"}}]}\n\n" +
    "data: {\"choices\":[{\"delta\":{\"content\":\"[Alpha](poh:subj_alpha)\"}}]}\n\n" +
    "data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n" +
    "data: [DONE]\n\n";
  return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

async function mockSseFixture(name, scenario) {
  const file = mapFixtureName(scenario, "sse");
  try {
    return await window.__librarainDashboardFetchRestore(FIXTURE_BASE + file);
  } catch {
    const body = "event: progress\ndata: {\"status\":\"progress\",\"message\":\"mock\"}\n\n";
    return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  }
}

function mapFixtureName(scenario, kind) {
  if (kind === "json" && scenario.startsWith("preflight")) return `dashboard-${scenario}.json`;
  if (kind === "chat" && scenario === "ricerca-perplexity-stream") return "dashboard-ricerca-perplexity-stream.json";
  if (kind === "sse" && scenario === "admin-repair-sse") return "dashboard-admin-repair-sse.json";
  if (kind === "sse" && scenario === "ingest-sse-progress") return "dashboard-ingest-sse-progress.json";
  if (kind === "json" && scenario === "ricerca-google-hit") return "dashboard-ricerca-google-hit.json";
  if (kind === "json" && scenario === "ricerca-google-empty") return "dashboard-ricerca-google-empty.json";
  if (kind === "json" && scenario === "ingest-done-poh-list") return "dashboard-ingest-done-poh-list.json";
  if (kind === "missing" && scenario === "ingest-done-poh-list") return "dashboard-ingest-done-poh-list.json";
  if (kind === "overlaps" && scenario === "genera-articoli-skip-simile") return "dashboard-genera-articoli-skip-simile.json";
  if (kind === "json" && scenario === "genera-articoli-skip-simile") return "dashboard-genera-articoli-skip-simile.json";
  return `dashboard-${scenario}.json`;
}
