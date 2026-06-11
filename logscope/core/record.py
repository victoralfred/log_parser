"""Normalized log record schema shared by all scanners.

Every scanner, whatever its channel (files, journald, container runtime...),
yields these records. The ingest engine — not scanners — fills `channel`,
`fingerprint` and the derived epoch timestamp.
"""

from dataclasses import dataclass, field


@dataclass
class LogRecord:
    time: str = ""            # timestamp string as found in the source
    logger: str = ""          # raw logger name (CORE, TRACE-LOADER, ...)
    level: str = ""           # TRACE/DEBUG/INFO/WARN/ERROR/CRITICAL or "" if unknown
    file: str = ""            # caller file path (may be empty for non-agent channels)
    line: int = 0
    func: str = ""
    msg: str = ""
    service: str = ""         # classified service (agent, trace-agent, ...)
    component: str = ""       # classified source component (pkg path prefix)
    lineno: int = 0           # line number within the source
    source: str = ""          # SourceInfo.source_id (file path, journald unit, ...)
    parsed: bool = True       # False for lines no parser understood
    continuation: list = field(default_factory=list)  # multiline tails (stack traces)
    channel: str = ""         # "file" | "journald" | ... — set by the engine
    fingerprint: str = ""     # message-template hash — set by the engine
    extra: dict = field(default_factory=dict)  # scanner-specific fields (unit, pid, event...)
