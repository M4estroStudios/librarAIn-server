(function (global) {
  "use strict";

  function create(mergePages) {
    function pagesKey(item) {
      if (!item || !item.aligned || !item.aligned.length) return "";
      return item.aligned.slice().sort(function (a, b) { return a - b; }).join(",");
    }

    function yearSpan(label) {
      var m = String(label || "").match(/^(\d+)(?:\s*[-–—]\s*(\d+))?(?:\s+a\.C\.)?$/i);
      if (!m) return null;
      var a = Number(m[1]);
      var b = m[2] != null ? Number(m[2]) : a;
      var isAc = /\ba\.C\./i.test(label);
      return { hi: Math.max(a, b), lo: Math.min(a, b), isAc: isAc, count: Math.abs(a - b) + 1 };
    }

    function spanLabel(span) {
      if (span.hi === span.lo) {
        return span.isAc ? span.hi + " a.C." : String(span.hi);
      }
      return span.isAc
        ? span.hi + "-" + span.lo + " a.C."
        : span.lo + "-" + span.hi;
    }

    function spansAdjacent(prev, next) {
      if (!prev || !next || prev.isAc !== next.isAc) return false;
      if (prev.isAc) return next.hi === prev.lo - 1;
      return next.lo === prev.hi + 1;
    }

    function mergeSpans(prev, next) {
      return {
        hi: Math.max(prev.hi, next.hi),
        lo: Math.min(prev.lo, next.lo),
        isAc: prev.isAc,
        count: Math.max(prev.hi, next.hi) - Math.min(prev.lo, next.lo) + 1
      };
    }

    function unionViaKinds(nodes) {
      var seen = {};
      var out = [];
      nodes.forEach(function (node) {
        (node.viaKinds || []).forEach(function (kind) {
          if (seen[kind]) return;
          seen[kind] = true;
          out.push(kind);
        });
      });
      if (!seen.range) out.push("range");
      return out;
    }

    function makeRangeNode(chunk) {
      var span = yearSpan(chunk[0].label);
      for (var i = 1; i < chunk.length; i++) {
        span = mergeSpans(span, yearSpan(chunk[i].label));
      }
      var first = chunk[0];
      return {
        label: spanLabel(span),
        isAc: span.isAc,
        aligned: first.aligned.slice(),
        original: first.original.slice(),
        viaKinds: unionViaKinds(chunk),
        monthList: [],
        sortKey: first.sortKey,
        isRange: span.hi !== span.lo,
        rangeCount: span.count
      };
    }

    function collapseMonthRuns(monthList) {
      if (!monthList || monthList.length < 2) return monthList || [];
      var out = [];
      var i = 0;
      while (i < monthList.length) {
        var start = monthList[i];
        var key = pagesKey(start);
        if (!key) {
          out.push(start);
          i += 1;
          continue;
        }
        var j = i + 1;
        while (j < monthList.length) {
          var next = monthList[j];
          if (next.monthNum !== monthList[j - 1].monthNum + 1) break;
          if (pagesKey(next) !== key) break;
          j += 1;
        }
        if (j - i < 2) {
          out.push(start);
          i += 1;
          continue;
        }
        var end = monthList[j - 1];
        var days = [];
        var pages = { aligned: [], original: [] };
        for (var k = i; k < j; k++) {
          monthList[k].days.forEach(function (day) { days.push(day); });
          mergePages(pages, monthList[k].aligned, monthList[k].original);
        }
        out.push({
          month: start.month + "–" + end.month,
          monthNum: start.monthNum,
          days: days,
          aligned: pages.aligned,
          original: pages.original,
          isRange: true
        });
        i = j;
      }
      return out;
    }

    function collapseYearRuns(yearList) {
      if (!yearList || !yearList.length) return yearList || [];
      var prepared = yearList.map(function (node) {
        if (node.monthList && node.monthList.length) {
          node.monthList = collapseMonthRuns(node.monthList);
        }
        return node;
      });
      var out = [];
      var i = 0;
      while (i < prepared.length) {
        var start = prepared[i];
        var startSpan = yearSpan(start.label);
        var key = pagesKey(start);
        var hasMonths = start.monthList && start.monthList.length;
        if (!startSpan || !key || hasMonths) {
          out.push(start);
          i += 1;
          continue;
        }
        var j = i + 1;
        var cursor = startSpan;
        while (j < prepared.length) {
          var next = prepared[j];
          var nextSpan = yearSpan(next.label);
          if (!nextSpan || next.monthList && next.monthList.length) break;
          if (pagesKey(next) !== key) break;
          if (!spansAdjacent(cursor, nextSpan)) break;
          cursor = mergeSpans(cursor, nextSpan);
          j += 1;
        }
        if (j - i < 2) {
          out.push(start);
          i += 1;
          continue;
        }
        out.push(makeRangeNode(prepared.slice(i, j)));
        i = j;
      }
      return out;
    }

    return { collapseYearRuns: collapseYearRuns, collapseMonthRuns: collapseMonthRuns };
  }

  global.BiblioTimeCollapse = { create: create };
})(window);
