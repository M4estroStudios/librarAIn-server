(function (global) {
  var MIN_ZOOM = 1;
  var MAX_ZOOM = 4;
  var ZOOM_STEP = 0.05;
  var SELECTORS = [
    ".page-preview-img-wrap",
    ".page-review-img-wrap",
    ".page-picker-detail-img-wrap",
    ".validation-preview-img-wrap",
  ];
  var STYLE_ID = "librarain-page-zoom-style";
  var controllers = new WeakMap();

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = [
      ".page-zoom-column{flex:1 1 auto;min-width:0;min-height:0;display:flex;flex-direction:column;gap:0.35rem;}",
      ".page-review-pane>.page-zoom-column{flex:0 0 auto;}",
      ".page-zoom-viewport{flex:1 1 auto!important;min-width:0;min-height:0;position:relative!important;display:grid!important;place-items:center;overflow:hidden!important;touch-action:none;}",
      ".page-zoom-viewport.is-zoomed{overflow:auto!important;cursor:default;}",
      ".page-zoom-viewport.is-panning,.page-zoom-viewport.is-panning *{cursor:grabbing!important;user-select:none;}",
      ".page-zoom-stage{position:relative;line-height:0;flex:0 0 auto;}",
      ".page-zoom-viewport .page-zoom-stage>img,.page-zoom-viewport .page-zoom-stage>.validation-preview-frame{display:block!important;width:100%!important;height:auto!important;max-width:none!important;max-height:none!important;position:relative!important;top:auto!important;left:auto!important;transform:none!important;object-fit:contain!important;aspect-ratio:auto;background:#fff;}",
      ".page-zoom-viewport .page-zoom-stage>.validation-preview-frame{height:100%!important;}",
      ".page-zoom-viewport .page-zoom-stage>.validation-preview-frame>.page-preview-img{width:100%!important;height:100%!important;max-width:none!important;max-height:none!important;object-fit:contain!important;}",
      ".page-zoom-controls{flex:0 0 auto;display:flex;align-items:center;gap:0.45rem;padding:0.1rem 0.15rem 0;}",
      ".page-zoom-controls label{flex:0 0 auto;font-size:0.72rem;color:#9d9d9d;}",
      ".page-zoom-controls input[type=range]{flex:1 1 auto;min-width:0;accent-color:#4ec9b0;}",
      ".page-zoom-controls .page-zoom-value{flex:0 0 2.6rem;text-align:right;font-size:0.72rem;color:#c8c8c8;font-variant-numeric:tabular-nums;}",
      ".page-zoom-controls.hidden{display:none!important;}",
    ].join("");
    document.head.appendChild(style);
  }

  function clamp(n, lo, hi) {
    return Math.max(lo, Math.min(hi, n));
  }

  function findMedia(viewport) {
    var frame =
      viewport.querySelector(".page-zoom-stage > .validation-preview-frame") ||
      Array.prototype.find.call(viewport.children, function (c) {
        return c.classList && c.classList.contains("validation-preview-frame");
      });
    if (frame) return { el: frame, img: frame.querySelector("img") };
    var img =
      viewport.querySelector(".page-zoom-stage > img") ||
      Array.prototype.find.call(viewport.children, function (c) {
        return c.tagName === "IMG";
      });
    if (img) return { el: img, img: img };
    return null;
  }

  function setupStructure(viewport) {
    var reviewFrame = viewport.closest(".page-review-frame");
    var host = reviewFrame || viewport;
    var parent = host.parentElement;
    if (!parent) return null;

    var column = parent.classList.contains("page-zoom-column") ? parent : null;
    if (!column) {
      column = document.createElement("div");
      column.className = "page-zoom-column";
      parent.insertBefore(column, host);
      column.appendChild(host);
    }

    viewport.classList.add("page-zoom-viewport");

    var stage = viewport.querySelector(".page-zoom-stage");
    if (!stage) {
      stage = document.createElement("div");
      stage.className = "page-zoom-stage";
      viewport.insertBefore(stage, viewport.firstChild);
    }
    var media = findMedia(viewport);
    if (media && media.el.parentElement !== stage) stage.appendChild(media.el);

    var controls = column.querySelector(":scope > .page-zoom-controls");
    if (!controls) {
      controls = document.createElement("div");
      controls.className = "page-zoom-controls hidden";
      controls.innerHTML =
        "<label>Zoom</label>" +
        '<input type="range" min="' + MIN_ZOOM + '" max="' + MAX_ZOOM + '" step="' + ZOOM_STEP + '" value="' + MIN_ZOOM + '" aria-label="Zoom pagina">' +
        '<span class="page-zoom-value">100%</span>';
      column.appendChild(controls);
      var slider = controls.querySelector("input");
      var label = controls.querySelector("label");
      var sid = viewport.id ? viewport.id + "-zoom" : "page-zoom-" + Math.random().toString(36).slice(2, 8);
      slider.id = sid;
      label.setAttribute("for", sid);
    }
    return { column: column, stage: stage, controls: controls };
  }

  function attach(viewport) {
    if (!viewport || !viewport.isConnected) return null;
    var existing = controllers.get(viewport);
    if (existing) return existing;
    injectStyles();
    var parts = setupStructure(viewport);
    if (!parts) return null;
    var stage = parts.stage;
    var controls = parts.controls;
    var slider = controls.querySelector("input");
    var valueEl = controls.querySelector(".page-zoom-value");

    var state = {
      zoom: MIN_ZOOM,
      panning: false,
      panMoved: false,
      startX: 0,
      startY: 0,
      startLeft: 0,
      startTop: 0,
      lastSrc: "",
      appliedW: 0,
      appliedH: 0,
      fitBaseW: 0,
      fitBaseH: 0,
    };

    function mediaReady() {
      var media = findMedia(viewport);
      if (!media || !media.img) return null;
      if (media.img.classList.contains("hidden")) return null;
      if (!media.img.getAttribute("src")) return null;
      if (!media.img.naturalWidth || !media.img.naturalHeight) return null;
      if (media.el.parentElement !== stage) stage.appendChild(media.el);
      return media;
    }

    function layout() {
      var media = mediaReady();
      if (!media) {
        controls.classList.add("hidden");
        viewport.classList.remove("is-zoomed");
        return;
      }
      controls.classList.remove("hidden");
      var z = state.zoom;
      var atMin = z <= MIN_ZOOM + 0.001;
      var nw = media.img.naturalWidth;
      var nh = media.img.naturalHeight;
      if (atMin || !state.fitBaseW || !state.fitBaseH) {
        var vw = Math.max(1, viewport.clientWidth);
        var vh = Math.max(1, viewport.clientHeight);
        if (vw <= 1 || vh <= 1) return;
        var fit = Math.min(vw / nw, vh / nh);
        if (!isFinite(fit) || fit <= 0) return;
        state.fitBaseW = Math.max(1, Math.floor(nw * fit));
        state.fitBaseH = Math.max(1, Math.floor(nh * fit));
        if (state.fitBaseW > vw) state.fitBaseW = vw;
        if (state.fitBaseH > vh) state.fitBaseH = vh;
        var ratio = nw / nh;
        if (state.fitBaseW / state.fitBaseH > ratio) {
          state.fitBaseW = Math.max(1, Math.floor(state.fitBaseH * ratio));
        } else {
          state.fitBaseH = Math.max(1, Math.floor(state.fitBaseW / ratio));
        }
      }
      var w = Math.max(1, Math.floor(state.fitBaseW * z));
      var h = Math.max(1, Math.floor(state.fitBaseH * z));
      var ratio = nw / nh;
      h = Math.max(1, Math.round(w / ratio));
      if (w === state.appliedW && h === state.appliedH) {
        viewport.classList.toggle("is-zoomed", !atMin);
        if (Number(slider.value) !== z) slider.value = String(z);
        valueEl.textContent = Math.round(z * 100) + "%";
        return;
      }
      state.appliedW = w;
      state.appliedH = h;
      stage.style.width = w + "px";
      stage.style.height = h + "px";
      viewport.classList.toggle("is-zoomed", !atMin);
      if (Number(slider.value) !== z) slider.value = String(z);
      valueEl.textContent = Math.round(z * 100) + "%";
    }

    function setZoom(next) {
      var z = clamp(Number(next) || MIN_ZOOM, MIN_ZOOM, MAX_ZOOM);
      z = clamp(Number((Math.round(z / ZOOM_STEP) * ZOOM_STEP).toFixed(2)), MIN_ZOOM, MAX_ZOOM);
      var prev = state.zoom;
      var rect = viewport.getBoundingClientRect();
      var ax = rect.width / 2;
      var ay = rect.height / 2;
      var contentX = viewport.scrollLeft + ax;
      var contentY = viewport.scrollTop + ay;
      state.zoom = z;
      if (atMinZoom(z)) {
        state.fitBaseW = 0;
        state.fitBaseH = 0;
      }
      state.appliedW = 0;
      state.appliedH = 0;
      layout();
      if (prev > 0 && z !== prev) {
        var ratio = z / prev;
        viewport.scrollLeft = Math.max(0, contentX * ratio - ax);
        viewport.scrollTop = Math.max(0, contentY * ratio - ay);
      }
      global.dispatchEvent(new Event("resize"));
    }

    function atMinZoom(z) {
      return (z == null ? state.zoom : z) <= MIN_ZOOM + 0.001;
    }

    function reset() {
      state.zoom = MIN_ZOOM;
      state.fitBaseW = 0;
      state.fitBaseH = 0;
      state.appliedW = 0;
      state.appliedH = 0;
      viewport.scrollLeft = 0;
      viewport.scrollTop = 0;
      layout();
    }

    function onImgLoad() {
      var media = findMedia(viewport);
      var src = media && media.img ? media.img.getAttribute("src") || "" : "";
      if (src !== state.lastSrc) {
        state.lastSrc = src;
        state.zoom = MIN_ZOOM;
        viewport.scrollLeft = 0;
        viewport.scrollTop = 0;
      }
      state.fitBaseW = 0;
      state.fitBaseH = 0;
      state.appliedW = 0;
      state.appliedH = 0;
      layout();
    }

    var media = findMedia(viewport);
    if (media && media.img && !media.img._pageZoomBound) {
      media.img._pageZoomBound = true;
      media.img.addEventListener("load", onImgLoad);
    }

    slider.addEventListener("input", function () {
      setZoom(slider.value);
    });

    viewport.addEventListener(
      "mousedown",
      function (ev) {
        if (ev.button !== 2) return;
        if (!atMinZoom()) {
          ev.preventDefault();
          state.panning = true;
          state.panMoved = false;
          state.startX = ev.clientX;
          state.startY = ev.clientY;
          state.startLeft = viewport.scrollLeft;
          state.startTop = viewport.scrollTop;
          viewport.classList.add("is-panning");
        }
      },
      true
    );

    function onMove(ev) {
      if (!state.panning) return;
      var dx = ev.clientX - state.startX;
      var dy = ev.clientY - state.startY;
      if (Math.abs(dx) > 2 || Math.abs(dy) > 2) state.panMoved = true;
      viewport.scrollLeft = state.startLeft - dx;
      viewport.scrollTop = state.startTop - dy;
    }

    function onUp(ev) {
      if (!state.panning) return;
      if (ev.button === 2 || ev.buttons === 0) {
        state.panning = false;
        viewport.classList.remove("is-panning");
      }
    }

    global.addEventListener("mousemove", onMove);
    global.addEventListener("mouseup", onUp);

    viewport.addEventListener(
      "contextmenu",
      function (ev) {
        if (!atMinZoom() || state.panMoved) {
          ev.preventDefault();
          state.panMoved = false;
        }
      },
      true
    );

    var api = {
      reset: reset,
      setZoom: setZoom,
      getZoom: function () { return state.zoom; },
      layout: layout,
      onImgLoad: onImgLoad,
      destroy: function () {
        global.removeEventListener("mousemove", onMove);
        global.removeEventListener("mouseup", onUp);
        controls.remove();
        viewport.classList.remove("page-zoom-viewport", "is-zoomed", "is-panning");
        controllers.delete(viewport);
      },
    };
    controllers.set(viewport, api);
    layout();
    return api;
  }

  function scan(root) {
    injectStyles();
    var scope = root && root.querySelectorAll ? root : document;
    for (var i = 0; i < SELECTORS.length; i++) {
      var nodes = scope.querySelectorAll(SELECTORS[i]);
      for (var j = 0; j < nodes.length; j++) attach(nodes[j]);
    }
  }

  function resetAll(root) {
    var scope = root && root.querySelectorAll ? root : document;
    for (var i = 0; i < SELECTORS.length; i++) {
      var nodes = scope.querySelectorAll(SELECTORS[i]);
      for (var j = 0; j < nodes.length; j++) {
        var api = controllers.get(nodes[j]);
        if (api) api.reset();
      }
    }
  }

  function onDocumentImgLoad(ev) {
    var img = ev.target;
    if (!img || img.tagName !== "IMG") return;
    var viewport = img.closest(SELECTORS.join(","));
    if (!viewport) return;
    var api = controllers.get(viewport) || attach(viewport);
    if (api && api.onImgLoad) api.onImgLoad();
  }

  var winResizeTimer = 0;
  function onWindowResize() {
    if (winResizeTimer) return;
    winResizeTimer = global.setTimeout(function () {
      winResizeTimer = 0;
      for (var i = 0; i < SELECTORS.length; i++) {
        var nodes = document.querySelectorAll(SELECTORS[i]);
        for (var j = 0; j < nodes.length; j++) {
          var api = controllers.get(nodes[j]);
          if (!api) continue;
          if (api.getZoom() <= MIN_ZOOM + 0.001) api.reset();
          else api.layout();
        }
      }
    }, 150);
  }

  function boot() {
    injectStyles();
    scan(document);
    document.addEventListener("load", onDocumentImgLoad, true);
    global.addEventListener("resize", onWindowResize);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  global.LibrarAInPageZoom = {
    attach: attach,
    scan: scan,
    resetAll: resetAll,
    MIN: MIN_ZOOM,
    MAX: MAX_ZOOM,
  };
})(window);
