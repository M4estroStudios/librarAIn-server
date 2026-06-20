(function (global) {
  function pageLabel() {
    var title = (document.title || "").split("\u2014")[0].trim();
    return title || global.location.pathname || "web";
  }

  function formatLine(level, message, params) {
    var line = "[" + new Date().toISOString() + "][" + level + "][" + pageLabel() + "] " + message;
    if (params && typeof params === "object" && Object.keys(params).length) {
      line += " | " + JSON.stringify(params);
    }
    return line;
  }

  function write(level, consoleFn, message, params) {
    var line = formatLine(level, message, params);
    consoleFn(line);
    if (params && params.error instanceof Error) {
      consoleFn(params.error);
    }
    return line;
  }

  function fromError(err) {
    if (err instanceof Error) {
      return { message: err.message, error: err };
    }
    return { message: String(err) };
  }

  var log = {
    error: function (message, params) {
      return write("ERROR", global.console.error.bind(global.console), message, params);
    },
    warn: function (message, params) {
      return write("WARNING", global.console.warn.bind(global.console), message, params);
    },
    info: function (message, params) {
      return write("INFO", global.console.info.bind(global.console), message, params);
    },
    debug: function (message, params) {
      return write("DEBUG", global.console.debug.bind(global.console), message, params);
    },
    reportError: function (context, err) {
      var params = fromError(err);
      return log.error(context, params);
    },
    logHttpFailure: function (url, status, detail) {
      var level = status >= 500 ? "error" : "warn";
      return log[level]("api request failed", { url: url, status: status, error: detail });
    },
  };

  global.LibrarAInLog = log;

  global.addEventListener("error", function (ev) {
    log.error("uncaught error", {
      message: ev.message,
      source: ev.filename,
      line: ev.lineno,
      col: ev.colno,
      error: ev.error instanceof Error ? ev.error : undefined,
    });
  });

  global.addEventListener("unhandledrejection", function (ev) {
    log.reportError("unhandled rejection", ev.reason);
  });
})(window);
