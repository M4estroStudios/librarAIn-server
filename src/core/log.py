from __future__ import annotations

import sys
from datetime import datetime
from inspect import currentframe
from os.path import abspath, basename
from typing import Any

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


redText = lambda text: f"{RED}{text}{END}"

greenText = lambda text: f"{GREEN}{text}{END}"

yellowText = lambda text: f"{YELLOW}{text}{END}"

magentaText = lambda text: f"{MAGENTA}{text}{END}"

cyanText = lambda text: f"{CYAN}{text}{END}"

grayText = lambda text: f"{GRAY}{text}{END}"


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


def logInit(globalLogLevel: int = INFO_LOG_LEVEL) -> None:
    global GLOBAL_LOG_LEVEL, _globalLogLevelInitialized

    if globalLogLevel < ERROR_LOG_LEVEL or globalLogLevel > RESULT_LOG_LEVEL:
        raise ValueError("Invalid log level")

    GLOBAL_LOG_LEVEL = globalLogLevel
    _globalLogLevelInitialized = True
    Log(globalLogLevel, "Global log level initialized correctly")


def Log(currentLogLevel: int, message: str, params: dict[str, Any] | None = None, override: bool = False) -> None:
    global GLOBAL_LOG_LEVEL

    if not _globalLogLevelInitialized:
        raise RuntimeError("Global log level not initialized")

    if currentLogLevel not in logLevel:
        raise ValueError("Invalid log level")

    localLogLevel = GLOBAL_LOG_LEVEL
    if override:
        localLogLevel = currentLogLevel
    parameters = params

    if currentLogLevel > localLogLevel:
        return

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
        params = grayText(f"| {parameters}")
        logMessage = f"[{date}][{logType}][{loc}][{callerColored}] {messageColored} {params}"
    else:
        fileColored = colorFn(file)
        logMessage = f"[{date}][{logType}][{fileColored}][{callerColored}] {messageColored}"

    print(logMessage, file=sys.stdout, flush=True)
