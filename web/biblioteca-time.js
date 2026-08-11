(function (global) {
  "use strict";

  var MONTH_ORDER = {
    gennaio: 1, febbraio: 2, marzo: 3, aprile: 4, maggio: 5, giugno: 6,
    luglio: 7, agosto: 8, settembre: 9, ottobre: 10, novembre: 11, dicembre: 12
  };
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function displayLabel(label) {
    return String(label || "")
      .replace(/\s*d\.\s*C\.?\s*$/i, "")
      .replace(/\s*a\.\s*C\.?\s*$/i, " a.C.")
      .replace(/\s+/g, " ")
      .trim();
  }

  function isAcLabel(label) {
    return /\ba\.C\.?\s*$/i.test(String(label || ""));
  }

  var VIA_BADGE_LABELS = {
    secolo: "Secolo",
    inizi: "Inizi",
    fine: "Fine",
    metà: "Metà",
    età: "Età",
    periodo: "Periodo",
    range: "Range"
  };

  function sectionEntries(section) {
    if (!section || typeof section !== "object") return [];
    var acNums = {};
    Object.keys(section).forEach(function (label) {
      var shown = displayLabel(label);
      var match = shown.match(/^(\d+) a\.C\.?$/i);
      if (match) acNums[match[1]] = true;
    });
    return Object.keys(section).map(function (label) {
      var entry = section[label];
      var aligned = [];
      var original = [];
      var viaKinds = [];
      var hasDirect = false;
      if (entry && typeof entry === "object") {
        if (Array.isArray(entry.aligned_pages)) aligned = entry.aligned_pages.filter(function (n) { return typeof n === "number"; });
        if (Array.isArray(entry.original_pages)) original = entry.original_pages.filter(function (n) { return typeof n === "number"; });
        if (Array.isArray(entry.direct_pages) && entry.direct_pages.length) hasDirect = true;
        if (!hasDirect && entry.via && typeof entry.via === "object") {
          viaKinds = Object.keys(entry.via).filter(function (k) {
            return Array.isArray(entry.via[k]) && entry.via[k].length;
          });
        }
      }
      var shown = displayLabel(label);
      return {
        label: shown,
        aligned: aligned,
        original: original,
        isAc: isAcLabel(shown),
        viaKinds: viaKinds
      };
    }).filter(function (item) {
      if (!item.label) return false;
      if (/^\d+$/.test(item.label) && acNums[item.label]) return false;
      return true;
    });
  }

  function viaBadgesHtml(kinds) {
    if (!kinds || !kinds.length) return "";
    return kinds.map(function (kind) {
      var text = VIA_BADGE_LABELS[kind] || kind;
      return "<span class='time-index-via-badge' data-via='" + esc(kind) + "' title='Anno solo da espansione: " + esc(text) + "'>" + esc(text) + "</span>";
    }).join("");
  }

  function parseDateParts(label) {
    var cleaned = String(label || "").trim();
    var match = cleaned.match(/^(?:(\d{1,2})(?:[–—-](\d{1,2}))?\s+)?([a-zà]+(?:\s*[–—-]\s*[a-zà]+)?)(?:\s+(\d{1,4})(?:\s+(a\.C\.))?)?$/i);
    if (!match) return null;
    var monthPart = match[3].toLowerCase().split(/\s*[–—-]\s*/)[0];
    if (!MONTH_ORDER[monthPart]) return null;
    var yearNum = match[4] ? Number(match[4]) : null;
    var isAc = !!match[5];
    var yearKey = yearNum == null ? null : (isAc ? yearNum + " a.C." : String(yearNum));
    return {
      day: match[1] ? Number(match[1]) : 0,
      month: monthPart,
      monthNum: MONTH_ORDER[monthPart],
      yearKey: yearKey,
      yearOrd: yearNum == null ? null : (isAc ? -yearNum : yearNum),
      isAc: isAc
    };
  }

  function yearSortKey(label) {
    var m = String(label || "").match(/^(\d+)(?:\s+a\.C\.)?$/i);
    if (!m) return [1, 1e9, String(label || "").toLowerCase()];
    var n = Number(m[1]);
    if (/a\.C\./i.test(label)) return [0, -n, label.toLowerCase()];
    return [0, n, label.toLowerCase()];
  }

  function cmpKey(a, b) {
    for (var i = 0; i < a.length; i++) {
      if (a[i] < b[i]) return -1;
      if (a[i] > b[i]) return 1;
    }
    return 0;
  }

  function pageChipsHtml(item) {
    if (!item.aligned || !item.aligned.length) return "<span class='time-index-empty'>—</span>";
    return item.aligned.map(function (page, idx) {
      var orig = item.original && item.original[idx];
      var title = "Apri pagina allineata " + page + (orig != null ? (" (originale " + orig + ")") : "");
      return "<button type='button' class='time-index-page' data-time-page='" + page + "' title='" + esc(title) + "'>p.&nbsp;" + page + "</button>";
    }).join("");
  }

  function matchesQuery(item, q) {
    if (!q) return true;
    if (item.label && item.label.toLowerCase().indexOf(q) >= 0) return true;
    if (item.aligned && String(item.aligned.join(" ")).indexOf(q) >= 0) return true;
    return false;
  }

  function mergePages(target, aligned, original) {
    if (!target || !aligned || !aligned.length) return;
    if (!target.aligned) target.aligned = [];
    if (!target.original) target.original = [];
    aligned.forEach(function (page, idx) {
      if (typeof page !== "number" || target.aligned.indexOf(page) >= 0) return;
      target.aligned.push(page);
      var orig = original && original[idx];
      target.original.push(orig != null ? orig : null);
    });
  }

  var collapseApi = (global.BiblioTimeCollapse && typeof global.BiblioTimeCollapse.create === "function")
    ? global.BiblioTimeCollapse.create(mergePages)
    : { collapseYearRuns: function (list) { return list || []; } };
  var collapseYearRuns = collapseApi.collapseYearRuns;

  function buildTimeline(years, dates) {
    var yearMap = {};
    years.forEach(function (item) {
      yearMap[item.label] = {
        label: item.label,
        isAc: item.isAc,
        aligned: item.aligned.slice(),
        original: item.original.slice(),
        viaKinds: item.viaKinds ? item.viaKinds.slice() : [],
        months: {},
        sortKey: yearSortKey(item.label)
      };
    });

    var undated = [];
    dates.forEach(function (item) {
      var parts = parseDateParts(item.label);
      if (!parts || !parts.yearKey) {
        undated.push(item);
        return;
      }
      if (!yearMap[parts.yearKey]) {
        yearMap[parts.yearKey] = {
          label: parts.yearKey,
          isAc: parts.isAc,
          aligned: [],
          original: [],
          viaKinds: [],
          months: {},
          sortKey: yearSortKey(parts.yearKey)
        };
      }
      var yearNode = yearMap[parts.yearKey];
      if (!yearNode.months[parts.month]) {
        yearNode.months[parts.month] = { month: parts.month, monthNum: parts.monthNum, days: [], aligned: [], original: [] };
      }
      var monthNode = yearNode.months[parts.month];
      monthNode.days.push({
        label: item.label,
        day: parts.day,
        aligned: item.aligned,
        original: item.original
      });
      mergePages(monthNode, item.aligned, item.original);
      mergePages(yearNode, item.aligned, item.original);
    });

    Object.keys(yearMap).forEach(function (key) {
      var yearNode = yearMap[key];
      yearNode.monthList = Object.keys(yearNode.months).map(function (m) {
        return yearNode.months[m];
      }).sort(function (a, b) { return a.monthNum - b.monthNum; });
      yearNode.monthList.forEach(function (monthNode) {
        monthNode.days.sort(function (a, b) {
          if (a.day !== b.day) return a.day - b.day;
          return a.label.localeCompare(b.label);
        });
      });
    });

    var yearList = Object.keys(yearMap).map(function (k) { return yearMap[k]; });
    yearList.sort(function (a, b) { return cmpKey(a.sortKey, b.sortKey); });
    undated.sort(function (a, b) {
      var pa = parseDateParts(a.label) || { monthNum: 99, day: 99 };
      var pb = parseDateParts(b.label) || { monthNum: 99, day: 99 };
      if (pa.monthNum !== pb.monthNum) return pa.monthNum - pb.monthNum;
      if (pa.day !== pb.day) return pa.day - pb.day;
      return a.label.localeCompare(b.label);
    });
    return { years: collapseYearRuns(yearList), undated: undated };
  }

  function filterTimeline(timeline, query) {
    var q = String(query || "").trim().toLowerCase();
    if (!q) return { years: collapseYearRuns(timeline.years.slice()), undated: timeline.undated };
    var years = [];
    timeline.years.forEach(function (yearNode) {
      var yearHit = matchesQuery(yearNode, q) || yearNode.label.toLowerCase().indexOf(q) >= 0;
      var months = [];
      (yearNode.monthList || []).forEach(function (monthNode) {
        var monthHit = monthNode.month.indexOf(q) >= 0;
        var days = monthNode.days.filter(function (day) { return matchesQuery(day, q); });
        if (yearHit || monthHit) days = monthNode.days.slice();
        if (days.length || monthHit) {
          var monthDays = days.length ? days : monthNode.days.slice();
          var monthPages = { aligned: [], original: [] };
          monthDays.forEach(function (day) { mergePages(monthPages, day.aligned, day.original); });
          months.push({
            month: monthNode.month,
            monthNum: monthNode.monthNum,
            days: monthDays,
            aligned: monthPages.aligned,
            original: monthPages.original
          });
        }
      });
      if (yearHit || months.length) {
        years.push({
          label: yearNode.label,
          isAc: yearNode.isAc,
          aligned: yearNode.aligned,
          original: yearNode.original,
          viaKinds: yearNode.viaKinds,
          monthList: yearHit && !months.length ? (yearNode.monthList || []).slice() : months,
          sortKey: yearNode.sortKey,
          forceOpen: !!q,
          isRange: !!yearNode.isRange,
          rangeCount: yearNode.rangeCount
        });
      }
    });
    var undated = timeline.undated.filter(function (item) { return matchesQuery(item, q); });
    return { years: collapseYearRuns(years), undated: undated };
  }

  function renderDayRow(day) {
    return "<div class='time-index-day-row'>" +
      "<div class='time-index-day-label'>" + esc(day.label) + "</div>" +
      "<div class='time-index-pages'>" + pageChipsHtml(day) + "</div>" +
      "</div>";
  }

  function renderMonth(monthNode, forceOpen) {
    var open = forceOpen ? " open" : "";
    var daysHtml = monthNode.days.map(renderDayRow).join("");
    var pages = "<div class='time-index-pages'>" + pageChipsHtml(monthNode) + "</div>";
    return "<details class='time-index-month'" + open + ">" +
      "<summary>" +
      "<span class='time-index-month-summary-main'><span class='time-index-month-name'>" + esc(monthNode.month) + "</span>" +
      "<span class='time-index-count'>" + monthNode.days.length + " giorn" + (monthNode.days.length === 1 ? "o" : "i") + "</span></span>" +
      pages +
      "</summary>" +
      "<div class='time-index-days'>" + daysHtml + "</div>" +
      "</details>";
  }

  function renderYear(yearNode) {
    var hasMonths = yearNode.monthList && yearNode.monthList.length;
    var badge = yearNode.isAc ? "<span class='time-index-ac-badge'>a.C.</span>" : "";
    var via = viaBadgesHtml(yearNode.viaKinds);
    var pages = "<div class='time-index-pages'>" + pageChipsHtml(yearNode) + "</div>";
    var kind = yearNode.isRange
      ? (yearNode.isAc ? "Anni a.C." : "Anni")
      : (yearNode.isAc ? "Anno a.C." : "Anno");
    var countHtml = "";
    if (yearNode.isRange && yearNode.rangeCount) {
      countHtml = "<span class='time-index-count'>" + yearNode.rangeCount + " anni</span>";
    } else if (hasMonths) {
      countHtml = "<span class='time-index-count'>" + yearNode.monthList.length + " mes" + (yearNode.monthList.length === 1 ? "e" : "i") + "</span>";
    }
    if (!hasMonths) {
      return "<div class='time-index-year-row" + (yearNode.isAc ? " is-ac" : "") + (via ? " is-via" : "") + (yearNode.isRange ? " is-range" : "") + "'>" +
        "<div class='time-index-label'><span class='time-index-kind'>" + kind + "</span>" +
        "<span class='time-index-label-text'>" + esc(yearNode.label) + badge + via + countHtml + "</span></div>" + pages + "</div>";
    }
    var open = yearNode.forceOpen ? " open" : "";
    var monthsHtml = yearNode.monthList.map(function (m) { return renderMonth(m, yearNode.forceOpen); }).join("");
    return "<details class='time-index-year" + (yearNode.isAc ? " is-ac" : "") + (via ? " is-via" : "") + "'" + open + ">" +
      "<summary>" +
      "<span class='time-index-year-summary-main'><span class='time-index-kind'>" + kind + "</span>" +
      "<span class='time-index-label-text'>" + esc(yearNode.label) + badge + via + "</span>" +
      countHtml + "</span>" +
      pages +
      "</summary>" +
      "<div class='time-index-year-body'>" + monthsHtml + "</div>" +
      "</details>";
  }

  function isPeriodGroupLabel(label) {
    return !/^(\d+)(?:\s*[-–—]\s*\d+)?(?:\s+a\.C\.)?$/i.test(String(label || "").trim());
  }

  function readShowPeriodGroups() {
    try { return window.localStorage.getItem("biblio.time.showPeriodGroups") === "1"; }
    catch (_) { return false; }
  }

  function writeShowPeriodGroups(on) {
    try { window.localStorage.setItem("biblio.time.showPeriodGroups", on ? "1" : "0"); }
    catch (_) {}
  }

  function applyPeriodGroupFilter(timeline, showPeriodGroups) {
    if (showPeriodGroups) return timeline;
    return {
      years: (timeline.years || []).filter(function (y) { return !isPeriodGroupLabel(y.label); }),
      undated: timeline.undated || []
    };
  }

  function renderTimelineHtml(timeline) {
    var ac = timeline.years.filter(function (y) { return y.isAc; });
    var dc = timeline.years.filter(function (y) { return !y.isAc; });
    var html = "";
    if (ac.length) {
      html += "<div class='time-index-group-head is-ac'>Avanti Cristo (a.C.) · " + ac.length + "</div>";
      ac.forEach(function (y) { html += renderYear(y); });
    }
    if (dc.length) {
      html += "<div class='time-index-group-head is-dc'>Dopo Cristo · " + dc.length + "</div>";
      dc.forEach(function (y) { html += renderYear(y); });
    }
    if (timeline.undated.length) {
      html += "<div class='time-index-group-head is-undated'>Senza anno · " + timeline.undated.length + "</div>";
      timeline.undated.forEach(function (item) {
        html += "<div class='time-index-year-row is-undated'>" +
          "<div class='time-index-label'><span class='time-index-kind'>Data</span>" +
          "<span class='time-index-label-text'>" + esc(item.label) + "</span></div>" +
          "<div class='time-index-pages'>" + pageChipsHtml(item) + "</div></div>";
      });
    }
    return html;
  }

  function renderList(state) {
    var filtered = applyPeriodGroupFilter(filterTimeline(state.timeline, state.query), state.showPeriodGroups);
    var body = renderTimelineHtml(filtered);
    if (!body) body = "<p class='hint'>Nessuna voce" + (state.query ? " per questo filtro" : "") + ".</p>";
    state.listEl.innerHTML = body;
    state.metaEl.textContent = filtered.years.length + " anni · " + filtered.undated.length + " senza anno";
    state.helpEl.textContent = "Anno → mesi → giorni in ordine cronologico. Le date senza anno sono in fondo. I chip aprono le pagine.";
  }

  function bind(state) {
    state.root.addEventListener("click", function (ev) {
      var pageBtn = ev.target.closest("[data-time-page]");
      if (pageBtn) {
        ev.preventDefault();
        ev.stopPropagation();
        var page = Number(pageBtn.getAttribute("data-time-page"));
        if (!isNaN(page) && typeof state.onOpenPage === "function") state.onOpenPage(page);
      }
    });
    state.filterEl.addEventListener("input", function () {
      state.query = state.filterEl.value || "";
      renderList(state);
    });
    if (state.periodToggleEl) {
      state.periodToggleEl.addEventListener("change", function () {
        state.showPeriodGroups = !!state.periodToggleEl.checked;
        writeShowPeriodGroups(state.showPeriodGroups);
        renderList(state);
      });
    }
  }

  function render(viewEl, data, opts) {
    opts = opts || {};
    if (!viewEl) return;
    if (!data || typeof data !== "object") {
      viewEl.innerHTML = "<p class='hint'>TIME_INDEX non valido.</p>";
      return;
    }
    var years = sectionEntries(data.years);
    var dates = sectionEntries(data.dates);
    var timeline = buildTimeline(years, dates);
    var acCount = timeline.years.filter(function (y) { return y.isAc; }).length;
    var showPeriodGroups = readShowPeriodGroups();
    viewEl.innerHTML =
      "<div class='time-index-view'>" +
      "<div class='time-index-sticky'>" +
      "<div class='time-index-head'>" +
      "<div class='time-index-title'>TIME INDEX</div>" +
      "<div class='time-index-summary'>" + timeline.years.length + " anni (" + acCount + " a.C.) · " + dates.length + " date · " + timeline.undated.length + " senza anno</div>" +
      "</div>" +
      "<p class='time-index-help'></p>" +
      "<div class='time-index-toolbar'>" +
      "<input type='search' class='time-index-filter' placeholder='Filtra anno, data o pagina…' aria-label='Filtra time index'>" +
      "<label class='time-index-period-toggle'><input type='checkbox' class='time-index-period-check'" + (showPeriodGroups ? " checked" : "") + "> Mostra etichette periodo</label>" +
      "<div class='time-index-meta'></div>" +
      "</div>" +
      "</div>" +
      "<div class='time-index-list'></div>" +
      "</div>";
    var state = {
      root: viewEl.querySelector(".time-index-view"),
      listEl: viewEl.querySelector(".time-index-list"),
      metaEl: viewEl.querySelector(".time-index-meta"),
      helpEl: viewEl.querySelector(".time-index-help"),
      filterEl: viewEl.querySelector(".time-index-filter"),
      periodToggleEl: viewEl.querySelector(".time-index-period-check"),
      timeline: timeline,
      query: "",
      showPeriodGroups: showPeriodGroups,
      onOpenPage: opts.onOpenPage
    };
    bind(state);
    renderList(state);
  }

  global.BiblioTimeIndexView = { render: render };
})(window);
