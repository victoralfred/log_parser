"""Automated flare health report.

Cross-references the verdicts a flare already contains — diagnose results,
component health, configuration errors — with the signals computed from the
ingested log records (error fingerprints, lifecycle events, silence gaps)
into one summary a support engineer would otherwise assemble by hand.
"""

import json
import re

import yaml

from logscope.core import analysis

_DIAGNOSE_LINE = re.compile(r"^\s+(PASS|FAIL|WARNING|UNEXPECTED ERROR) (.+)$")
_DIAGNOSE_TOTAL = re.compile(
    r"Total:(\d+), Success:(\d+)(?:, Fail:(\d+))?(?:, Warning:(\d+))?")


def parse_diagnose(text: str) -> dict:
    """Parse diagnose.log: '  PASS|FAIL|WARNING <name>' lines, each followed
    by '  Diagnosis: ...', ending with 'Total:N, Success:N[, Warning:N]'."""
    entries = []
    counts = {"PASS": 0, "FAIL": 0, "WARNING": 0, "UNEXPECTED ERROR": 0}
    current = None
    for line in text.splitlines():
        m = _DIAGNOSE_LINE.match(line)
        if m:
            status, name = m.group(1), m.group(2).strip()
            counts[status] += 1
            current = {"status": status, "name": name, "diagnosis": ""}
            if status != "PASS":
                entries.append(current)
            continue
        stripped = line.strip()
        if stripped.startswith("Diagnosis:") and current is not None:
            current["diagnosis"] = stripped[len("Diagnosis:"):].strip()
            current = None
    total = sum(counts.values())
    m = _DIAGNOSE_TOTAL.search(text)
    if m:  # trust the file's own summary when present
        total = int(m.group(1))
    return {"total": total, "success": counts["PASS"],
            "warning": counts["WARNING"],
            "fail": counts["FAIL"] + counts["UNEXPECTED ERROR"],
            "entries": entries}


def parse_health(yaml_text: str) -> dict:
    try:
        data = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError:
        return {"healthy_count": 0, "unhealthy": []}
    healthy = data.get("healthy") or []
    return {"healthy_count": len(set(healthy)),
            "unhealthy": sorted(set(data.get("unhealthy") or []))}


def parse_config_errors(text: str) -> list[dict]:
    """Entries from the '=== Configuration errors ===' section of
    config-check.log (terminated by the next '===' header)."""
    errors = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("==="):
            if "Configuration errors" in line:
                in_section = True
                continue
            if in_section:
                break
        elif in_section and ":" in line:
            name, _, error = line.partition(":")
            if name.strip():
                errors.append({"name": name.strip(), "error": error.strip()})
    return errors


def _doc_content(store, source: str, path: str) -> str | None:
    row = store.one(
        "SELECT content FROM documents WHERE source = ? AND path = ?",
        (source, path))
    return row["content"] if row else None


def build_report(store, source: str) -> dict | None:
    """Assemble the health report for one ingested flare root."""
    if not store.one("SELECT 1 FROM documents WHERE source = ? LIMIT 1",
                     (source,)):
        return None
    report = {"source": source}

    text = _doc_content(store, source, "diagnose.log")
    report["diagnose"] = parse_diagnose(text) if text else None

    text = _doc_content(store, source, "health.yaml")
    report["health"] = parse_health(text) if text else None

    text = _doc_content(store, source, "config-check.log")
    report["config_errors"] = parse_config_errors(text) if text else []

    # agent identity
    agent = {}
    text = _doc_content(store, source, "metadata/host.json")
    if text:
        try:
            host = json.loads(text)
            agent["version"] = host.get("agentVersion")
            agent["os"] = host.get("os")
            agent["hostname"] = (host.get("meta") or {}).get("hostname")
        except (json.JSONDecodeError, ValueError):
            pass
    text = _doc_content(store, source, "install_info.log")
    if text:
        try:
            info = (yaml.safe_load(text) or {}).get("install_method") or {}
            agent["install_method"] = info.get("tool")
        except yaml.YAMLError:
            pass
    text = _doc_content(store, source, "version-history.json")
    if text:
        try:
            agent["version_history"] = [
                {"version": e.get("version"), "timestamp": e.get("timestamp"),
                 "tool": (e.get("install_method") or {}).get("tool")}
                for e in (json.loads(text).get("entries") or [])]
        except (json.JSONDecodeError, ValueError):
            pass
    report["agent"] = agent

    # log-derived signals, scoped to this flare's log files
    log_source_like = source.rstrip("/") + "/logs/%"
    level_rows = store.query(
        "SELECT level, COUNT(*) AS n FROM records"
        " WHERE source LIKE ? GROUP BY level", (log_source_like,))
    report["log_levels"] = {r["level"]: r["n"] for r in level_rows}
    has_logs = bool(level_rows)

    report["top_errors"] = [
        {"fingerprint": f["fingerprint"], "count": f["count"],
         "worst_level": f["worst_level"], "template": f["template"][:200],
         "services": f["services"]}
        for f in analysis.fingerprints(store, limit=5, min_level="ERROR")
    ] if has_logs else []

    lifecycle = store.query(
        "SELECT json_extract(extra, '$.event') AS event, COUNT(*) AS n"
        " FROM records WHERE channel = 'journald'"
        " AND json_extract(extra, '$.event') IN ('restart','oom','panic')"
        " GROUP BY event")
    report["lifecycle"] = {r["event"]: r["n"] for r in lifecycle}

    gaps = [g for g in analysis.gaps(store, threshold=600)
            if has_logs][:3]
    report["gaps"] = gaps

    # verdict rollup
    problems = ((report["diagnose"] or {}).get("fail", 0)
                + len((report["health"] or {}).get("unhealthy", []))
                + len(report["config_errors"])
                + report["lifecycle"].get("oom", 0)
                + report["lifecycle"].get("panic", 0)
                + sum(1 for f in report["top_errors"]
                      if f["worst_level"] == "CRITICAL"))
    warnings = ((report["diagnose"] or {}).get("warning", 0)
                + sum(1 for f in report["top_errors"]
                      if f["worst_level"] == "ERROR")
                + len(report["gaps"]))
    report["problems"] = problems
    report["warnings"] = warnings
    report["verdict"] = ("problems found" if problems
                         else "needs attention" if warnings else "healthy")
    return report
