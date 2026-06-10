from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from inspect import currentframe
from os.path import abspath, basename
from pathlib import Path
from typing import Any, AsyncIterator

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
GRAY = "\033[90m"
END = "\033[0m"

ERROR_LOG_LEVEL = 0
WARNING_LOG_LEVEL = 1
INFO_LOG_LEVEL = 2
DEBUG_LOG_LEVEL = 3
RESULT_LOG_LEVEL = 4

GLOBAL_LOG_LEVEL = -1
_globalLogLevelInitialized = False
_LOG_DIR = Path("./log")
_file_lock = threading.Lock()

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
source_sha256_var: ContextVar[str | None] = ContextVar("source_sha256", default=None)

def redText(text: object) -> str:
    return f"{RED}{text}{END}"


def greenText(text: object) -> str:
    return f"{GREEN}{text}{END}"


def yellowText(text: object) -> str:
    return f"{YELLOW}{text}{END}"


def magentaText(text: object) -> str:
    return f"{MAGENTA}{text}{END}"


def cyanText(text: object) -> str:
    return f"{CYAN}{text}{END}"


def grayText(text: object) -> str:
    return f"{GRAY}{text}{END}"


logLevel: dict[int, str] = {
    ERROR_LOG_LEVEL: "ERROR",
    WARNING_LOG_LEVEL: "WARNING",
    INFO_LOG_LEVEL: "INFO",
    DEBUG_LOG_LEVEL: "DEBUG",
    RESULT_LOG_LEVEL: "RESULT",
}

logColor: dict[int, Any] = {
    ERROR_LOG_LEVEL: redText,
    WARNING_LOG_LEVEL: yellowText,
    INFO_LOG_LEVEL: greenText,
    DEBUG_LOG_LEVEL: magentaText,
    RESULT_LOG_LEVEL: cyanText,
}


def safe_text(value: str, max_len: int = 200) -> str:
    if len(value) <= max_len:
        return value
    return value[:max_len] + "..."


def bind_log_context(
    *,
    request_id: str | None = None,
    source_sha256: str | None = None,
) -> tuple[Token[str | None] | None, Token[str | None] | None]:
    request_token = request_id_var.set(request_id) if request_id is not None else None
    sha_token = source_sha256_var.set(source_sha256) if source_sha256 is not None else None
    return request_token, sha_token


def reset_log_context(
    request_token: Token[str | None] | None,
    sha_token: Token[str | None] | None,
) -> None:
    if request_token is not None:
        request_id_var.reset(request_token)
    if sha_token is not None:
        source_sha256_var.reset(sha_token)


def _merge_log_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    merged: dict[str, Any] = dict(params) if params else {}
    request_id = request_id_var.get()
    source_sha256 = source_sha256_var.get()
    if request_id is not None:
        merged.setdefault("request_id", request_id)
    if source_sha256 is not None:
        merged.setdefault("source_sha256", source_sha256)
    return merged or None


def _build_log_record(
    currentLogLevel: int,
    message: str,
    parameters: dict[str, Any] | None,
    file: str,
    line: int,
    caller: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": logLevel[currentLogLevel],
        "file": file,
        "line": line,
        "caller": caller,
        "message": message,
    }
    if parameters:
        for key, value in parameters.items():
            if key not in record:
                record[key] = value
    return record


def _serialize_log_record(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, default=str)


def _append_log_file(record: dict[str, Any]) -> None:
    day = datetime.now().astimezone().date().isoformat()
    path = _LOG_DIR / f"{day}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = _serialize_log_record(record) + "\n"
    with _file_lock:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def logInit(globalLogLevel: int = INFO_LOG_LEVEL, log_dir: str | Path = "./log") -> None:
    global GLOBAL_LOG_LEVEL, _globalLogLevelInitialized, _LOG_DIR

    if globalLogLevel < ERROR_LOG_LEVEL or globalLogLevel > RESULT_LOG_LEVEL:
        raise ValueError("Invalid log level")

    GLOBAL_LOG_LEVEL = globalLogLevel
    _LOG_DIR = Path(log_dir)
    _globalLogLevelInitialized = True
    Log(globalLogLevel, "Global log level initialized correctly", {"log_dir": str(_LOG_DIR)})


def Log(
    currentLogLevel: int,
    message: str,
    params: dict[str, Any] | None = None,
    override: bool = False,
    *,
    json: bool = False,
    to_file: bool = False,
) -> str | None:
    global GLOBAL_LOG_LEVEL, _globalLogLevelInitialized

    if not _globalLogLevelInitialized:
        GLOBAL_LOG_LEVEL = INFO_LOG_LEVEL
        _globalLogLevelInitialized = True

    if currentLogLevel not in logLevel:
        raise ValueError("Invalid log level")

    localLogLevel = GLOBAL_LOG_LEVEL
    if override:
        localLogLevel = currentLogLevel
    parameters = _merge_log_params(params)

    if currentLogLevel > localLogLevel:
        return None

    date = datetime.now().astimezone().isoformat()
    colorFn = logColor[currentLogLevel]
    logType = colorFn(logLevel[currentLogLevel])

    frame = currentframe()
    if frame is None or frame.f_back is None:
        raise RuntimeError("Cannot resolve caller frame")
    callerFrame = frame.f_back
    file = basename(abspath(callerFrame.f_code.co_filename))
    line = callerFrame.f_lineno

    callerName = callerFrame.f_code.co_name
    if callerName == "<module>":
        callerName = "main"

    callerColored = colorFn(callerName)
    messageColored = colorFn(message)

    if parameters is not None:
        loc = colorFn(f"{file}:{line}")
        paramsText = grayText(f"| {parameters}")
        logMessage = f"[{date}][{logType}][{loc}][{callerColored}] {messageColored} {paramsText}"
    else:
        fileColored = colorFn(file)
        logMessage = f"[{date}][{logType}][{fileColored}][{callerColored}] {messageColored}"

    print(logMessage, file=sys.stdout, flush=True)

    record = _build_log_record(currentLogLevel, message, parameters, file, line, callerName)
    if to_file:
        _append_log_file(record)
    if json:
        return _serialize_log_record(record)
    return None


@asynccontextmanager
async def log_stage_block_async(stage_name: str) -> AsyncIterator[None]:
    start = time.perf_counter()
    Log(INFO_LOG_LEVEL, "stage start", {"stage": stage_name, "event": "start"})
    try:
        yield
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        Log(
            INFO_LOG_LEVEL,
            "stage end",
            {"stage": stage_name, "event": "end", "duration_ms": duration_ms},
        )
