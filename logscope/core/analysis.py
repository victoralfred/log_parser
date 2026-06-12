"""Aggregate queries over the record store — the SQL behind the API.

Plain functions taking a Store, so they are testable without HTTP.
"""

import re

from logscope.core.fingerprint import template

LEVEL_RANK = {"TRACE": 0, "DEBUG": 1, "INFO": 2, "WARN": 3,
              "ERROR": 4, "CRITICAL": 5}


def compile_or_raise(pattern: str):
    """Validate a user-supplied regex eagerly; raises re.error."""
    return re.compile(pattern, re.IGNORECASE)


_FTS_SAFE = re.compile(r"[\w.-]+")


def _fts_query(q: str) -> str | None:
    """Build an FTS5 prefix-token query from a plain search string, or None
    when the string needs LIKE semantics (special characters etc.)."""
    tokens = q.split()
    if not tokens or any(_FTS_SAFE.fullmatch(t) is None for t in tokens):
        return None
    return " ".join(f'"{t}"*' for t in tokens)


def _filters(service=None, level=None, component=None, fp=None, channel=None,
             source=None, q=None, regex=None, since=None, until=None,
             parsed=None, fts=False):
    where, params = [], []
    if service:
        where.append("service = ?"); params.append(service)
    if level:
        # "INFO" or comma-separated "INFO,DEBUG"
        levels = [l for l in str(level).split(",") if l]
        if len(levels) == 1:
            where.append("level = ?"); params.append(levels[0])
        else:
            where.append(f"level IN ({','.join('?' * len(levels))})")
            params.extend(levels)
    if component:
        where.append("component = ?"); params.append(component)
    if fp:
        where.append("fingerprint = ?"); params.append(fp)
    if channel:
        where.append("channel = ?"); params.append(channel)
    if source:
        where.append("source = ?"); params.append(source)
    if q:
        match = _fts_query(q) if fts else None
        if match:
            where.append("id IN (SELECT rowid FROM records_fts"
                         " WHERE records_fts MATCH ?)")
            params.append(match)
        else:
            where.append("msg LIKE ?"); params.append(f"%{q}%")
    if regex:
        compile_or_raise(regex)  # reject bad patterns before they hit SQL
        where.append("msg REGEXP ?"); params.append(regex)
    if since is not None:
        where.append("ts_epoch >= ?"); params.append(since)
    if until is not None:
        where.append("ts_epoch <= ?"); params.append(until)
    if parsed is not None:
        where.append("parsed = ?"); params.append(int(parsed))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def records(store, limit=200, offset=0, order="desc", **filters):
    clause, params = _filters(fts=getattr(store, "fts_enabled", False),
                              **filters)
    direction = "DESC" if order != "asc" else "ASC"
    return store.query(
        f"SELECT * FROM records{clause}"
        f" ORDER BY ts_epoch {direction}, id {direction} LIMIT ? OFFSET ?",
        (*params, limit, offset))


def record_count(store, **filters):
    clause, params = _filters(fts=getattr(store, "fts_enabled", False),
                              **filters)
    return store.one(f"SELECT COUNT(*) AS n FROM records{clause}", params)["n"]


def summary(store):
    total = store.one("SELECT COUNT(*) AS n FROM records")["n"]
    unparsed = store.one(
        "SELECT COUNT(*) AS n FROM records WHERE parsed = 0")["n"]
    matrix = store.query(
        "SELECT service, level, COUNT(*) AS n FROM records WHERE parsed = 1"
        " GROUP BY service, level")
    components = store.query(
        "SELECT service, component, level, COUNT(*) AS n FROM records"
        " WHERE parsed = 1 GROUP BY service, component, level"
        " ORDER BY n DESC LIMIT 30")
    channels = store.query(
        "SELECT channel, COUNT(*) AS n FROM records GROUP BY channel")
    span = store.one(
        "SELECT MIN(ts_epoch) AS first_ts, MAX(ts_epoch) AS last_ts"
        " FROM records WHERE ts_epoch IS NOT NULL")
    return {"total": total, "unparsed": unparsed, "matrix": matrix,
            "top_components": components, "channels": channels,
            "first_ts": span["first_ts"] if span else None,
            "last_ts": span["last_ts"] if span else None}


def fingerprints(store, limit=50, level=None, service=None, min_level=None):
    clause, params = _filters(service=service, level=level)
    rows = store.query(
        f"SELECT fingerprint, COUNT(*) AS count, MIN(ts_epoch) AS first_ts,"
        f" MAX(ts_epoch) AS last_ts, MAX(msg) AS sample_msg,"
        f" GROUP_CONCAT(DISTINCT level) AS levels,"
        f" GROUP_CONCAT(DISTINCT service) AS services"
        f" FROM records{clause or ' WHERE 1=1'} AND parsed = 1"
        f" GROUP BY fingerprint ORDER BY count DESC LIMIT ?",
        (*params, limit * 3 if min_level else limit))
    out = []
    for row in rows:
        levels = (row["levels"] or "").split(",")
        worst = max(levels, key=lambda l: LEVEL_RANK.get(l, -1), default="")
        if min_level and LEVEL_RANK.get(worst, -1) < LEVEL_RANK.get(min_level, 0):
            continue
        row["worst_level"] = worst
        row["template"] = template(row["sample_msg"] or "")
        out.append(row)
        if len(out) >= limit:
            break
    return out


def timeline(store, bucket=60, **filters):
    clause, params = _filters(**filters)
    base = f"FROM records{clause or ' WHERE 1=1'} AND ts_epoch IS NOT NULL"
    return store.query(
        f"SELECT CAST(ts_epoch / ? AS INT) * ? AS bucket_ts,"
        f" level, COUNT(*) AS n {base}"
        f" GROUP BY bucket_ts, level ORDER BY bucket_ts",
        (bucket, bucket, *params))


FLATTEN_CAP = 5000


def flatten(obj, prefix=""):
    """Flatten nested dicts/lists into dot-notation (key, value) pairs, so
    config files become a searchable variables table.
    {"apm_config": {"enabled": true}} -> [("apm_config.enabled", "true")]"""
    pairs = []

    def walk(node, path):
        if len(pairs) >= FLATTEN_CAP:
            return
        if isinstance(node, dict):
            if not node:
                pairs.append((path, "{}"))
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            if not node:
                pairs.append((path, "[]"))
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        else:
            pairs.append((path, "null" if node is None else
                          str(node).lower() if isinstance(node, bool) else
                          str(node)))

    walk(obj, prefix)
    return pairs[:FLATTEN_CAP]


def gaps(store, threshold=300.0):
    """Per service, intervals longer than `threshold` seconds with no records
    — candidate crashes, hangs, or rotation losses."""
    out = []
    services = store.query(
        "SELECT DISTINCT service FROM records"
        " WHERE parsed = 1 AND ts_epoch IS NOT NULL")
    for row in services:
        service = row["service"]
        ts_rows = store.query(
            "SELECT DISTINCT ts_epoch FROM records"
            " WHERE service = ? AND ts_epoch IS NOT NULL ORDER BY ts_epoch",
            (service,))
        prev = None
        for r in ts_rows:
            ts = r["ts_epoch"]
            if prev is not None and ts - prev > threshold:
                out.append({"service": service, "from_ts": prev, "to_ts": ts,
                            "duration": ts - prev})
            prev = ts
    out.sort(key=lambda g: -g["duration"])
    return out
