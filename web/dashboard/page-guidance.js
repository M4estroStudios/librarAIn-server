const BOX = "#dc3c3c";
const POINT = "#2878dc";
const TRAIL = "#28aa6e";
const TRAIL_START = "#1ec8ff";
const TRAIL_END = "#ff7a1a";
const TOOLS = { bbox: 1, point: 1, trail: 1 };

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

  const nameEditor = document.createElement("div");
  nameEditor.className = "annotate-name-editor hidden";
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
  const wrap = bridge.getDetailWrapEl();
  if (wrap) wrap.appendChild(nameEditor);

  function hideNameInput() {
    nameEditor.classList.add("hidden");
    nameInput.blur();
    descInput.blur();
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

  function editorAnchorFor(el) {
    const w = canvas.width;
    const h = canvas.height;
    let x = 0;
    let y = 0;
    if (el.type === "bbox") {
      const p1 = fromDeepSeek(el.coords[0], el.coords[1], w, h);
      const p2 = fromDeepSeek(el.coords[2], el.coords[3], w, h);
      x = Math.min(p1[0], p2[0]);
      y = Math.min(p1[1], p2[1]);
    } else if (el.type === "point") {
      const p = fromDeepSeek(el.coords[0], el.coords[1], w, h);
      x = p[0];
      y = p[1];
    } else if (el.type === "trail" && el.coords && el.coords[0]) {
      const p = fromDeepSeek(el.coords[0][0], el.coords[0][1], w, h);
      x = p[0];
      y = p[1];
    }
    return { x: x, y: y };
  }

  function showNameInputFor(el) {
    if (!el || !canvas.width || canvas.width < 2) { hideNameInput(); return; }
    const anchor = editorAnchorFor(el);
    const active = document.activeElement;
    const editingHere = active === nameInput || active === descInput;
    if (!editingHere) {
      nameInput.value = el.name || "";
      descInput.value = el.description || "";
    }
    nameEditor.classList.remove("hidden");
    nameEditor.style.left = Math.round(canvas.offsetLeft + anchor.x) + "px";
    nameEditor.style.top = Math.max(0, Math.round(canvas.offsetTop + anchor.y - 8)) + "px";
    autosizeNameEditor();
  }

  function commitNameFromInput() {
    const page = bridge.getDetailPage();
    if (!page || !state.selectedId) return;
    const el = pageMap(page).find(function (item) {
      return item.id === state.selectedId;
    });
    if (!el) return;
    const typed = String(nameInput.value || "");
    el.name = typed.trim() ? typed : defaultNameFor(el.type, page, el.id);
    if (!typed.trim()) nameInput.value = el.name;
    el.description = String(descInput.value || "");
    redraw(false);
    notifyAnnotationsChange();
  }

  function onEditorInput() {
    commitNameFromInput();
    autosizeNameEditor();
  }

  nameInput.addEventListener("input", onEditorInput);
  descInput.addEventListener("input", onEditorInput);
  nameEditor.addEventListener("mousedown", function (ev) {
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
    if (nameEditor.classList.contains("hidden")) return false;
    const active = document.activeElement;
    return active === nameInput || active === descInput;
  }

  function applyNameFromPool(token) {
    const page = bridge.getDetailPage();
    if (!page || !state.selectedId) return false;
    const el = pageMap(page).find(function (item) {
      return item.id === state.selectedId;
    });
    if (!el) return false;
    const name = String(token || "").trim();
    if (!name) return false;
    el.name = name;
    nameInput.value = name;
    if (!String(el.description || "").trim()) {
      const tokenKey = name.replace(/\s+/g, "_");
      Object.keys(state.pages).some(function (pageKey) {
        return (state.pages[pageKey] || []).some(function (other) {
          if (!other || other.id === el.id) return false;
          const otherToken = String(other.name || "").trim().replace(/\s+/g, "_");
          if (otherToken !== tokenKey) return false;
          if (!String(other.description || "").trim()) return false;
          el.description = other.description;
          descInput.value = el.description;
          return true;
        });
      });
    } else {
      descInput.value = el.description || "";
    }
    nameEditor.classList.remove("hidden");
    autosizeNameEditor();
    redraw(false);
    notifyAnnotationsChange();
    nameInput.focus();
    const len = nameInput.value.length;
    nameInput.setSelectionRange(len, len);
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
    const el = { id: uid("trail"), type: "trail", name: defaultNameFor("trail", page), coords: state.trailPoints.slice() };
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
      state.selectedId = hit.id;
      if (hit.type === "bbox") {
        const p2 = fromDeepSeek(hit.coords[2], hit.coords[3], canvas.width, canvas.height);
        if (Math.hypot(p2[0] - pt[0], p2[1] - pt[1]) <= 10) {
          drag = { mode: "resize", el: hit, start: pt, origin: hit.coords.slice() };
        } else {
          drag = { mode: "move", el: hit, start: pt, origin: JSON.parse(JSON.stringify(hit.coords)) };
        }
      } else {
        drag = { mode: "move", el: hit, start: pt, origin: JSON.parse(JSON.stringify(hit.coords)) };
      }
      if (ev.detail === 2) promptRename(hit);
      redraw();
      return;
    }
    state.selectedId = null;
    if (state.tool === "bbox") {
      const a = toDeepSeek(pt[0], pt[1], canvas.width, canvas.height);
      state.draft = { id: uid("bbox"), type: "bbox", name: defaultNameFor("bbox", page), coords: [a[0], a[1], a[0], a[1]] };
      drag = { mode: "bbox", start: pt };
    } else if (state.tool === "point") {
      const el = {
        id: uid("point"),
        type: "point",
        name: defaultNameFor("point", page),
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

  canvas.addEventListener("mouseup", function () {
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
      mutated = true;
    }
    drag = null;
    redraw();
    if (mutated) notifyAnnotationsChange();
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
  bridge.onDetailChange(function () {
    redraw();
    syncNotesFieldset();
  });
  bridge.onPdfReset(function () {
    state.pages = {}; state.selectedId = null; state.draft = null; clearTrailDraft();
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
