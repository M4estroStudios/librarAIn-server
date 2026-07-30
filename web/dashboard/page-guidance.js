const BOX = "#dc3c3c";
const POINT = "#2878dc";
const TRAIL = "#28aa6e";
const TRAIL_START = "#1ec8ff";
const TRAIL_END = "#ff7a1a";

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
  };
  const annotationListeners = [];
  function notifyAnnotationsChange() {
    for (let i = 0; i < annotationListeners.length; i++) {
      try {
        annotationListeners[i]();
      } catch (_) {}
    }
  }

  const btn = document.getElementById("annotate-tool-btn");
  const switchEl = document.getElementById("annotate-primitive-switch");
  const canvas = document.getElementById("annotate-canvas");
  const guidanceField = document.querySelector('[name="ai_page_guidance"]');
  if (!btn || !canvas || !switchEl) return null;
  const ctx = canvas.getContext("2d");

  function pageMap(page) {
    if (!state.pages[page]) state.pages[page] = [];
    return state.pages[page];
  }

  function setActive(on) {
    const next = !!on;
    if (next && typeof bridge.onAnnotateActivate === "function") {
      bridge.onAnnotateActivate();
    }
    state.active = next;
    btn.classList.toggle("is-active", state.active);
    switchEl.classList.toggle("hidden", !state.active);
    canvas.classList.toggle("hidden", !state.active);
    if (state.active) redraw();
    else {
      state.selectedId = null;
      state.draft = null;
      state.trailPoints = null;
      hideNameInput();
    }
  }

  function setTool(tool) {
    state.tool = tool === "point_trail" ? "point_trail" : "bbox";
    switchEl.querySelectorAll("[data-annotate-tool]").forEach(function (el) {
      el.classList.toggle("is-active", el.getAttribute("data-annotate-tool") === state.tool);
    });
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
    const pad = 3;
    const boxH = 14;
    const top = Math.max(0, y - boxH - 2);
    ctx.fillStyle = "rgba(20,20,20,0.9)";
    ctx.fillRect(x, top, tw + pad * 2, boxH);
    ctx.fillStyle = "#fff";
    ctx.fillText(label, x + pad, top + 11);
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
      const w = Math.abs(p2[0] - p1[0]);
      const h = Math.abs(p2[1] - p1[1]);
      ctx.strokeRect(x, y, w, h);
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
      if (pts.length) {
        ctx.beginPath();
        ctx.moveTo(pts[0][0], pts[0][1]);
        for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
        ctx.stroke();
        const start = pts[0];
        const end = pts[pts.length - 1];
        ctx.fillStyle = TRAIL_START;
        ctx.beginPath();
        ctx.arc(start[0], start[1], selected ? 7 : 6, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "#0b3a4a";
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.fillStyle = TRAIL_END;
        ctx.beginPath();
        ctx.arc(end[0], end[1], selected ? 7 : 6, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "#5a2a00";
        ctx.lineWidth = 1.5;
        ctx.stroke();
        drawLabel(start[0], start[1], el.name);
      }
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
      drawElement(
        { type: "trail", name: "…", coords: state.trailPoints },
        canvas.width,
        canvas.height,
        true
      );
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

  function hitTest(page, x, y) {
    const list = pageMap(page);
    for (let i = list.length - 1; i >= 0; i--) {
      const el = list[i];
      if (el.type === "bbox") {
        const p1 = fromDeepSeek(el.coords[0], el.coords[1], canvas.width, canvas.height);
        const p2 = fromDeepSeek(el.coords[2], el.coords[3], canvas.width, canvas.height);
        const left = Math.min(p1[0], p2[0]);
        const right = Math.max(p1[0], p2[0]);
        const top = Math.min(p1[1], p2[1]);
        const bottom = Math.max(p1[1], p2[1]);
        if (x >= left && x <= right && y >= top && y <= bottom) return el;
      } else if (el.type === "point") {
        const p = fromDeepSeek(el.coords[0], el.coords[1], canvas.width, canvas.height);
        if (Math.hypot(p[0] - x, p[1] - y) <= 8) return el;
      } else if (el.type === "trail") {
        const pts = (el.coords || []).map(function (pair) {
          return fromDeepSeek(pair[0], pair[1], canvas.width, canvas.height);
        });
        for (let j = 0; j < pts.length; j++) {
          if (Math.hypot(pts[j][0] - x, pts[j][1] - y) <= 8) return el;
        }
      }
    }
    return null;
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
    if (!el || !canvas.width || canvas.width < 2) {
      hideNameInput();
      return;
    }
    let x = 0;
    let y = 0;
    if (el.type === "bbox") {
      const p1 = fromDeepSeek(el.coords[0], el.coords[1], canvas.width, canvas.height);
      const p2 = fromDeepSeek(el.coords[2], el.coords[3], canvas.width, canvas.height);
      x = Math.min(p1[0], p2[0]);
      y = Math.min(p1[1], p2[1]);
    } else if (el.type === "point") {
      const p = fromDeepSeek(el.coords[0], el.coords[1], canvas.width, canvas.height);
      x = p[0];
      y = p[1];
    } else if (el.type === "trail" && el.coords && el.coords[0]) {
      const p = fromDeepSeek(el.coords[0][0], el.coords[0][1], canvas.width, canvas.height);
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

  function promptRename(el) {
    state.selectedId = el.id;
    showNameInputFor(el);
    nameInput.focus();
    nameInput.select();
  }

  let drag = null;

  canvas.addEventListener("mousedown", function (ev) {
    if (!state.active || ev.button !== 0) return;
    const page = bridge.getDetailPage();
    if (!page) return;
    const pt = localPoint(ev);
    const hit = hitTest(page, pt[0], pt[1]);
    if (hit) {
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
      state.draft = {
        id: uid("bbox"),
        type: "bbox",
        name: "bbox",
        coords: [a[0], a[1], a[0], a[1]],
      };
      drag = { mode: "bbox", start: pt };
    } else {
      state.trailPoints = [toDeepSeek(pt[0], pt[1], canvas.width, canvas.height)];
      drag = { mode: "trail", moved: false, start: pt };
    }
    redraw();
  });

  canvas.addEventListener("mousemove", function (ev) {
    if (!drag) return;
    const pt = localPoint(ev);
    if (drag.mode === "bbox" && state.draft) {
      const a = toDeepSeek(drag.start[0], drag.start[1], canvas.width, canvas.height);
      const b = toDeepSeek(pt[0], pt[1], canvas.width, canvas.height);
      state.draft.coords = [a[0], a[1], b[0], b[1]];
    } else if (drag.mode === "trail") {
      if (Math.hypot(pt[0] - drag.start[0], pt[1] - drag.start[1]) > 3) drag.moved = true;
      const next = toDeepSeek(pt[0], pt[1], canvas.width, canvas.height);
      const last = state.trailPoints[state.trailPoints.length - 1];
      if (!last || last[0] !== next[0] || last[1] !== next[1]) state.trailPoints.push(next);
    } else if (drag.mode === "move" && drag.el) {
      const dx = pt[0] - drag.start[0];
      const dy = pt[1] - drag.start[1];
      const dnx = Math.round((dx / Math.max(canvas.width - 1, 1)) * 999);
      const dny = Math.round((dy / Math.max(canvas.height - 1, 1)) * 999);
      if (drag.el.type === "bbox") {
        drag.el.coords = [
          clamp(drag.origin[0] + dnx, 0, 999),
          clamp(drag.origin[1] + dny, 0, 999),
          clamp(drag.origin[2] + dnx, 0, 999),
          clamp(drag.origin[3] + dny, 0, 999),
        ];
      } else if (drag.el.type === "point") {
        drag.el.coords = [clamp(drag.origin[0] + dnx, 0, 999), clamp(drag.origin[1] + dny, 0, 999)];
      } else if (drag.el.type === "trail") {
        drag.el.coords = drag.origin.map(function (pair) {
          return [clamp(pair[0] + dnx, 0, 999), clamp(pair[1] + dny, 0, 999)];
        });
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
        state.draft.name = "bbox";
        pageMap(page).push(state.draft);
        state.selectedId = state.draft.id;
        promptRename(state.draft);
        mutated = true;
      }
      state.draft = null;
    } else if (drag.mode === "trail" && page && state.trailPoints) {
      if (!drag.moved) {
        const el = {
          id: uid("point"),
          type: "point",
          name: "point",
          coords: state.trailPoints[0],
        };
        pageMap(page).push(el);
        state.selectedId = el.id;
        promptRename(el);
        mutated = true;
      } else if (state.trailPoints.length >= 2) {
        const el = {
          id: uid("trail"),
          type: "trail",
          name: "trail",
          coords: state.trailPoints.slice(),
        };
        pageMap(page).push(el);
        state.selectedId = el.id;
        promptRename(el);
        mutated = true;
      }
      state.trailPoints = null;
    }
    drag = null;
    redraw();
    if (mutated) notifyAnnotationsChange();
  });

  document.addEventListener("keydown", function (ev) {
    if (!state.active || !state.selectedId) return;
    if (ev.key !== "Delete" && ev.key !== "Backspace") return;
    const tag = (ev.target && ev.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    const page = bridge.getDetailPage();
    if (!page) return;
    state.pages[page] = pageMap(page).filter(function (el) {
      return el.id !== state.selectedId;
    });
    state.selectedId = null;
    redraw();
    notifyAnnotationsChange();
  });

  btn.addEventListener("click", function () {
    setActive(!state.active);
  });
  switchEl.querySelectorAll("[data-annotate-tool]").forEach(function (el) {
    el.addEventListener("click", function () {
      setTool(el.getAttribute("data-annotate-tool"));
    });
  });
  setTool("bbox");

  bridge.onDetailChange(function () {
    if (state.active) redraw();
  });
  bridge.onPdfReset(function () {
    state.pages = {};
    state.selectedId = null;
    state.draft = null;
    state.trailPoints = null;
    if (guidanceField) guidanceField.value = "";
    setActive(false);
    notifyAnnotationsChange();
  });

  function annotationsPayload() {
    return Object.keys(state.pages)
      .map(Number)
      .filter(function (page) {
        return pageMap(page).length > 0;
      })
      .sort(function (a, b) {
        return a - b;
      })
      .map(function (page) {
        return { page: page, elements: pageMap(page) };
      });
  }

  window.addEventListener("resize", function () {
    if (state.active) redraw();
  });

  return {
    getAnnotations: annotationsPayload,
    setActive: setActive,
    isActive: function () {
      return !!state.active;
    },
    onAnnotationsChange: function (cb) {
      if (typeof cb === "function") annotationListeners.push(cb);
    },
  };
}
