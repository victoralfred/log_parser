"""Built-in scanner: journald entries for Datadog systemd units.

Captures what never reaches the agent log files: Go panics written to
stderr, OOM kills, systemd restart loops, and startup failures from before
the agent's logging subsystem initializes.

Read-only: shells out to `journalctl -o json`. When an entry's MESSAGE is
itself an agent-format log line (agents mirror their template to stdout),
it is re-parsed through datadog_format to recover level/file/component.
"""

import json
import re
import shutil
import subprocess
from datetime import datetime, timezone

from logscope.core.contract import (PanelSpec, Scanner, ScannerError,
                                    ScanTarget, SourceInfo)
from logscope.core.datadog_format import parse_text_line
from logscope.core.record import LogRecord

# journald PRIORITY (syslog levels) -> agent-style level
_PRIORITY_LEVEL = {0: "CRITICAL", 1: "CRITICAL", 2: "CRITICAL", 3: "ERROR",
                   4: "WARN", 5: "INFO", 6: "INFO", 7: "DEBUG"}

_LIFECYCLE_PATTERNS = [
    (re.compile(r"oom-kill|Out of memory|Killed process"), "oom"),
    (re.compile(r"^panic:|runtime error:|fatal error:"), "panic"),
    (re.compile(r"Main process exited|Failed with result|"
                r"Scheduled restart job|Start request repeated too quickly"), "restart"),
    (re.compile(r"^(Started|Stopped|Stopping) "), "lifecycle"),
]


def _classify_event(msg: str) -> str | None:
    for pattern, event in _LIFECYCLE_PATTERNS:
        if pattern.search(msg):
            return event
    return None


class JournaldScanner(Scanner):
    name = "journald"
    channel = "journald"
    description = ("systemd journal for datadog* units — crashes, OOM kills, "
                   "restarts, startup failures")
    needs_host_access = True

    UNIT_PATTERN = "datadog*"

    def discover(self, target: ScanTarget) -> list[SourceInfo]:
        if shutil.which("journalctl") is None:
            raise ScannerError("journalctl not found on PATH")
        try:
            out = subprocess.run(
                ["journalctl", "--field", "_SYSTEMD_UNIT", "--no-pager"],
                capture_output=True, text=True, timeout=30)
        except (subprocess.SubprocessError, OSError) as exc:
            raise ScannerError(f"journalctl failed: {exc}")
        if out.returncode != 0:
            raise ScannerError(f"journalctl failed: {out.stderr.strip()[:200]}")
        units = sorted(u for u in out.stdout.split()
                       if u.startswith("datadog"))
        return [SourceInfo(scanner=self.name,
                           source_id=f"journald:{unit}", label=unit)
                for unit in units]

    def scan(self, source: SourceInfo, target: ScanTarget):
        unit = source.source_id.split(":", 1)[1]
        cmd = ["journalctl", "-u", unit, "-o", "json", "--no-pager"]
        if target.since:
            cmd += ["--since", target.since]
        # Lazy line iteration over Popen stdout is the live-mode seam:
        # follow mode later just appends -f to the same command.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                errors="replace")
        emitted = 0
        try:
            for lineno, raw in enumerate(proc.stdout, 1):
                raw = raw.strip()
                if not raw:
                    continue
                rec = self._map_entry(raw, unit, source.source_id, lineno)
                emitted += 1
                yield rec
        finally:
            proc.stdout.close()
            stderr = proc.stderr.read()
            proc.stderr.close()
            code = proc.wait()
        if code != 0 and emitted == 0:
            raise ScannerError(f"journalctl exited {code}: {stderr.strip()[:200]}")

    def _map_entry(self, raw: str, unit: str, source_id: str,
                   lineno: int) -> LogRecord:
        try:
            entry = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return LogRecord(msg=raw, lineno=lineno, source=source_id,
                             parsed=False, service="unknown",
                             component="unparsed")
        msg = entry.get("MESSAGE") or ""
        if isinstance(msg, list):  # journald encodes non-utf8 messages as byte arrays
            msg = bytes(b for b in msg if isinstance(b, int)).decode("utf-8", "replace")

        usec = entry.get("__REALTIME_TIMESTAMP")
        time_str = ""
        if usec:
            dt = datetime.fromtimestamp(int(usec) / 1e6, tz=timezone.utc)
            time_str = dt.strftime("%Y-%m-%d %H:%M:%S%z")

        service = unit.removeprefix("datadog-").removesuffix(".service") or unit
        try:
            level = _PRIORITY_LEVEL.get(int(entry.get("PRIORITY", -1)), "")
        except (TypeError, ValueError):
            level = ""

        extra = {"unit": unit}
        for src_key, dst_key in (("_PID", "pid"), ("_COMM", "comm"),
                                 ("SYSLOG_IDENTIFIER", "ident")):
            if entry.get(src_key):
                extra[dst_key] = entry[src_key]
        event = _classify_event(msg)
        if event:
            extra["event"] = event

        # If the message is itself an agent-format line, recover the richer
        # classification (true level, caller file, component, service).
        inner = parse_text_line(msg)
        if inner is not None:
            from logscope.core.datadog_format import (classify_component,
                                                      classify_service)
            inner.lineno = lineno
            inner.source = source_id
            inner.service = classify_service(inner.logger)
            inner.component = classify_component(inner.file, inner.msg, 4)
            inner.extra = extra
            return inner

        component = "systemd" if extra.get("comm") in ("systemd", None) else "stderr"
        return LogRecord(time=time_str, level=level, msg=msg, service=service,
                         component=component, lineno=lineno, source=source_id,
                         extra=extra)

    # --- UI contribution ---

    def panels(self) -> list[PanelSpec]:
        return [PanelSpec(
            panel_id="lifecycle",
            title="Restarts / OOM / Panics",
            kind="table",
            data_url=f"/api/scanners/{self.name}/panels/lifecycle")]

    def panel_data(self, panel_id: str, store) -> dict:
        if panel_id != "lifecycle":
            raise KeyError(panel_id)
        rows = store.query(
            "SELECT time, service, json_extract(extra, '$.event') AS event,"
            " msg FROM records WHERE channel = 'journald'"
            " AND json_extract(extra, '$.event') IN"
            " ('oom', 'panic', 'restart', 'lifecycle')"
            " ORDER BY ts_epoch DESC LIMIT 100")
        return {"columns": ["time", "service", "event", "message"],
                "rows": [[r["time"], r["service"], r["event"],
                          (r["msg"] or "")[:160]] for r in rows]}
