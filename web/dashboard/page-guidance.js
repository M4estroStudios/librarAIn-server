const BOX = "#dc3c3c";
const POINT = "#2878dc";
const TRAIL = "#28aa6e";
const TRAIL_START = "#1ec8ff";
const TRAIL_END = "#ff7a1a";
const TOOLS = { bbox: 1, point: 1, trail: 1 };

function uid(prefix) {
  return prefix + "_" + Math.random().toString(36).slice(2, 9);
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
    annotationListeners.forEach(function (cb) { try { cb(); } catch (_) {} });
  }

  const btn = document.getElementById("annotate-tool-btn");
  const switchEl = document.getElementById("annotate-primitive-switch");
  const canvas = document.getElementById("annotate-canvas");
  const guidanceField = document.querySelector('[name="ai_page_guidance"]');
  const notesFieldset = document.getElementById("model-notes-fieldset");
  if (!btn || !canvas || !switchEl) return null;
  const ctx = canvas.getContext("2d");
  let drag = null;

  function pageMap(page) {
    if (!state.pages[page]) state.pages[page] = [];
    return state.pages[page];
  }

  function clearTrailDraft() { state.trailPoints = null; state.trailCursor = null; }

  function setActive(on) {
    const next = !!on;
    if (next && typeof bridge.onAnnotateActivate === "function") bridge.onAnnotateActivate();
    state.active = next;
    btn.classList.toggle("is-active", state.active);
    switchEl.classList.toggle("hidden", !state.active);
    canvas.classList.toggle("hidden", !state.active);
    if (notesFieldset) notesFieldset.classList.toggle("hidden", !state.active);
    if (state.active) redraw();
    else {
      state.selectedId = null;
      state.draft = null;
      clearTrailDraft();
      hideNameInput();
    }
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
    const label = String(text || "").trim() || "?";
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
    if (!state.active || !syncCanvasSize()) {
      hideNameInput();
      return;
    }
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const page = bridge.getDetailPage();
    if (!page) {
      hideNameInput();
      return;
    }
    const list = pageMap(page);
    list.forEach(function (el) {
      drawElement(el, canvas.width, canvas.height, el.id === state.selectedId);
    });
    if (state.draft && state.draft.type === "bbox") {
      drawElement(state.draft, canvas.width, canvas.height, true);
    }
    if (state.trailPoints && state.trailPoints.length) {
      const preview = state.trailPoints.slice();
      if (state.trailCursor) preview.push(state.trailCursor);
      drawElement({ type: "trail", name: "…", coords: preview }, canvas.width, canvas.height, true);
    }
    if (syncInput === false) return;
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

  function deleteSelected() {
    const page = bridge.getDetailPage();
    if (!page || !state.selectedId) return false;
    state.pages[page] = pageMap(page).filter(function (el) { return el.id !== state.selectedId; });
    state.selectedId = null; hideNameInput(); redraw(); notifyAnnotationsChange();
    return true;
  }

  function resetPage() {
    const page = bridge.getDetailPage();
    if (!page) return;
    state.pages[page] = []; state.selectedId = null; state.draft = null; clearTrailDraft();
    hideNameInput(); redraw(); notifyAnnotationsChange();
  }

  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.className = "annotate-name-input hidden";
  nameInput.maxLength = 64;
  nameInput.setAttribute("aria-label", "Nome primitiva");
  const wrap = bridge.getDetailWrapEl();
  if (wrap) wrap.appendChild(nameInput);

  function hideNameInput() {
    nameInput.classList.add("hidden");
    nameInput.blur();
  }

  function showNameInputFor(el) {
    if (!el || !canvas.width || canvas.width < 2) { hideNameInput(); return; }
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
    nameInput.value = el.name || "";
    nameInput.classList.remove("hidden");
    nameInput.style.left = Math.round(canvas.offsetLeft + x) + "px";
    nameInput.style.top = Math.max(0, Math.round(canvas.offsetTop + y - 22)) + "px";
  }

  nameInput.addEventListener("input", function () {
    const page = bridge.getDetailPage();
    if (!page || !state.selectedId) return;
    const el = pageMap(page).find(function (item) {
      return item.id === state.selectedId;
    });
    if (!el) return;
    el.name = String(nameInput.value || "").trim() || el.type;
    redraw(false);
    notifyAnnotationsChange();
  });
  nameInput.addEventListener("mousedown", function (ev) {
    ev.stopPropagation();
  });
  nameInput.addEventListener("keydown", function (ev) {
    if (ev.key === "Delete") {
      ev.preventDefault();
      ev.stopPropagation();
      deleteSelected();
      return;
    }
    if (ev.key !== "Enter" && ev.key !== "Escape" && ev.code !== "NumpadEnter") return;
    ev.preventDefault();
    ev.stopPropagation();
    hideNameInput();
  });

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
    const el = { id: uid("trail"), type: "trail", name: "trail", coords: state.trailPoints.slice() };
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
      state.draft = { id: uid("bbox"), type: "bbox", name: "bbox", coords: [a[0], a[1], a[0], a[1]] };
      drag = { mode: "bbox", start: pt };
    } else if (state.tool === "point") {
      const el = {
        id: uid("point"),
        type: "point",
        name: "point",
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
    if (tag === "INPUT" || tag === "TEXTAREA") return;
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
  bridge.onDetailChange(function () { if (state.active) redraw(); });
  bridge.onPdfReset(function () {
    state.pages = {}; state.selectedId = null; state.draft = null; clearTrailDraft();
    if (guidanceField) guidanceField.value = "";
    setActive(false); notifyAnnotationsChange();
  });
  function annotationsPayload() {
    return Object.keys(state.pages).map(Number).filter(function (page) { return pageMap(page).length > 0; }).sort(function (a, b) { return a - b; }).map(function (page) { return { page: page, elements: pageMap(page) }; });
  }
  window.addEventListener("resize", function () { if (state.active) redraw(); });
  return {
    getAnnotations: annotationsPayload,
    setActive: setActive,
    isActive: function () { return !!state.active; },
    onAnnotationsChange: function (cb) { if (typeof cb === "function") annotationListeners.push(cb); },
  };
}
