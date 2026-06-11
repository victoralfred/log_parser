"""Parsing and classification for Datadog Agent log formats.

The agent text format, defined in pkg/util/log/setup/log_format.go of the
agent source:

    {date} | {LOGGER_NAME} | {LEVEL} | ({file}:{line} in {function}) | {message}

and the optional JSON format (log_format_json: true) with fields
agent/time/level/file/line/func/msg.

Records are classified on two axes:
  - service:   which binary wrote the line (logger name)
  - component: which source package emitted it (caller file path)
"""

import json
import re
from collections import Counter
from dataclasses import asdict

from logscope.core.record import LogRecord

# Logger-name -> service. Names are set per binary in cmd/*/command/command.go.
# Suffixed variants seen in the wild (TRACE-LOADER, SYS-PROBE-LITE) are
# resolved by longest-prefix match in classify_service().
SERVICE_BY_LOGGER = {
    "CORE": "agent",
    "TRACE": "trace-agent",
    "PROCESS": "process-agent",
    "SYS-PROBE": "system-probe",
    "CLUSTER": "cluster-agent",
    "DSD": "dogstatsd",
    "DOGSTATSD": "dogstatsd",
    "SECURITY": "security-agent",
    "INSTALLER": "installer",
    "PRIV-ACTION": "private-action-runner",
    "JMXFETCH": "jmxfetch",
}

LEVELS = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL", "OFF"}

# Text-format line. The message may itself contain " | ", so the tail is
# matched greedily as a single group rather than split.
TEXT_LINE_RE = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\s+\S+|[+-]\d{2}:\d{2}|Z)?)"
    r" \| (?P<logger>[A-Z0-9-]+)"
    r" \| (?P<level>[A-Z]+)"
    r" \| \((?P<file>[^:|]+):(?P<line>\d+) in (?P<func>[^)]+)\)"
    r" \| (?P<msg>.*)$"
)

# Lines logged through config-notification wrappers report the wrapper as the
# caller; the true origin is lost. Classify these by message pattern instead.
CALLER_LOST_FILES = ("pkg/util/log/log.go",)
MSG_PATTERN_COMPONENTS = [
    (re.compile(r"^Set\('"), "config/runtime-settings"),
    (re.compile(r"config lib used|load the configuration|Features detected"), "config/setup"),
    (re.compile(r"remote.config|Remote Config", re.I), "config/remote"),
]


def classify_service(logger):
    if logger in SERVICE_BY_LOGGER:
        return SERVICE_BY_LOGGER[logger]
    # TRACE-LOADER, SYS-PROBE-LITE, etc. -> longest matching base name
    best = ""
    for name, service in SERVICE_BY_LOGGER.items():
        if logger.startswith(name + "-") and len(name) > len(best):
            best = name
    return SERVICE_BY_LOGGER[best] if best else logger.lower()


def classify_component(file_path, msg, depth):
    """Component = package prefix of the caller file path (works for .go and
    .rs paths alike). Falls back to message patterns when the caller is a
    logging wrapper, and to the bare path for vendored/stdlib files."""
    if any(file_path.startswith(p) for p in CALLER_LOST_FILES):
        for pattern, component in MSG_PATTERN_COMPONENTS:
            if pattern.search(msg):
                return component
        return "unattributed"
    parts = file_path.split("/")
    if len(parts) == 1:               # e.g. "value.go" from reflect, stdlib
        return "external/" + parts[0]
    if parts[-1].endswith((".go", ".rs", ".c", ".py")):
        parts = parts[:-1]
    return "/".join(parts[:depth]) or file_path


def parse_json_line(raw):
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or "msg" not in obj:
        return None
    return LogRecord(
        time=str(obj.get("time", "")),
        logger=str(obj.get("agent", "")).upper(),
        level=str(obj.get("level", "")).upper(),
        file=str(obj.get("file", "")),
        line=int(obj.get("line", 0) or 0),
        func=str(obj.get("func", "")),
        msg=str(obj.get("msg", "")),
    )


def parse_text_line(raw):
    m = TEXT_LINE_RE.match(raw)
    if not m:
        return None
    return LogRecord(
        time=m["time"],
        logger=m["logger"],
        level=m["level"] if m["level"] in LEVELS else m["level"],
        file=m["file"],
        line=int(m["line"]),
        func=m["func"],
        msg=m["msg"],
    )


def parse_stream(stream, source, depth):
    """Yield LogRecords. Non-matching lines that follow a parsed record are
    treated as continuations (stack traces, wrapped payloads); leading
    orphans are emitted as unparsed records so nothing is silently dropped."""
    current = None
    for lineno, raw in enumerate(stream, 1):
        raw = raw.rstrip("\n")
        if not raw.strip():
            continue
        rec = parse_json_line(raw) if raw.lstrip().startswith("{") else parse_text_line(raw)
        if rec is None:
            if current is not None:
                current.continuation.append(raw)
            else:
                yield LogRecord(msg=raw, lineno=lineno, source=source,
                                parsed=False, service="unknown",
                                component="unparsed")
            continue
        if current is not None:
            yield current
        rec.lineno = lineno
        rec.source = source
        rec.service = classify_service(rec.logger)
        rec.component = classify_component(rec.file, rec.msg, depth)
        current = rec
    if current is not None:
        yield current


def emit_ndjson(records, out):
    for rec in records:
        d = asdict(rec)
        # keep CLI output free of engine-owned fields when they're unset
        for key in ("continuation", "channel", "fingerprint", "extra"):
            if not d[key]:
                del d[key]
        out.write(json.dumps(d, ensure_ascii=False) + "\n")


def emit_summary(records, out):
    total = 0
    unparsed = 0
    by_service_level = Counter()
    by_component = Counter()
    errors = Counter()
    for rec in records:
        total += 1
        if not rec.parsed:
            unparsed += 1
            continue
        by_service_level[(rec.service, rec.level)] += 1
        by_component[(rec.service, rec.component, rec.level)] += 1
        if rec.level in ("ERROR", "CRITICAL"):
            errors[(rec.service, rec.file, rec.msg[:120])] += 1

    out.write(f"total lines: {total}  (unparsed: {unparsed})\n\n")
    out.write("== records by service / level ==\n")
    for (service, level), n in sorted(by_service_level.items(),
                                      key=lambda x: -x[1]):
        out.write(f"{n:>8}  {service:<24} {level}\n")
    out.write("\n== top components ==\n")
    for (service, comp, level), n in by_component.most_common(25):
        out.write(f"{n:>8}  {service:<24} {level:<6} {comp}\n")
    if errors:
        out.write("\n== distinct errors ==\n")
        for (service, file_, msg), n in errors.most_common(20):
            out.write(f"{n:>6}x [{service}] {file_}: {msg}\n")
