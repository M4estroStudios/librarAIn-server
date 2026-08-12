const SECTION_FIELDS = ["notes", "index_notes", "page_notes"];

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function mentionToken(name) {
  const cleaned = String(name || "")
    .trim()
    .replace(/\s+/g, "_")
    .replace(/[^\w.@\-àáèéìíòóùúÀÁÈÉÌÍÒÓÙÚ]/g, "");
  return cleaned || "elemento";
}

function ensureChipRows() {
  SECTION_FIELDS.forEach(function (field) {
    const textarea = document.querySelector('textarea[name="' + field + '"]');
    if (!textarea) return;
    const label = textarea.closest("label");
    if (!label) return;
    let row = label.querySelector('[data-mention-chips="' + field + '"]');
    if (row) return;
    row = document.createElement("div");
    row.className = "mention-chips";
    row.setAttribute("data-mention-chips", field);
    row.setAttribute("aria-label", "Annotazioni @ per " + field);
    const anchor = textarea.closest(".mention-editor") || textarea;
    label.insertBefore(row, anchor);
  });
}

function ensureHighlightEditor(textarea) {
  if (!textarea || textarea.closest(".mention-editor")) return;
  const wrap = document.createElement("div");
  wrap.className = "mention-editor";
  textarea.parentNode.insertBefore(wrap, textarea);
  const backdrop = document.createElement("div");
  backdrop.className = "mention-backdrop";
  backdrop.setAttribute("aria-hidden", "true");
  wrap.appendChild(backdrop);
  wrap.appendChild(textarea);
}

function renderMentionHighlight(textarea) {
  if (!textarea) return;
  ensureHighlightEditor(textarea);
  const backdrop = textarea.parentElement && textarea.parentElement.querySelector(".mention-backdrop");
  if (!backdrop) return;
  const value = String(textarea.value || "");
  const html = escapeHtml(value).replace(/@([\w.\-àáèéìíòóùúÀÁÈÉÌÍÒÓÙÚ]+)/g, '<span class="mention-in-text">@$1</span>');
  backdrop.innerHTML = (html || "&nbsp;") + (value.endsWith("\n") ? "<br>" : "");
  backdrop.scrollTop = textarea.scrollTop;
  backdrop.scrollLeft = textarea.scrollLeft;
}

function syncMentionScroll(textarea) {
  const backdrop = textarea && textarea.parentElement && textarea.parentElement.querySelector(".mention-backdrop");
  if (!backdrop) return;
  backdrop.scrollTop = textarea.scrollTop;
  backdrop.scrollLeft = textarea.scrollLeft;
}

function createMentionMenu() {
  let menu = document.getElementById("mention-selector");
  if (menu) return menu;
  menu = document.createElement("div");
  menu.id = "mention-selector";
  menu.className = "mention-selector hidden";
  menu.setAttribute("role", "listbox");
  document.body.appendChild(menu);
  return menu;
}

function getTextareaCaretRect(textarea, index) {
  if (!textarea) return null;
  const pos = Math.max(0, Math.min(Number(index) || 0, (textarea.value || "").length));
  const style = window.getComputedStyle(textarea);
  const mirror = document.createElement("div");
  const props = [
    "boxSizing", "width", "height", "overflowX", "overflowY",
    "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
    "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
    "fontStyle", "fontVariant", "fontWeight", "fontStretch", "fontSize", "fontSizeAdjust",
    "lineHeight", "fontFamily", "textAlign", "textTransform", "textIndent",
    "textDecoration", "letterSpacing", "wordSpacing", "tabSize", "whiteSpace", "wordBreak",
    "overflowWrap", "wordWrap",
  ];
  mirror.setAttribute("aria-hidden", "true");
  mirror.style.position = "absolute";
  mirror.style.visibility = "hidden";
  mirror.style.top = "0";
  mirror.style.left = "-9999px";
  mirror.style.pointerEvents = "none";
  mirror.style.whiteSpace = "pre-wrap";
  mirror.style.wordWrap = "break-word";
  props.forEach(function (prop) {
    mirror.style[prop] = style[prop];
  });
  mirror.style.width = textarea.clientWidth + "px";
  mirror.style.height = "auto";
  mirror.style.overflow = "hidden";
  const value = textarea.value || "";
  mirror.textContent = value.slice(0, pos);
  const marker = document.createElement("span");
  marker.textContent = value.slice(pos) || ".";
  mirror.appendChild(marker);
  document.body.appendChild(mirror);
  const taRect = textarea.getBoundingClientRect();
  const markerRect = marker.getBoundingClientRect();
  const mirrorRect = mirror.getBoundingClientRect();
  const lineHeight = parseFloat(style.lineHeight) || (parseFloat(style.fontSize) * 1.35) || 16;
  const top = taRect.top + (markerRect.top - mirrorRect.top) - textarea.scrollTop;
  const left = taRect.left + (markerRect.left - mirrorRect.left) - textarea.scrollLeft;
  document.body.removeChild(mirror);
  return {
    top: top,
    left: left,
    bottom: top + lineHeight,
    height: lineHeight,
  };
}

export function bootPageGuidanceMentions(bridge, getAnnotations, removeAnnotation) {
  ensureChipRows();
  const menu = createMentionMenu();
  let activeField = null;
  let activeQuery = "";
  let activeStart = -1;
  let highlight = 0;
  let currentItems = [];

  function sectionOfPage(page) {
    if (typeof bridge.getSectionForPage === "function") {
      return bridge.getSectionForPage(page) || "page_notes";
    }
    return "page_notes";
  }

  function currentDetailPage() {
    if (typeof bridge.getDetailPage !== "function") return null;
    const page = Number(bridge.getDetailPage());
    return page >= 1 ? page : null;
  }

  function itemsForSection(section) {
    const payload = typeof getAnnotations === "function" ? getAnnotations() : [];
    const detailPage = currentDetailPage();
    const out = [];
    payload.forEach(function (pageItem) {
      const sectionName = sectionOfPage(pageItem.page);
      if (sectionName !== section) return;
      (pageItem.elements || []).forEach(function (el) {
        const lineStart = Number(el.lineStart);
        const lineEnd = Number(el.lineEnd);
        const charStart = Number(el.start);
        const charEnd = Number(el.end);
        let meta = "p." + pageItem.page;
        if (el.type === "text" && lineStart >= 1) {
          meta = "p." + pageItem.page + " · ";
          meta += lineStart === lineEnd || !(lineEnd >= 1)
            ? "r." + lineStart
            : "r." + lineStart + "-" + lineEnd;
          if (Number.isFinite(charStart) && Number.isFinite(charEnd) && charEnd >= charStart) {
            meta += " · c." + charStart + "-" + charEnd;
          }
        }
        out.push({
          page: pageItem.page,
          id: el.id,
          name: el.name || el.type,
          type: el.type,
          token: mentionToken(el.name || el.type),
          meta: meta,
          current: detailPage != null && Number(pageItem.page) === detailPage,
        });
      });
    });
    out.sort(function (a, b) {
      if (a.current !== b.current) return a.current ? -1 : 1;
      if (a.page !== b.page) return a.page - b.page;
      return String(a.token || "").localeCompare(String(b.token || ""), "it");
    });
    return out;
  }

  function renderChips() {
    ensureChipRows();
    SECTION_FIELDS.forEach(function (field) {
      const row = document.querySelector('[data-mention-chips="' + field + '"]');
      if (!row) return;
      const items = itemsForSection(field);
      row.innerHTML = "";
      if (!items.length) {
        row.classList.add("is-empty");
        return;
      }
      row.classList.remove("is-empty");
      items.forEach(function (item) {
        const chip = document.createElement("div");
        chip.className = "mention-chip" + (item.current ? " is-current" : "");
        chip.setAttribute("data-field", field);
        chip.setAttribute("data-token", item.token || "");
        chip.setAttribute("data-page", String(item.page));
        chip.setAttribute("data-id", String(item.id || ""));
        chip.title = item.meta + " · " + (item.type || "");

        const label = document.createElement("button");
        label.type = "button";
        label.className = "mention-chip-label";
        label.innerHTML = "@" + escapeHtml(item.token) + "<small>" + escapeHtml(item.meta) + "</small>";
        label.addEventListener("click", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          const textarea = document.querySelector('textarea[name="' + field + '"]');
          if (!textarea || !item.token) return;
          const start = textarea.selectionStart != null ? textarea.selectionStart : textarea.value.length;
          insertToken(textarea, start, start, item.token);
        });

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "mention-chip-remove";
        remove.title = "Elimina";
        remove.setAttribute("aria-label", "Elimina");
        remove.textContent = "×";
        remove.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
        });
        remove.addEventListener("click", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          if (typeof removeAnnotation === "function" && item.page >= 1 && item.id) {
            removeAnnotation(item.page, item.id, item.token);
          }
          renderChips();
        });

        chip.appendChild(label);
        chip.appendChild(remove);
        row.appendChild(chip);
      });
    });
  }

  function hideMenu() {
    menu.classList.add("hidden");
    menu.innerHTML = "";
    activeField = null;
    activeQuery = "";
    activeStart = -1;
    highlight = 0;
    currentItems = [];
  }

  function insertToken(textarea, start, end, token) {
    const value = textarea.value;
    const before = value.slice(0, start);
    const after = value.slice(end);
    const insertion = "@" + token + (after.startsWith(" ") || after.startsWith("\n") ? "" : " ");
    textarea.value = before + insertion + after;
    const caret = before.length + insertion.length;
    textarea.focus();
    textarea.setSelectionRange(caret, caret);
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function showMenu(textarea, items, atIndex) {
    currentItems = items;
    if (!items.length) {
      hideMenu();
      return;
    }
    highlight = Math.max(0, Math.min(highlight, items.length - 1));
    menu.innerHTML = items
      .map(function (item, idx) {
        return (
          '<button type="button" class="mention-option' +
          (idx === highlight ? " is-active" : "") +
          '" role="option" data-idx="' +
          idx +
          '">@' +
          escapeHtml(item.token) +
          "<span>" +
          escapeHtml(item.meta || ("p." + item.page)) +
          " · " +
          escapeHtml(item.type) +
          "</span></button>"
        );
      })
      .join("");
    menu.classList.remove("hidden");
    const caretIndex = Number.isFinite(atIndex) ? atIndex : activeStart;
    const caret = getTextareaCaretRect(textarea, caretIndex >= 0 ? caretIndex : (textarea.selectionStart || 0));
    const taRect = textarea.getBoundingClientRect();
    let left = caret ? caret.left : taRect.left;
    let top = caret ? caret.bottom + 4 : taRect.bottom + 4;
    menu.style.minWidth = "180px";
    const menuWidth = Math.max(180, menu.offsetWidth || 180);
    const menuHeight = Math.max(40, menu.offsetHeight || 40);
    if (left + menuWidth > window.innerWidth - 8) left = Math.max(8, window.innerWidth - menuWidth - 8);
    if (left < 8) left = 8;
    if (top + menuHeight > window.innerHeight - 8) {
      top = Math.max(8, (caret ? caret.top : taRect.top) - menuHeight - 4);
    }
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(top) + "px";
  }

  function filterItems(section, query) {
    const q = String(query || "").toLowerCase();
    return itemsForSection(section).filter(function (item) {
      if (!q) return true;
      return item.token.toLowerCase().indexOf(q) >= 0 || String(item.name).toLowerCase().indexOf(q) >= 0;
    });
  }

  function detectMention(textarea) {
    const pos = textarea.selectionStart || 0;
    const before = textarea.value.slice(0, pos);
    const match = before.match(/(^|[\s([{])@([^\s@]*)$/);
    if (!match) return null;
    return {
      start: pos - match[2].length - 1,
      end: pos,
      query: match[2] || "",
    };
  }

  function bindTextarea(field) {
    const textarea = document.querySelector('textarea[name="' + field + '"]');
    if (!textarea || textarea.dataset.mentionBound === "1") return;
    textarea.dataset.mentionBound = "1";
    ensureHighlightEditor(textarea);
    renderMentionHighlight(textarea);

    textarea.addEventListener("input", function () {
      renderMentionHighlight(textarea);
      const mention = detectMention(textarea);
      if (!mention) {
        if (activeField === field) hideMenu();
        return;
      }
      activeField = field;
      activeStart = mention.start;
      activeQuery = mention.query;
      showMenu(textarea, filterItems(field, mention.query), mention.start);
    });
    textarea.addEventListener("scroll", function () {
      syncMentionScroll(textarea);
      if (!menu.classList.contains("hidden") && activeField === field && activeStart >= 0) {
        showMenu(textarea, currentItems, activeStart);
      }
    });

    textarea.addEventListener("keydown", function (ev) {
      if (menu.classList.contains("hidden") || activeField !== field) return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        highlight = (highlight + 1) % currentItems.length;
        showMenu(textarea, currentItems, activeStart);
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        highlight = (highlight - 1 + currentItems.length) % currentItems.length;
        showMenu(textarea, currentItems, activeStart);
      } else if (ev.key === "Enter" || ev.key === "Tab") {
        if (!currentItems.length) return;
        ev.preventDefault();
        const item = currentItems[highlight] || currentItems[0];
        insertToken(textarea, activeStart, textarea.selectionStart || activeStart, item.token);
        hideMenu();
      } else if (ev.key === "Escape") {
        hideMenu();
      }
    });

    textarea.addEventListener("blur", function () {
      setTimeout(function () {
        if (document.activeElement && menu.contains(document.activeElement)) return;
        if (activeField === field) hideMenu();
      }, 120);
    });
  }

  SECTION_FIELDS.forEach(bindTextarea);

  document.addEventListener("click", function (ev) {
    if (ev.target.closest(".mention-chip-remove")) return;
    const label = ev.target.closest(".mention-chip-label");
    const chip = label && label.closest(".mention-chip");
    if (chip) {
      const field = chip.getAttribute("data-field");
      const token = chip.getAttribute("data-token") || "";
      const textarea = document.querySelector('textarea[name="' + field + '"]');
      if (!textarea || !token) return;
      const start = textarea.selectionStart != null ? textarea.selectionStart : textarea.value.length;
      insertToken(textarea, start, start, token);
      return;
    }
    const option = ev.target.closest(".mention-option");
    if (option && activeField) {
      const idx = parseInt(option.getAttribute("data-idx") || "0", 10);
      const item = currentItems[idx];
      const textarea = document.querySelector('textarea[name="' + activeField + '"]');
      if (item && textarea) {
        insertToken(textarea, activeStart, textarea.selectionStart || activeStart, item.token);
      }
      hideMenu();
      return;
    }
    if (!ev.target.closest("#mention-selector") && !ev.target.closest("textarea[name]")) {
      hideMenu();
    }
  });

  if (typeof bridge.onRangesChange === "function") {
    bridge.onRangesChange(renderChips);
  }
  if (typeof bridge.onDetailChange === "function") {
    bridge.onDetailChange(function () {
      renderChips();
    });
  }

  renderChips();
  SECTION_FIELDS.forEach(function (field) {
    renderMentionHighlight(document.querySelector('textarea[name="' + field + '"]'));
  });
  return {
    refresh: function () {
      renderChips();
      SECTION_FIELDS.forEach(function (field) {
        renderMentionHighlight(document.querySelector('textarea[name="' + field + '"]'));
      });
    },
    hideMenu: hideMenu,
  };
}
