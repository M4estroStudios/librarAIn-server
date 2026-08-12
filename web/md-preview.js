(function (global) {
  var markedReady = false;

  function escapeAttr(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;");
  }

  function cleanHref(href) {
    return String(href || "").trim().replace(/^<|>$/g, "");
  }

  function parseBookPageHref(href) {
    var h = cleanHref(href);
    var match = /(?:^|\/)p\.0*(\d+)\.[^\/?#\s]+\.md$/i.exec(h) || /(?:^|\/)p\.0*(\d+)(?:\.md)?(?:[?#]|$)/i.exec(h);
    if (!match) return null;
    var page = parseInt(match[1], 10);
    return page >= 1 ? page : null;
  }

  function ensureMarked() {
    if (markedReady || !global.marked) return !!global.marked;
    global.marked.setOptions({ gfm: true, breaks: true });
    global.marked.use({
      renderer: {
        html: function () {
          return "";
        },
        link: function (token) {
          var href = cleanHref(token.href);
          var text = this.parser.parseInline(token.tokens);
          var title = token.title ? ' title="' + escapeAttr(token.title) + '"' : "";
          var pageFromHref = parseBookPageHref(href);
          if (pageFromHref != null) {
            return '<a href="#" data-biblio-page="' + pageFromHref + '"' + title + ">" + text + "</a>";
          }
          if (/^polyindex:/i.test(href)) {
            return '<span class="md-preview-pending" title="Polyindex non risolto"' + title + ">" + text + "</span>";
          }
          if (/^https?:\/\//i.test(href)) {
            return '<a href="' + escapeAttr(href) + '" target="_blank" rel="noopener noreferrer"' + title + ">" + text + "</a>";
          }
          return text;
        },
      },
    });
    markedReady = true;
    return true;
  }

  function stripLeadingMeta(text) {
    var src = String(text || "").replace(/^\uFEFF/, "");
    var changed = true;
    while (changed) {
      changed = false;
      var fm = /^---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*\r?\n?/.exec(src);
      if (fm) {
        src = src.slice(fm[0].length);
        changed = true;
        continue;
      }
      var comment = /^<!--[\s\S]*?-->[ \t]*\r?\n?/.exec(src);
      if (comment) {
        src = src.slice(comment[0].length);
        changed = true;
      }
    }
    return src.replace(/^\s+/, "");
  }

  function renderMarkdown(text) {
    if (!ensureMarked()) {
      return "<pre class=\"md-preview-fallback\">" +
        String(text || "")
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;") +
        "</pre>";
    }
    return global.marked.parse(stripLeadingMeta(text));
  }

  function setModeButtons(root, mode) {
    if (!root) return;
    var buttons = root.querySelectorAll("[data-md-mode]");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      btn.classList.toggle("is-active", btn.getAttribute("data-md-mode") === mode);
    }
  }

  function bindPane(options) {
    var switchEl = options.switchEl;
    var sourceEl = options.sourceEl;
    var previewEl = options.previewEl;
    var emptyEl = options.emptyEl || null;
    var onOpenPage = typeof options.onOpenPage === "function" ? options.onOpenPage : null;
    var getText = options.getText || function () {
      return sourceEl ? sourceEl.value : "";
    };
    var mode = options.defaultMode === "source" ? "source" : "preview";
    var enabled = false;

    function apply() {
      if (!sourceEl || !previewEl) return;
      var hasEmpty = emptyEl && !emptyEl.classList.contains("hidden");
      if (!enabled || hasEmpty) {
        if (switchEl) switchEl.classList.add("hidden");
        previewEl.classList.add("hidden");
        return;
      }
      if (switchEl) switchEl.classList.remove("hidden");
      setModeButtons(switchEl, mode);
      if (mode === "preview") {
        previewEl.innerHTML = renderMarkdown(getText());
        previewEl.classList.remove("hidden");
        sourceEl.classList.add("hidden");
        var wrap = sourceEl.closest(".transcript-hl-wrap");
        if (wrap) wrap.classList.add("hidden");
      } else {
        previewEl.classList.add("hidden");
        previewEl.innerHTML = "";
        sourceEl.classList.remove("hidden");
        var sourceWrap = sourceEl.closest(".transcript-hl-wrap");
        if (sourceWrap) sourceWrap.classList.remove("hidden");
      }
    }

    function setMode(next) {
      mode = next === "source" ? "source" : "preview";
      apply();
    }

    function setEnabled(on) {
      enabled = !!on;
      if (!enabled && switchEl) switchEl.classList.add("hidden");
      apply();
    }

    function refresh() {
      apply();
    }

    if (switchEl && !switchEl._mdPreviewBound) {
      switchEl._mdPreviewBound = true;
      switchEl.addEventListener("click", function (event) {
        var btn = event.target.closest("[data-md-mode]");
        if (!btn || !switchEl.contains(btn)) return;
        setMode(btn.getAttribute("data-md-mode"));
      });
    }

    if (previewEl && !previewEl._mdPreviewLinksBound) {
      previewEl._mdPreviewLinksBound = true;
      previewEl.addEventListener("click", function (event) {
        var anchor = event.target.closest("a[data-biblio-page]");
        if (!anchor || !previewEl.contains(anchor)) return;
        event.preventDefault();
        var page = parseInt(anchor.getAttribute("data-biblio-page") || "", 10);
        if (!(page >= 1) || !onOpenPage) return;
        onOpenPage(page);
      });
    }

    return {
      setMode: setMode,
      getMode: function () { return mode; },
      setEnabled: setEnabled,
      refresh: refresh,
      isEnabled: function () { return enabled; },
    };
  }

  global.LibrarAInMdPreview = {
    renderMarkdown: renderMarkdown,
    bindPane: bindPane,
    stripLeadingMeta: stripLeadingMeta,
    parseBookPageHref: parseBookPageHref,
  };
})(typeof window !== "undefined" ? window : globalThis);
