const BOX = "#dc3c3c";
const POINT = "#2878dc";
const TRAIL = "#28aa6e";
const TRAIL_START = "#1ec8ff";
const TRAIL_END = "#ff7a1a";
const TOOLS = { bbox: 1, point: 1, trail: 1 };
/** F-003: system header labels (COPILOT top 15%). Not seeded into chip dock. */
const TESTATA_PARI = "Testata Pari";
const TESTATA_DISPARI = "Testata Dispari";
const TOP_EDGE_FRAC = 0.15;
/** F-004: max suggestions in body-region COPILOT (top N by page frequency). */
const COPILOT_BODY_TOP_N = 5;

function mentionTokenLocal(name) {
  if (typeof mentionToken === "function") return mentionToken(name);
  return String(name || "").trim().replace(/\s+/g, "_") || "elemento";
}

function eachAnnotationEl(pages, fn) {
  Object.keys(pages || {}).forEach(function (key) {
    (pages[key] || []).forEach(function (el) {
      if (el) fn(el);
    });
  });
}

function countTokenOthers(pages, token, exceptId) {
  let n = 0;
  if (!token) return 0;
  eachAnnotationEl(pages, function (el) {
    if (el.id === exceptId) return;
    if (!String(el.name || "").trim()) return;
    if (mentionTokenLocal(el.name) === token) n += 1;
  });
  return n;
}

function lookupTagDefaultDescription(pages, token, exceptId) {
  let stamped = "";
  let fallback = "";
  if (!token) return "";
  eachAnnotationEl(pages, function (el) {
    if (exceptId && el.id === exceptId) return;
    if (!String(el.name || "").trim()) return;
    if (mentionTokenLocal(el.name) !== token) return;
    const dd = String(el.defaultDescription || "").trim();
    if (dd && !stamped) stamped = dd;
    const d = String(el.description || "").trim();
    if (d && !fallback) fallback = d;
  });
  return stamped || fallback;
}

function fillEmptyDescriptionFromTagDefault(el, pages) {
  if (!el || String(el.description || "").trim()) return false;
  const name = String(el.name || "").trim();
  if (!name) return false;
  const def = lookupTagDefaultDescription(pages, mentionTokenLocal(name), el.id);
  if (!def) return false;
  el.description = def;
  if (!String(el.defaultDescription || "").trim()) el.defaultDescription = def;
  return true;
}

function syncTagDefaultOnCommit(el, pages) {
  const name = String(el && el.name || "").trim();
  if (!name) return;
  const token = mentionTokenLocal(name);
  if (countTokenOthers(pages, token, el.id) === 0) {
    el.defaultDescription = String(el.description || "");
    return;
  }
  const def = lookupTagDefaultDescription(pages, token, el.id);
  if (!String(el.defaultDescription || "").trim() && def) {
    el.defaultDescription = def;
  }
}

function isBboxInTop15(el) {
  if (!el || el.type !== "bbox" || !el.coords || el.coords.length < 4) return false;
  const y1 = Number(el.coords[1]);
  const y2 = Number(el.coords[3]);
  if (!Number.isFinite(y1) || !Number.isFinite(y2)) return false;
  return Math.min(y1, y2) / 999 <= TOP_EDGE_FRAC;
}

function isTestataToken(token) {
  return token === mentionTokenLocal(TESTATA_PARI) || token === mentionTokenLocal(TESTATA_DISPARI);
}

/** F-004: top N tags by page frequency, excluding testate. */
function frequentCopilotLabels(pages) {
  const pageKeys = Object.keys(pages || {}).filter(function (key) {
    return (pages[key] || []).length > 0;
  });
  const nPages = pageKeys.length;
  if (!nPages) return [];

  const byToken = Object.create(null);
  pageKeys.forEach(function (pageKey) {
    const seenOnPage = Object.create(null);
    (pages[pageKey] || []).forEach(function (el) {
      const name = String(el && el.name || "").trim();
      if (!name) return;
      const token = mentionTokenLocal(name);
      if (isTestataToken(token) || seenOnPage[token]) return;
      seenOnPage[token] = true;
      if (!byToken[token]) byToken[token] = { name: name, pageCount: 0 };
      byToken[token].pageCount += 1;
    });
  });

  const out = Object.keys(byToken).map(function (token) {
    const entry = byToken[token];
    return { name: entry.name, freq: entry.pageCount / nPages };
  });
  out.sort(function (a, b) {
    if (b.freq !== a.freq) return b.freq - a.freq;
    return String(a.name).localeCompare(String(b.name), "it");
  });
  return out.slice(0, COPILOT_BODY_TOP_N).map(function (item) { return item.name; });
}

function uid(prefix) {
  return prefix + "_" + Math.random().toString(36).slice(2, 9);
}

function nextDefaultAnnotationName(type, elements, exceptId) {
  const prefix = String(type || "bbox");
  const used = Object.create(null);
  const re = new RegExp("^" + prefix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "(\\d+)$", "i");
  (elements || []).forEach(function (el) {
    if (!el || (exceptId && el.id === exceptId)) return;
    const m = String(el.name || "").trim().match(re);
    if (m) used[Number(m[1])] = true;
  });
  let n = 1;
  while (used[n]) n += 1;
  return prefix + n;
}

function clamp(n, lo, hi) {
  return Math.max(lo, Math.min(hi, n));
}

function toDeepSeek(x, y, width, height) {
  return [
    clamp(Math.round((x / Math.max(width - 1, 1)) * 999), 0, 999),
    clamp(Math.round((y / Math.max(height - 1, 1)) * 999), 0, 999),
  ];
}

function fromDeepSeek(nx, ny, width, height) {
  return [
    (Number(nx) / 999) * Math.max(width - 1, 1),
    (Number(ny) / 999) * Math.max(height - 1, 1),
  ];
}

export function createPageGuidanceController(bridge) {
  const state = {
    active: false,
    tool: "bbox",
    pages: {},
    selectedId: null,
    draft: null,
    trailPoints: null,
    trailCursor: null,
  };
  const annotationListeners = [];
  function notifyAnnotationsChange() {
    syncNotesFieldset();
    annotationListeners.forEach(function (cb) { try { cb(); } catch (_) {} });
  }

  const btn = document.getElementById("annotate-tool-btn");
  const switchEl = document.getElementById("annotate-primitive-switch");
  const canvas = document.getElementById("annotate-canvas");
  const guidanceField = document.querySelector('[name="ai_page_guidance"]');
  const notesFieldset = document.getElementById("model-notes-fieldset");
  const mdFormattingFieldset = document.getElementById("md-formatting-fieldset");
  if (!btn || !canvas || !switchEl) return null;
  const ctx = canvas.getContext("2d");
  let drag = null;

  function pageMap(page) {
    if (!state.pages[page]) state.pages[page] = [];
    return state.pages[page];
  }

  function defaultNameFor(type, page, exceptId) {
    return nextDefaultAnnotationName(type, pageMap(page), exceptId);
  }

  function clearTrailDraft() { state.trailPoints = null; state.trailCursor = null; }

  function currentPageHasElements() {
    const page = bridge.getDetailPage();
    if (!(page >= 1)) return false;
    return !!(state.pages[page] && state.pages[page].length);
  }

  function syncCanvasChrome() {
    const showCanvas = !!state.active || currentPageHasElements();
    canvas.classList.toggle("hidden", !showCanvas);
    canvas.classList.toggle("is-preview", showCanvas && !state.active);
    // F-004: annotate attivo → draw (pointer-events auto); idle → pan via page-zoom (pointer-events none).
    canvas.style.pointerEvents = state.active ? "auto" : "none";
    canvas.style.cursor = state.active ? "crosshair" : "default";
  }

  function pageHasTextAnnotations(page) {
    return typeof bridge.hasTextAnnotations === "function" && !!bridge.hasTextAnnotations(page);
  }

  function anyPageHasElements() {
    return Object.keys(state.pages).some(function (key) {
      return (state.pages[key] || []).length > 0;
    });
  }

  function shouldShowNotes() {
    // Mutual exclusivity with REICAT / Appendici in the DX context slot.
    const reicat = document.getElementById("reicat-metadata-fieldset");
    if (reicat && !reicat.classList.contains("hidden")) return false;
    const appendix = document.getElementById("appendix-sections-fieldset");
    if (appendix && !appendix.classList.contains("hidden")) return false;
    const page = bridge.getDetailPage();
    return !!state.active || anyPageHasElements() || pageHasTextAnnotations(page);
  }

  function syncNotesFieldset() {
    const show = shouldShowNotes();
    if (notesFieldset) {
      notesFieldset.classList.toggle("hidden", !show);
      const ta = notesFieldset.querySelector("textarea");
      if (ta) {
        ta.readOnly = false;
        ta.disabled = false;
      }
    }
    if (mdFormattingFieldset) mdFormattingFieldset.classList.toggle("hidden", !show);
    const tagsDock = document.getElementById("page-picker-tags-dock");
    if (tagsDock) {
      const wasHidden = tagsDock.classList.contains("hidden") || tagsDock.classList.contains("is-empty");
      const hasChips = !!tagsDock.querySelector(".mention-chip");
      tagsDock.classList.toggle("is-empty", !hasChips);
      tagsDock.classList.toggle("hidden", !show || !hasChips);
      const nowHidden = tagsDock.classList.contains("hidden") || tagsDock.classList.contains("is-empty");
      if (nowHidden) {
        const ws = document.getElementById("page-picker-workspace");
        if (ws) ws.style.removeProperty("--page-picker-tags-row");
      }
      if (wasHidden !== nowHidden) {
        window.dispatchEvent(new Event("resize"));
      }
    }
  }

  function setActive(on) {
    const next = !!on;
    if (next && typeof bridge.onAnnotateActivate === "function") bridge.onAnnotateActivate();
    state.active = next;
    btn.classList.toggle("is-active", state.active);
    switchEl.classList.toggle("hidden", !state.active);
    if (state.active) redraw();
    else {
      state.selectedId = null;
      state.draft = null;
      clearTrailDraft();
      hideNameInput();
      redraw();
    }
    syncCanvasChrome();
    syncNotesFieldset();
    if (typeof bridge.onAnnotateModeChange === "function") bridge.onAnnotateModeChange(state.active);
  }

  function setTool(tool) {
    const next = TOOLS[tool] ? tool : "bbox";
    if (state.tool !== next) {
      state.draft = null;
      clearTrailDraft();
      drag = null;
    }
    state.tool = next;
    switchEl.querySelectorAll("[data-annotate-tool]").forEach(function (el) {
      el.classList.toggle("is-active", el.getAttribute("data-annotate-tool") === state.tool);
    });
    if (state.active) redraw();
  }

  function syncCanvasSize() {
    const img = bridge.getDetailImageEl();
    const wrap = bridge.getDetailWrapEl();
    if (!img || !wrap || img.classList.contains("hidden") || !img.naturalWidth) {
      if (canvas.width !== 1 || canvas.height !== 1) {
        canvas.width = 1;
        canvas.height = 1;
      }
      return false;
    }
    const rect = img.getBoundingClientRect();
    const wrapRect = wrap.getBoundingClientRect();
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    canvas.style.left = Math.round(rect.left - wrapRect.left + wrap.scrollLeft) + "px";
    canvas.style.top = Math.round(rect.top - wrapRect.top + wrap.scrollTop) + "px";
    return true;
  }

  function drawLabel(x, y, text) {
    const label = String(text || "").replace(/\s+/g, " ").trim() || "?";
    ctx.font = "11px sans-serif";
    const tw = ctx.measureText(label).width;
    const top = Math.max(0, y - 16);
    ctx.fillStyle = "rgba(20,20,20,0.9)";
    ctx.fillRect(x, top, tw + 6, 14);
    ctx.fillStyle = "#fff";
    ctx.fillText(label, x + 3, top + 11);
  }

  function drawElement(el, width, height, selected) {
    const color = el.type === "bbox" ? BOX : el.type === "trail" ? TRAIL : POINT;
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = selected ? 3 : 2;
    if (el.type === "bbox") {
      const p1 = fromDeepSeek(el.coords[0], el.coords[1], width, height);
      const p2 = fromDeepSeek(el.coords[2], el.coords[3], width, height);
      const x = Math.min(p1[0], p2[0]);
      const y = Math.min(p1[1], p2[1]);
      ctx.strokeRect(x, y, Math.abs(p2[0] - p1[0]), Math.abs(p2[1] - p1[1]));
      drawLabel(x, y, el.name);
    } else if (el.type === "point") {
      const p = fromDeepSeek(el.coords[0], el.coords[1], width, height);
      ctx.beginPath();
      ctx.arc(p[0], p[1], 5, 0, Math.PI * 2);
      ctx.stroke();
      drawLabel(p[0] + 8, p[1], el.name);
    } else if (el.type === "trail") {
      const pts = (el.coords || []).map(function (pair) {
        return fromDeepSeek(pair[0], pair[1], width, height);
      });
      if (!pts.length) return;
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
      ctx.stroke();
      for (let i = 0; i < pts.length; i++) {
        const isEnd = i === 0 || i === pts.length - 1;
        ctx.fillStyle = i === 0 ? TRAIL_START : i === pts.length - 1 ? TRAIL_END : TRAIL;
        ctx.beginPath();
        ctx.arc(pts[i][0], pts[i][1], selected && isEnd ? 7 : 5, 0, Math.PI * 2);
        ctx.fill();
      }
      drawLabel(pts[0][0], pts[0][1], el.name);
    }
  }

  function redraw(syncInput) {
    const page = bridge.getDetailPage();
    const list = page >= 1 ? (state.pages[page] || []) : [];
    const canDraw = !!state.active || list.length > 0;
    if (!canDraw || !syncCanvasSize()) {
      hideNameInput();
      syncCanvasChrome();
      return;
    }
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    if (!page) {
      hideNameInput();
      syncCanvasChrome();
      return;
    }
    list.forEach(function (el) {
      drawElement(el, canvas.width, canvas.height, state.active && el.id === state.selectedId);
    });
    if (state.active && state.draft && state.draft.type === "bbox") {
      drawElement(state.draft, canvas.width, canvas.height, true);
    }
    if (state.active && state.trailPoints && state.trailPoints.length) {
      const preview = state.trailPoints.slice();
      if (state.trailCursor) preview.push(state.trailCursor);
      drawElement({ type: "trail", name: "…", coords: preview }, canvas.width, canvas.height, true);
    }
    syncCanvasChrome();
    if (!state.active || syncInput === false) {
      if (!state.active) hideNameInput();
      return;
    }
    const selected = list.find(function (el) {
      return el.id === state.selectedId;
    });
    if (selected) showNameInputFor(selected);
    else hideNameInput();
  }

  function localPoint(ev) {
    const rect = canvas.getBoundingClientRect();
    return [ev.clientX - rect.left, ev.clientY - rect.top];
  }

  function distToSegment(x, y, ax, ay, bx, by) {
    const dx = bx - ax;
    const dy = by - ay;
    const len2 = dx * dx + dy * dy;
    if (len2 <= 0) return Math.hypot(x - ax, y - ay);
    let t = ((x - ax) * dx + (y - ay) * dy) / len2;
    t = clamp(t, 0, 1);
    return Math.hypot(x - (ax + t * dx), y - (ay + t * dy));
  }

  function hitTest(page, x, y) {
    const list = pageMap(page);
    const w = canvas.width;
    const h = canvas.height;
    for (let i = list.length - 1; i >= 0; i--) {
      const el = list[i];
      if (el.type === "bbox") {
        const p1 = fromDeepSeek(el.coords[0], el.coords[1], w, h);
        const p2 = fromDeepSeek(el.coords[2], el.coords[3], w, h);
        if (x >= Math.min(p1[0], p2[0]) && x <= Math.max(p1[0], p2[0]) && y >= Math.min(p1[1], p2[1]) && y <= Math.max(p1[1], p2[1])) return el;
      } else if (el.type === "point") {
        const p = fromDeepSeek(el.coords[0], el.coords[1], w, h);
        if (Math.hypot(p[0] - x, p[1] - y) <= 8) return el;
      } else if (el.type === "trail") {
        const pts = (el.coords || []).map(function (pair) { return fromDeepSeek(pair[0], pair[1], w, h); });
        for (let j = 0; j < pts.length; j++) {
          if (Math.hypot(pts[j][0] - x, pts[j][1] - y) <= 10) return el;
          if (j > 0 && distToSegment(x, y, pts[j - 1][0], pts[j - 1][1], pts[j][0], pts[j][1]) <= 8) return el;
        }
      }
    }
    return null;
  }

  function removeAnnotation(page, id) {
    const pageNum = Number(page);
    const elementId = String(id || "");
    if (!(pageNum >= 1) || !elementId) return false;
    const list = pageMap(pageNum);
    const next = list.filter(function (el) { return String(el.id) !== elementId; });
    if (next.length === list.length) return false;
    if (next.length) state.pages[pageNum] = next;
    else delete state.pages[pageNum];
    if (state.selectedId === elementId) {
      state.selectedId = null;
      hideNameInput();
    }
    notifyAnnotationsChange();
    if (state.active) redraw();
    return true;
  }

  /** F-002: drop annotations left on a page with empty (trim) title. */
  function removeEmptyNamedOnPage(page) {
    const pageNum = Number(page);
    if (!(pageNum >= 1)) return false;
    const list = state.pages[pageNum];
    if (!list || !list.length) return false;
    let removedSelected = false;
    const next = list.filter(function (el) {
      if (String(el.name || "").trim() !== "") return true;
      if (state.selectedId && String(el.id) === String(state.selectedId)) removedSelected = true;
      return false;
    });
    if (next.length === list.length) return false;
    if (next.length) state.pages[pageNum] = next;
    else delete state.pages[pageNum];
    if (removedSelected) {
      state.selectedId = null;
      hideNameInput();
    }
    return true;
  }

  function deleteSelected() {
    const page = bridge.getDetailPage();
    if (!page || !state.selectedId) return false;
    return removeAnnotation(page, state.selectedId);
  }

  function resetPage() {
    const page = bridge.getDetailPage();
    if (!page) return;
    clearPageAnnotations(page);
  }

  function clearPageAnnotations(page) {
    const pageNum = Number(page);
    if (!(pageNum >= 1)) return;
    delete state.pages[pageNum];
    if (Number(bridge.getDetailPage()) === pageNum) {
      state.selectedId = null;
      state.draft = null;
      clearTrailDraft();
      hideNameInput();
    }
    redraw();
    notifyAnnotationsChange();
  }

  function clientLog(message, details, level) {
    const payload = {
      level: level || "info",
      message: String(message || "copilot-debug"),
      ts: new Date().toISOString(),
      path: String(window.location && window.location.pathname || ""),
      details: details && typeof details === "object" ? details : { value: details },
    };
    try {
      if (window.LibrarAInLog && typeof window.LibrarAInLog.info === "function") {
        window.LibrarAInLog[level === "error" ? "error" : level === "warn" ? "warn" : "info"](
          "copilot-debug: " + payload.message,
          payload.details
        );
      }
    } catch (_) {}
    try {
      const body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        const blob = new Blob([body], { type: "application/json" });
        navigator.sendBeacon("/api/client-log", blob);
        return;
      }
      fetch("/api/client-log", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
        keepalive: true,
      }).catch(function () {});
    } catch (_) {}
  }

  function describeEl(node) {
    if (!node || !node.tagName) return null;
    return {
      tag: String(node.tagName).toLowerCase(),
      id: node.id || "",
      className: typeof node.className === "string" ? node.className.slice(0, 120) : "",
      copilotLabel: node.getAttribute && node.getAttribute("data-copilot-label") || "",
    };
  }

  const chromeLayer = document.createElement("div");
  chromeLayer.className = "annotate-chrome-layer hidden";

  const copilotPanel = document.createElement("div");
  copilotPanel.className = "annotate-copilot hidden";
  copilotPanel.setAttribute("role", "group");
  copilotPanel.setAttribute("aria-label", "COPILOT");

  const copilotTitle = document.createElement("div");
  copilotTitle.className = "annotate-copilot-title";
  copilotTitle.textContent = "COPILOT";
  copilotPanel.appendChild(copilotTitle);

  const copilotSuggestions = document.createElement("div");
  copilotSuggestions.className = "annotate-copilot-suggestions";
  copilotPanel.appendChild(copilotSuggestions);

  function renderCopilotButtons(labels, opts) {
    const asTestata = !!(opts && opts.testata);
    copilotSuggestions.textContent = "";
    clientLog("copilot-render", { labels: labels || [], testata: asTestata });
    (labels || []).forEach(function (label) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "annotate-copilot-btn";
      btn.textContent = label;
      btn.title = (asTestata ? "Testata di pagina · @" : "@") + mentionTokenLocal(label);
      btn.setAttribute("data-copilot-label", label);
      // Apply on pointerdown. Do NOT preventDefault on pointerdown: that suppresses
      // subsequent mouse click events (and was making COPILOT appear dead).
      btn.addEventListener("pointerdown", function (ev) {
        if (ev.pointerType === "mouse" && ev.button !== 0) return;
        ev.stopPropagation();
        clientLog("copilot-pointerdown", {
          label: label,
          pointerType: ev.pointerType || "",
          button: ev.button,
          selectedId: state.selectedId || "",
          nameBefore: nameInput.value || "",
          underPoint: describeEl(document.elementFromPoint(ev.clientX, ev.clientY)),
        });
        const ok = applyNameFromPool(label);
        clientLog("copilot-apply-result", {
          label: label,
          ok: !!ok,
          selectedId: state.selectedId || "",
          nameAfter: nameInput.value || "",
        });
      });
      btn.addEventListener("mousedown", function (ev) {
        // Keep title focused; safe here because apply already ran on pointerdown.
        ev.preventDefault();
        ev.stopPropagation();
        clientLog("copilot-mousedown", { label: label, button: ev.button });
      });
      btn.addEventListener("click", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        clientLog("copilot-click", { label: label });
      });
      copilotSuggestions.appendChild(btn);
    });
  }

  // Capture-phase probe: if the user clicks near the editor but the target is canvas
  // (or something else), we see it in the server log.
  if (!window.__librarainCopilotClickProbe) {
    window.__librarainCopilotClickProbe = true;
    document.addEventListener(
      "pointerdown",
      function (ev) {
        const t = ev.target;
        if (!t || !t.closest) return;
        if (!t.closest(".annotate-chrome-layer") && !t.closest("#annotate-canvas")) return;
        clientLog("pointerdown-probe", {
          target: describeEl(t),
          closestCopilot: !!t.closest(".annotate-copilot-btn"),
          closestChrome: !!t.closest(".annotate-chrome-layer"),
          closestTrash: !!t.closest(".annotate-trash-tab"),
          closestCanvas: !!t.closest("#annotate-canvas"),
          underPoint: describeEl(document.elementFromPoint(ev.clientX, ev.clientY)),
          x: ev.clientX,
          y: ev.clientY,
        });
      },
      true
    );
  }

  const nameEditor = document.createElement("div");
  nameEditor.className = "annotate-name-editor";
  nameEditor.setAttribute("role", "group");
  nameEditor.setAttribute("aria-label", "Titolo e descrizione annotazione");

  const nameInput = document.createElement("textarea");
  nameInput.rows = 1;
  nameInput.className = "annotate-name-title";
  nameInput.maxLength = 80;
  nameInput.setAttribute("aria-label", "Titolo (tag)");
  nameInput.setAttribute("placeholder", "Titolo / tag");
  nameInput.setAttribute("spellcheck", "false");

  const descInput = document.createElement("textarea");
  descInput.rows = 2;
  descInput.className = "annotate-name-desc";
  descInput.maxLength = 500;
  descInput.setAttribute("aria-label", "Descrizione (istruzioni modello)");
  descInput.setAttribute("placeholder", "Descrizione / istruzioni modello");
  descInput.setAttribute("spellcheck", "true");

  nameEditor.appendChild(nameInput);
  nameEditor.appendChild(descInput);

  const trashTab = document.createElement("button");
  trashTab.type = "button";
  trashTab.className = "annotate-trash-tab";
  trashTab.setAttribute("aria-label", "Elimina annotazione");
  trashTab.title = "Elimina annotazione";
  trashTab.innerHTML =
    '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
    '<path fill="currentColor" d="M9 3v1H4v2h16V4h-5V3H9zm2 5v10h2V8h-2zm4 0v10h2V8h-2zM7 8v10h2V8H7zm-2 14h14V7H5v15z"/>' +
    "</svg>";
  trashTab.addEventListener("pointerdown", function (ev) {
    if (ev.pointerType === "mouse" && ev.button !== 0) return;
    ev.stopPropagation();
  });
  trashTab.addEventListener("mousedown", function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
  });
  trashTab.addEventListener("click", function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    deleteSelected();
  });

  chromeLayer.appendChild(copilotPanel);
  chromeLayer.appendChild(nameEditor);
  chromeLayer.appendChild(trashTab);
  const wrap = bridge.getDetailWrapEl();
  if (wrap) wrap.appendChild(chromeLayer);

  function hideNameInput() {
    chromeLayer.classList.add("hidden");
    copilotPanel.classList.add("hidden");
    nameInput.blur();
    descInput.blur();
  }

  function syncCopilotFor(el) {
    // F-003: top 15% → Testata Pari/Dispari. F-004: below → top-5 frequent non-testata tags.
    // US-4: never seed testate into chip dock.
    if (isBboxInTop15(el)) {
      renderCopilotButtons([TESTATA_PARI, TESTATA_DISPARI], { testata: true });
      copilotPanel.classList.remove("hidden");
      return;
    }
    if (el && el.type === "bbox") {
      const labels = frequentCopilotLabels(state.pages);
      if (!labels.length) {
        copilotPanel.classList.add("hidden");
        return;
      }
      renderCopilotButtons(labels);
      copilotPanel.classList.remove("hidden");
      return;
    }
    copilotPanel.classList.add("hidden");
  }

  function autosizeField(el, minPx, maxPx) {
    el.style.height = "auto";
    const next = Math.max(minPx, Math.min(maxPx, el.scrollHeight));
    el.style.height = next + "px";
  }

  function autosizeNameEditor() {
    autosizeField(nameInput, 22, 72);
    autosizeField(descInput, 36, 160);
  }

  /** Bbox (or point/trail anchor) in wrap coordinates for chrome layout. */
  function bboxRectInWrap(el) {
    const w = canvas.width;
    const h = canvas.height;
    const ox = canvas.offsetLeft;
    const oy = canvas.offsetTop;
    let left = ox;
    let top = oy;
    let right = ox + 8;
    let bottom = oy + 8;
    if (el.type === "bbox") {
      const p1 = fromDeepSeek(el.coords[0], el.coords[1], w, h);
      const p2 = fromDeepSeek(el.coords[2], el.coords[3], w, h);
      left = ox + Math.min(p1[0], p2[0]);
      top = oy + Math.min(p1[1], p2[1]);
      right = ox + Math.max(p1[0], p2[0]);
      bottom = oy + Math.max(p1[1], p2[1]);
    } else if (el.type === "point") {
      const p = fromDeepSeek(el.coords[0], el.coords[1], w, h);
      left = ox + p[0] - 4;
      top = oy + p[1] - 4;
      right = ox + p[0] + 4;
      bottom = oy + p[1] + 4;
    } else if (el.type === "trail" && el.coords && el.coords[0]) {
      const p = fromDeepSeek(el.coords[0][0], el.coords[0][1], w, h);
      left = ox + p[0] - 4;
      top = oy + p[1] - 4;
      right = ox + p[0] + 4;
      bottom = oy + p[1] + 4;
    }
    return {
      left: left,
      top: top,
      right: right,
      bottom: bottom,
      width: Math.max(1, right - left),
      height: Math.max(1, bottom - top),
    };
  }

  function layoutChromeAround(el) {
    if (!el || chromeLayer.classList.contains("hidden") || !wrap) return;
    const pad = 4;
    const gap = 4;
    const rect = bboxRectInWrap(el);
    const wrapW = wrap.clientWidth || 0;
    const wrapH = wrap.clientHeight || 0;

    // Notes: under bbox bottom, left-aligned; flip above if too low.
    nameEditor.classList.remove("is-flipped-y");
    const notesW = nameEditor.offsetWidth || 176;
    const notesH = nameEditor.offsetHeight || 80;
    let notesLeft = rect.left;
    let notesTop = rect.bottom + gap;
    if (notesTop + notesH > wrapH - pad) {
      notesTop = rect.top - gap - notesH;
      nameEditor.classList.add("is-flipped-y");
    }
    if (notesLeft + notesW > wrapW - pad) notesLeft = wrapW - pad - notesW;
    if (notesLeft < pad) notesLeft = pad;
    if (notesTop < pad) notesTop = pad;
    if (notesTop + notesH > wrapH - pad) notesTop = Math.max(pad, wrapH - pad - notesH);
    nameEditor.style.left = Math.round(notesLeft) + "px";
    nameEditor.style.top = Math.round(notesTop) + "px";

    // COPILOT: left of bbox, bottom-aligned; flip to right if too far left.
    if (!copilotPanel.classList.contains("hidden")) {
      copilotPanel.classList.remove("is-flipped-x");
      const cw = copilotPanel.offsetWidth || 120;
      const ch = copilotPanel.offsetHeight || 60;
      let copilotLeft = rect.left - gap - cw;
      let copilotTop = rect.bottom - ch;
      if (copilotLeft < pad) {
        copilotLeft = rect.right + gap;
        copilotPanel.classList.add("is-flipped-x");
      }
      if (copilotLeft + cw > wrapW - pad) {
        copilotLeft = Math.max(pad, wrapW - pad - cw);
      }
      if (copilotTop < pad) copilotTop = pad;
      if (copilotTop + ch > wrapH - pad) {
        copilotTop = Math.max(pad, wrapH - pad - ch);
      }
      copilotPanel.style.left = Math.round(copilotLeft) + "px";
      copilotPanel.style.top = Math.round(copilotTop) + "px";
    }

    // Trash tab: top-right of bbox; flip to top-left if too far right.
    trashTab.classList.remove("is-flipped-x");
    const tw = trashTab.offsetWidth || 26;
    const th = trashTab.offsetHeight || 26;
    let trashLeft = rect.right;
    let trashTop = rect.top;
    if (trashLeft + tw > wrapW - pad) {
      trashLeft = rect.left - tw;
      trashTab.classList.add("is-flipped-x");
    }
    if (trashLeft < pad) trashLeft = pad;
    if (trashTop < pad) trashTop = pad;
    if (trashTop + th > wrapH - pad) trashTop = Math.max(pad, wrapH - pad - th);
    trashTab.style.left = Math.round(trashLeft) + "px";
    trashTab.style.top = Math.round(trashTop) + "px";
  }

  function showNameInputFor(el) {
    if (!el || !canvas.width || canvas.width < 2) { hideNameInput(); return; }
    state.selectedId = el.id;
    const active = document.activeElement;
    const editingHere = active === nameInput || active === descInput;
    if (!editingHere) {
      nameInput.value = el.name || "";
      descInput.value = el.description || "";
    }
    chromeLayer.classList.remove("hidden");
    syncCopilotFor(el);
    autosizeNameEditor();
    layoutChromeAround(el);
    // Second pass after paint so offsetWidth/Height reflect real content.
    window.requestAnimationFrame(function () {
      if (state.selectedId === el.id && !chromeLayer.classList.contains("hidden")) {
        layoutChromeAround(el);
      }
    });
  }

  function commitNameFromInput() {
    const page = bridge.getDetailPage();
    if (!page || !state.selectedId) return;
    const el = pageMap(page).find(function (item) {
      return item.id === state.selectedId;
    });
    if (!el) return;
    const prevName = String(el.name || "");
    const typed = String(nameInput.value || "");
    // F-002: empty trim stays empty — do not call defaultNameFor / rewrite nameInput with bboxN.
    el.name = typed.trim() ? typed : "";
    el.description = String(descInput.value || "");
    if (el.name !== prevName && el.name && fillEmptyDescriptionFromTagDefault(el, state.pages)) {
      descInput.value = el.description || "";
    }
    if (el.name) syncTagDefaultOnCommit(el, state.pages);
    redraw(false);
    notifyAnnotationsChange();
  }

  function onEditorInput() {
    commitNameFromInput();
    autosizeNameEditor();
    const page = bridge.getDetailPage();
    const el = page && state.selectedId
      ? pageMap(page).find(function (item) { return item.id === state.selectedId; })
      : null;
    if (el) layoutChromeAround(el);
  }

  nameInput.addEventListener("input", onEditorInput);
  descInput.addEventListener("input", onEditorInput);
  chromeLayer.addEventListener("mousedown", function (ev) {
    ev.stopPropagation();
  });
  function onEditorKeydown(ev) {
    ev.stopPropagation();
    if (ev.key === "Escape") {
      ev.preventDefault();
      hideNameInput();
    }
  }
  nameInput.addEventListener("keydown", onEditorKeydown);
  descInput.addEventListener("keydown", onEditorKeydown);

  function isNameInputActive() {
    if (chromeLayer.classList.contains("hidden")) return false;
    const active = document.activeElement;
    return active === nameInput || active === descInput;
  }

  function applyNameFromPool(token) {
    const page = bridge.getDetailPage();
    const name = String(token || "").trim();
    if (!name || !page) {
      clientLog("applyNameFromPool-bail", { reason: !name ? "empty-name" : "no-page", token: token || "" }, "warn");
      return false;
    }
    let el = state.selectedId
      ? pageMap(page).find(function (item) { return item.id === state.selectedId; })
      : null;
    // Fallback: if selection was lost but the name editor is open, use the only
    // unnamed (or sole) annotation on the page being edited.
    if (!el && !chromeLayer.classList.contains("hidden")) {
      const list = pageMap(page);
      el = list.find(function (item) { return item && String(item.name || "").trim() === ""; })
        || (list.length === 1 ? list[0] : null);
      if (el) state.selectedId = el.id;
      clientLog("applyNameFromPool-fallback", {
        found: !!el,
        id: el && el.id || "",
        pageAnnCount: list.length,
      });
    }
    if (!el) {
      clientLog("applyNameFromPool-bail", {
        reason: "no-el",
        selectedId: state.selectedId || "",
        shellHidden: chromeLayer.classList.contains("hidden"),
        page: page,
      }, "warn");
      return false;
    }
    el.name = name;
    nameInput.value = name;
    fillEmptyDescriptionFromTagDefault(el, state.pages);
    syncTagDefaultOnCommit(el, state.pages);
    descInput.value = el.description || "";
    chromeLayer.classList.remove("hidden");
    autosizeNameEditor();
    layoutChromeAround(el);
    nameInput.focus();
    const len = nameInput.value.length;
    nameInput.setSelectionRange(len, len);
    // Defer redraw/chip refresh so we don't rebuild COPILOT buttons mid-click
    // (that retargets the gesture onto the canvas underneath).
    window.requestAnimationFrame(function () {
      if (state.selectedId === el.id) {
        syncCopilotFor(el);
        layoutChromeAround(el);
      }
      redraw(false);
      notifyAnnotationsChange();
    });
    return true;
  }

  function promptRename(el) {
    state.selectedId = el.id;
    showNameInputFor(el);
    nameInput.focus();
    nameInput.select();
  }

  function finishTrail() {
    const page = bridge.getDetailPage();
    if (!page || !state.trailPoints || state.trailPoints.length < 2) {
      clearTrailDraft();
      redraw();
      return;
    }
    // F-002: create with empty name; promptRename opens empty selectable field (no defaultNameFor until user types).
    const el = { id: uid("trail"), type: "trail", name: "", coords: state.trailPoints.slice() };
    pageMap(page).push(el);
    clearTrailDraft();
    promptRename(el);
    redraw();
    notifyAnnotationsChange();
  }

  canvas.addEventListener("mousedown", function (ev) {
    if (!state.active || ev.button !== 0) return;
    const page = bridge.getDetailPage();
    if (!page) return;
    const pt = localPoint(ev);
    if (state.tool === "trail" && state.trailPoints && ev.detail >= 2) {
      finishTrail();
      return;
    }
    const hit = hitTest(page, pt[0], pt[1]);
    if (hit && !(state.tool === "trail" && state.trailPoints)) {
      // Existing shapes: select / rename only — no move or resize drag (F-001).
      state.selectedId = hit.id;
      if (ev.detail === 2) promptRename(hit);
      redraw();
      return;
    }
    state.selectedId = null;
    if (state.tool === "bbox") {
      const a = toDeepSeek(pt[0], pt[1], canvas.width, canvas.height);
      // F-002: empty name; rename after draw commit opens empty field.
      state.draft = { id: uid("bbox"), type: "bbox", name: "", coords: [a[0], a[1], a[0], a[1]] };
      drag = { mode: "bbox", start: pt };
    } else if (state.tool === "point") {
      const el = {
        id: uid("point"),
        type: "point",
        name: "",
        coords: toDeepSeek(pt[0], pt[1], canvas.width, canvas.height),
      };
      pageMap(page).push(el);
      promptRename(el);
      notifyAnnotationsChange();
    } else if (state.tool === "trail") {
      const next = toDeepSeek(pt[0], pt[1], canvas.width, canvas.height);
      if (!state.trailPoints) state.trailPoints = [next];
      else state.trailPoints.push(next);
      state.trailCursor = null;
    }
    redraw();
  });

  canvas.addEventListener("mousemove", function (ev) {
    const pt = localPoint(ev);
    if (!drag && state.tool === "trail" && state.trailPoints && state.trailPoints.length) {
      state.trailCursor = toDeepSeek(pt[0], pt[1], canvas.width, canvas.height);
      redraw(false);
      return;
    }
    if (!drag) return;
    if (drag.mode === "bbox" && state.draft) {
      const a = toDeepSeek(drag.start[0], drag.start[1], canvas.width, canvas.height);
      const b = toDeepSeek(pt[0], pt[1], canvas.width, canvas.height);
      state.draft.coords = [a[0], a[1], b[0], b[1]];
    } else if (drag.mode === "move" && drag.el) {
      const dnx = Math.round(((pt[0] - drag.start[0]) / Math.max(canvas.width - 1, 1)) * 999);
      const dny = Math.round(((pt[1] - drag.start[1]) / Math.max(canvas.height - 1, 1)) * 999);
      if (drag.el.type === "bbox") {
        drag.el.coords = [clamp(drag.origin[0] + dnx, 0, 999), clamp(drag.origin[1] + dny, 0, 999), clamp(drag.origin[2] + dnx, 0, 999), clamp(drag.origin[3] + dny, 0, 999)];
      } else if (drag.el.type === "point") {
        drag.el.coords = [clamp(drag.origin[0] + dnx, 0, 999), clamp(drag.origin[1] + dny, 0, 999)];
      } else if (drag.el.type === "trail") {
        drag.el.coords = drag.origin.map(function (pair) { return [clamp(pair[0] + dnx, 0, 999), clamp(pair[1] + dny, 0, 999)]; });
      }
    } else if (drag.mode === "resize" && drag.el) {
      const b = toDeepSeek(pt[0], pt[1], canvas.width, canvas.height);
      drag.el.coords = [drag.origin[0], drag.origin[1], b[0], b[1]];
    }
    redraw();
  });

  function endDrag() {
    if (!drag) return;
    const page = bridge.getDetailPage();
    let mutated = false;
    if (drag.mode === "bbox" && state.draft && page) {
      const c = state.draft.coords;
      if (Math.abs(c[0] - c[2]) > 4 || Math.abs(c[1] - c[3]) > 4) {
        pageMap(page).push(state.draft);
        promptRename(state.draft);
        mutated = true;
      }
      state.draft = null;
    } else if (drag.mode === "move" || drag.mode === "resize") {
      // Legacy paths: no longer started from mousedown (F-001); clear residual only.
      mutated = true;
    }
    drag = null;
    redraw();
    if (mutated) notifyAnnotationsChange();
  }

  canvas.addEventListener("mouseup", endDrag);
  // Release outside canvas must not leave create-draw (or residual) drag stuck to the cursor.
  window.addEventListener("mouseup", endDrag);
  canvas.addEventListener("mouseleave", function () {
    if (!drag) return;
    // Only clear if pointer left during a residual move/resize; bbox create finishes on window mouseup.
    if (drag.mode === "move" || drag.mode === "resize") endDrag();
  });

  document.addEventListener("keydown", function (ev) {
    if (!state.active) return;
    const tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (state.trailPoints) {
      if (ev.key === "Enter" || ev.code === "NumpadEnter") {
        ev.preventDefault();
        finishTrail();
        return;
      }
      if (ev.key === "Escape") {
        ev.preventDefault();
        clearTrailDraft();
        redraw();
        return;
      }
    }
    if ((ev.key === "Delete" || ev.key === "Backspace") && state.selectedId) {
      ev.preventDefault();
      deleteSelected();
    }
  });

  btn.addEventListener("click", function () { setActive(!state.active); });
  switchEl.addEventListener("click", function (ev) {
    const action = ev.target.closest("[data-annotate-action]");
    if (action) { if (action.getAttribute("data-annotate-action") === "reset-page") resetPage(); return; }
    const tool = ev.target.closest("[data-annotate-tool]");
    if (tool) setTool(tool.getAttribute("data-annotate-tool"));
  });
  setTool("bbox");
  let lastDetailPage = Number(bridge.getDetailPage()) || null;
  bridge.onDetailChange(function () {
    const left = lastDetailPage;
    const current = Number(bridge.getDetailPage()) || null;
    let purged = false;
    // F-002: leaving a page with trim-empty title deletes that annotation (not on Escape/blur alone).
    if (left >= 1 && left !== current) {
      purged = removeEmptyNamedOnPage(left);
    }
    lastDetailPage = current;
    if (purged) notifyAnnotationsChange();
    redraw();
    syncNotesFieldset();
  });
  bridge.onPdfReset(function () {
    state.pages = {}; state.selectedId = null; state.draft = null; clearTrailDraft();
    lastDetailPage = null;
    if (guidanceField) guidanceField.value = "";
    setActive(false); notifyAnnotationsChange();
  });
  function annotationsPayload() {
    return Object.keys(state.pages).map(Number).filter(function (page) { return (state.pages[page] || []).length > 0; }).sort(function (a, b) { return a - b; }).map(function (page) { return { page: page, elements: (state.pages[page] || []).slice() }; });
  }
  function setAnnotations(pages) {
    const next = {};
    (Array.isArray(pages) ? pages : []).forEach(function (item) {
      if (!item || typeof item !== "object") return;
      const page = Number(item.page);
      if (!(page >= 1)) return;
      const elements = Array.isArray(item.elements) ? item.elements.slice() : [];
      if (elements.length) next[page] = elements;
    });
    state.pages = next;
    state.selectedId = null;
    state.draft = null;
    clearTrailDraft();
    notifyAnnotationsChange();
    redraw();
  }
  window.addEventListener("resize", function () { redraw(); });
  syncCanvasChrome();
  syncNotesFieldset();
  return {
    getAnnotations: annotationsPayload,
    setAnnotations: setAnnotations,
    setActive: setActive,
    isActive: function () { return !!state.active; },
    isNameInputActive: isNameInputActive,
    applyNameFromPool: applyNameFromPool,
    removeAnnotation: removeAnnotation,
    clearPageAnnotations: clearPageAnnotations,
    redraw: redraw,
    syncNotesFieldset: syncNotesFieldset,
    onAnnotationsChange: function (cb) { if (typeof cb === "function") annotationListeners.push(cb); },
  };
}
