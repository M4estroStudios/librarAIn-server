(function (global) {
  var STYLE_ID = "librarain-transcript-highlight-style";
  var controllers = new WeakMap();
  var valueDesc = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value");

  function injectStyles() {
    if (document.getElementById(STYLE_ID)) return;
    var style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = [
      ".transcript-hl-wrap{position:relative;flex:1 1 auto;width:100%;height:100%;min-height:0;overflow:hidden;}",
      ".transcript-hl-backdrop,.transcript-hl-wrap>textarea.transcript-hl-input{",
      "position:absolute;inset:0;box-sizing:border-box;margin:0;width:100%;height:100%;min-height:0;",
      "padding:0.65rem;border:0;outline:none;resize:none;",
      "font-family:ui-monospace,\"Cascadia Mono\",\"Segoe UI Mono\",monospace;font-size:0.72rem;line-height:1.35;",
      "white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;",
      "}",
      ".transcript-hl-backdrop{overflow:hidden;pointer-events:none;z-index:0;color:var(--th-base,#111);background:transparent;}",
      ".transcript-hl-wrap>textarea.transcript-hl-input{",
      "z-index:1;overflow:auto;background:transparent!important;color:transparent!important;",
      "caret-color:var(--th-caret,#111);-webkit-text-fill-color:transparent;",
      "}",
      ".transcript-hl-wrap>textarea.transcript-hl-input::selection,",
      ".transcript-hl-wrap>textarea.transcript-hl-input::-moz-selection{",
      "color:transparent!important;-webkit-text-fill-color:transparent!important;",
      "background:rgba(80,140,255,0.38);",
      "}",
      ".transcript-hl-wrap{-webkit-font-smoothing:antialiased;}",
      ".transcript-hl-wrap{--th-base:#111;--th-caret:#111;--th-h1:#0b7a6a;--th-h2:#1a5fb4;--th-h3:#0e7490;--th-quote:#b86e00;--th-em:#7a3e9d;--th-link:#c62828;}",
      ".transcript-hl-wrap.is-dark{--th-base:#d4d4d4;--th-caret:#e8e8e8;--th-h1:#4ec9b0;--th-h2:#6cb6ff;--th-h3:#22d3ee;--th-quote:#dcdcaa;--th-em:#c586c0;--th-link:#f07178;}",
      ".transcript-hl-backdrop .th-h1{color:var(--th-h1);font-weight:700;}",
      ".transcript-hl-backdrop .th-h2{color:var(--th-h2);font-weight:650;}",
      ".transcript-hl-backdrop .th-h3{color:var(--th-h3);font-weight:600;}",
      ".transcript-hl-backdrop .th-quote{color:var(--th-quote);}",
      ".transcript-hl-backdrop .th-em{color:var(--th-em);font-style:italic;}",
      ".transcript-hl-backdrop .th-link{color:var(--th-link);text-decoration:underline;text-decoration-color:color-mix(in srgb,var(--th-link) 55%,transparent);}",
      ".biblio-book-text-frame .transcript-hl-backdrop,.biblio-book-text-frame .transcript-hl-wrap>textarea.transcript-hl-input{padding:0.75rem;}",
      ".transcript-hl-wrap:has(>textarea.hidden){display:none!important;}",
    ].join("");
    document.head.appendChild(style);
  }

  function escapeHtml(text) {
    return String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function highlightInline(escaped) {
    return escaped
      .replace(/(^|[^*_])\*([^*\n]+)\*(?!\*)/g, function (_, pre, body) {
        return pre + '<span class="th-em">*' + body + "*</span>";
      })
      .replace(/(^|[^_*])_([^_\n]+)_(?!_)/g, function (_, pre, body) {
        return pre + '<span class="th-em">_' + body + "_</span>";
      })
      .replace(/\[([^\]]*)\]\(([^)\n]*)\)/g, function (match, label, href) {
        return '<span class="th-link">[' + label + "](" + href + ")</span>";
      });
  }

  function highlightMarkdown(text) {
    var raw = String(text || "");
    var lines = raw.split("\n");
    var out = [];
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var heading = /^(#{1,3})(\s+)(.*)$/.exec(line);
      if (heading) {
        var level = heading[1].length;
        var cls = level === 1 ? "th-h1" : level === 2 ? "th-h2" : "th-h3";
        out.push(
          '<span class="' + cls + '">' +
            escapeHtml(heading[1] + heading[2]) +
            highlightInline(escapeHtml(heading[3])) +
            "</span>"
        );
        continue;
      }
      if (/^>(\s|$)/.test(line)) {
        out.push('<span class="th-quote">' + highlightInline(escapeHtml(line)) + "</span>");
        continue;
      }
      out.push(highlightInline(escapeHtml(line)));
    }
    return out.join("\n") + "\n";
  }

  function luminance(rgb) {
    var m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(rgb || "");
    if (!m) return 1;
    return (0.2126 * Number(m[1]) + 0.7152 * Number(m[2]) + 0.0722 * Number(m[3])) / 255;
  }

  function detectDark(textarea) {
    if (textarea.classList.contains("biblio-book-text")) return true;
    var host = textarea.closest(".page-review-frame, .biblio-book-text-frame") || textarea.parentElement;
    if (!host) return false;
    return luminance(getComputedStyle(host).backgroundColor) < 0.45;
  }

  function refreshController(ctrl) {
    ctrl.backdrop.innerHTML = highlightMarkdown(ctrl.textarea.value);
    ctrl.backdrop.scrollTop = ctrl.textarea.scrollTop;
    ctrl.backdrop.scrollLeft = ctrl.textarea.scrollLeft;
  }

  function attach(textarea) {
    if (!textarea || !(textarea instanceof HTMLTextAreaElement)) return null;
    var existing = controllers.get(textarea);
    if (existing) {
      refreshController(existing);
      return existing;
    }
    injectStyles();
    var wrap = document.createElement("div");
    wrap.className = "transcript-hl-wrap";
    if (detectDark(textarea)) wrap.classList.add("is-dark");
    var backdrop = document.createElement("pre");
    backdrop.className = "transcript-hl-backdrop";
    backdrop.setAttribute("aria-hidden", "true");
    var parent = textarea.parentNode;
    parent.insertBefore(wrap, textarea);
    wrap.appendChild(backdrop);
    wrap.appendChild(textarea);
    textarea.classList.add("transcript-hl-input");

    var ctrl = { textarea: textarea, wrap: wrap, backdrop: backdrop };
    controllers.set(textarea, ctrl);

    if (valueDesc && valueDesc.get && valueDesc.set) {
      Object.defineProperty(textarea, "value", {
        configurable: true,
        enumerable: true,
        get: function () {
          return valueDesc.get.call(this);
        },
        set: function (next) {
          valueDesc.set.call(this, next);
          refreshController(ctrl);
        },
      });
    }

    textarea.addEventListener("input", function () {
      refreshController(ctrl);
    });
    textarea.addEventListener("scroll", function () {
      backdrop.scrollTop = textarea.scrollTop;
      backdrop.scrollLeft = textarea.scrollLeft;
    });

    refreshController(ctrl);
    return ctrl;
  }

  function refresh(textarea) {
    var ctrl = controllers.get(textarea);
    if (!ctrl) return;
    refreshController(ctrl);
  }

  function scan(root) {
    injectStyles();
    var scope = root && root.querySelectorAll ? root : document;
    var nodes = scope.querySelectorAll(
      "textarea.page-review-text, textarea.biblio-book-text, textarea[id^='polyindex-transcript-']"
    );
    for (var i = 0; i < nodes.length; i++) attach(nodes[i]);
  }

  global.LibrarAInTranscriptHighlight = {
    attach: attach,
    refresh: refresh,
    scan: scan,
    highlightMarkdown: highlightMarkdown,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      scan(document);
    });
  } else {
    scan(document);
  }
})(typeof window !== "undefined" ? window : globalThis);
