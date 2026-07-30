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
    label.insertBefore(row, textarea);
  });
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

export function bootPageGuidanceMentions(bridge, getAnnotations) {
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

  function itemsForSection(section) {
    const payload = typeof getAnnotations === "function" ? getAnnotations() : [];
    const out = [];
    payload.forEach(function (pageItem) {
      const sectionName = sectionOfPage(pageItem.page);
      if (sectionName !== section) return;
      (pageItem.elements || []).forEach(function (el) {
        out.push({
          page: pageItem.page,
          id: el.id,
          name: el.name || el.type,
          type: el.type,
          token: mentionToken(el.name || el.type),
        });
      });
    });
    return out;
  }

  function renderChips() {
    ensureChipRows();
    SECTION_FIELDS.forEach(function (field) {
      const row = document.querySelector('[data-mention-chips="' + field + '"]');
      if (!row) return;
      const items = itemsForSection(field);
      if (!items.length) {
        row.innerHTML = "";
        row.classList.add("is-empty");
        return;
      }
      row.classList.remove("is-empty");
      row.innerHTML = items
        .map(function (item) {
          return (
            '<button type="button" class="mention-chip" data-field="' +
            field +
            '" data-token="' +
            escapeHtml(item.token) +
            '" title="p.' +
            item.page +
            " · " +
            escapeHtml(item.type) +
            '">@' +
            escapeHtml(item.token) +
            "<small>p." +
            item.page +
            "</small></button>"
          );
        })
        .join("");
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

  function showMenu(textarea, items, rectAnchor) {
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
          "<span>p." +
          item.page +
          " · " +
          escapeHtml(item.type) +
          "</span></button>"
        );
      })
      .join("");
    menu.classList.remove("hidden");
    const rect = rectAnchor || textarea.getBoundingClientRect();
    menu.style.left = Math.round(rect.left + window.scrollX) + "px";
    menu.style.top = Math.round(rect.bottom + window.scrollY + 4) + "px";
    menu.style.minWidth = Math.max(180, Math.round(rect.width * 0.45)) + "px";
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

    textarea.addEventListener("input", function () {
      const mention = detectMention(textarea);
      if (!mention) {
        if (activeField === field) hideMenu();
        return;
      }
      activeField = field;
      activeStart = mention.start;
      activeQuery = mention.query;
      showMenu(textarea, filterItems(field, mention.query), textarea.getBoundingClientRect());
    });

    textarea.addEventListener("keydown", function (ev) {
      if (menu.classList.contains("hidden") || activeField !== field) return;
      if (ev.key === "ArrowDown") {
        ev.preventDefault();
        highlight = (highlight + 1) % currentItems.length;
        showMenu(textarea, currentItems, textarea.getBoundingClientRect());
      } else if (ev.key === "ArrowUp") {
        ev.preventDefault();
        highlight = (highlight - 1 + currentItems.length) % currentItems.length;
        showMenu(textarea, currentItems, textarea.getBoundingClientRect());
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
    const chip = ev.target.closest(".mention-chip");
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

  renderChips();
  return { refresh: renderChips, hideMenu: hideMenu };
}
