const TOKEN_KEY = "librarainApiToken";
let mockScenario = null;
let mockEnabled = false;

export function apiToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setApiToken(token) {
  try {
    localStorage.setItem(TOKEN_KEY, token);
    sessionStorage.setItem(TOKEN_KEY, token);
  } catch {}
}

export function articleUrl(pohId, basePath) {
  const path = basePath || `/articolo/${encodeURIComponent(pohId)}.html`;
  if (!isMockEnabled() || !mockScenario) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}mock=${encodeURIComponent(mockScenario)}`;
}

export function promptApiToken() {
  const t = prompt("Token API (INGEST_API_TOKEN):", apiToken());
  if (t) setApiToken(t.trim());
  return apiToken();
}

export function getMockScenario() {
  return mockScenario;
}

export function setMockEnabled(on) {
  mockEnabled = on;
  try {
    sessionStorage.setItem("librarainDashboardMock", on ? "1" : "0");
    if (mockScenario) sessionStorage.setItem("librarainDashboardMockScenario", mockScenario);
  } catch {}
}

export function setMockScenario(scenario) {
  mockScenario = scenario || null;
  try {
    if (scenario) sessionStorage.setItem("librarainDashboardMockScenario", scenario);
  } catch {}
}

export function isMockEnabled() {
  return mockEnabled;
}

export function restoreMockState() {
  try {
    mockEnabled = sessionStorage.getItem("librarainDashboardMock") === "1";
    mockScenario = sessionStorage.getItem("librarainDashboardMockScenario") || null;
  } catch {}
}

export async function apiFetch(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  const token = apiToken();
  if (token) headers.set("X-API-Token", token);
  if (mockEnabled && mockScenario) {
    headers.set("X-Mock-Scenario", mockScenario);
  }
  let res;
  try {
    res = await fetch(url, { ...options, headers });
  } catch (err) {
    if (window.LibrarAInLog) {
      window.LibrarAInLog.reportError("http request network error", err);
    }
    throw err;
  }
  if (window.LibrarAInLog && (method !== "GET" || !res.ok)) {
    window.LibrarAInLog.logHttpDone(method, url, res.status);
  }
  if (res.status === 401 && !options._retried && !options.noAuthPrompt) {
    const newToken = promptApiToken();
    if (newToken) {
      return apiFetch(url, { ...options, _retried: true });
    }
  }
  return res;
}

export async function apiJson(url, options = {}) {
  const res = await apiFetch(url, options);
  let data = {};
  try {
    data = await res.json();
  } catch {}
  if (!res.ok) {
    const err = data.error || data.message || res.statusText;
    throw new Error(err);
  }
  return data;
}

export async function preflightOrBlock(operation) {
  const data = await apiJson(`/api/system/preflight?operation=${encodeURIComponent(operation)}`);
  if (!data.ok) {
    const gate = window.__librarainGate;
    if (gate) gate.show(operation, data.message || "Risorse insufficienti");
    return false;
  }
  return true;
}
