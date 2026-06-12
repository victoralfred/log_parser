"""SQLite-backed record store and the scan engine.

The store owns the ingest pipeline: every record yielded by a scanner is
post-processed here (channel stamped, fingerprint computed, epoch timestamp
derived) before being written in batches. Re-scanning a source replaces its
previous records, so scans are idempotent.
"""

import functools
import json
import re
import sqlite3
import threading
import time as time_mod
from datetime import datetime, timezone

from logscope.core.contract import ScannerError, ScanTarget
from logscope.core.fingerprint import fingerprint, template

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    ts_epoch REAL, time TEXT, level TEXT, service TEXT, component TEXT,
    channel TEXT, scanner TEXT, source TEXT, lineno INT,
    logger TEXT, file TEXT, line INT, func TEXT, msg TEXT,
    fingerprint TEXT, parsed INT, continuation TEXT, extra TEXT
);
CREATE INDEX IF NOT EXISTS ix_records_main ON records(service, level, ts_epoch);
CREATE INDEX IF NOT EXISTS ix_records_fp ON records(fingerprint);
CREATE INDEX IF NOT EXISTS ix_records_source ON records(source);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source TEXT, path TEXT, category TEXT, format TEXT,
    scrubbed INT, truncated INT, size INT, content TEXT,
    UNIQUE(source, path)
);
CREATE INDEX IF NOT EXISTS ix_documents_cat ON documents(category);
CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY,
    started REAL, finished REAL, status TEXT, error TEXT,
    scanners TEXT, root TEXT, record_count INT DEFAULT 0,
    sources TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS uploads (
    hash TEXT PRIMARY KEY, root TEXT, filename TEXT, created REAL,
    files INT, bytes INT
);
"""

_TZ_SUFFIX = re.compile(r"\s+[A-Z]{2,5}$")  # "CEST", "UTC" — abbreviations are ambiguous; strip


@functools.lru_cache(maxsize=64)
def _compile(pattern: str):
    # case-insensitive by default; inline flags like (?-i:...) can override
    return re.compile(pattern, re.IGNORECASE)


def _regexp(pattern, value) -> bool:
    """Backs the SQL `msg REGEXP ?` operator (SQLite has none built in)."""
    if value is None:
        return False
    return _compile(pattern).search(value) is not None


def parse_epoch(time_str: str) -> float | None:
    """Best-effort epoch seconds from the timestamp strings scanners emit."""
    if not time_str:
        return None
    s = time_str.strip()
    s = _TZ_SUFFIX.sub("", s)
    s = s.replace("T", " ").replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    return None


class Store:
    def __init__(self, path="logscope.db"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("regexp", 2, _regexp)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self.fts_enabled = self._init_fts()
        self._housekeep()

    def _init_fts(self) -> bool:
        """Full-text index on msg (external content + sync triggers), so the
        q= filter is instant at scale. Falls back to LIKE when the SQLite
        build lacks FTS5."""
        try:
            with self._lock:
                self._conn.executescript("""
CREATE VIRTUAL TABLE IF NOT EXISTS records_fts
    USING fts5(msg, content='records', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS records_ai AFTER INSERT ON records BEGIN
    INSERT INTO records_fts(rowid, msg) VALUES (new.id, new.msg);
END;
CREATE TRIGGER IF NOT EXISTS records_ad AFTER DELETE ON records BEGIN
    INSERT INTO records_fts(records_fts, rowid, msg)
        VALUES ('delete', old.id, old.msg);
END;
""")
                # one-time backfill for dbs created before FTS existed
                n_fts = self._conn.execute(
                    "SELECT COUNT(*) FROM records_fts").fetchone()[0]
                n_rec = self._conn.execute(
                    "SELECT COUNT(*) FROM records").fetchone()[0]
                if n_fts == 0 and n_rec > 0:
                    self._conn.execute(
                        "INSERT INTO records_fts(rowid, msg)"
                        " SELECT id, msg FROM records")
                self._conn.commit()
            return True
        except sqlite3.OperationalError:
            return False

    def _housekeep(self):
        """Startup hygiene: cap scan-run history, fail runs interrupted by
        a previous shutdown."""
        with self._lock:
            self._conn.execute(
                "UPDATE scan_runs SET status='failed',"
                " error='interrupted by restart' WHERE status='running'")
            self._conn.execute(
                "DELETE FROM scan_runs WHERE id NOT IN"
                " (SELECT id FROM scan_runs ORDER BY id DESC LIMIT 50)")
            self._conn.commit()

    # --- ingest -----------------------------------------------------------

    def replace_source(self, source_id: str):
        with self._lock:
            self._conn.execute("DELETE FROM records WHERE source = ?", (source_id,))
            self._conn.commit()

    def ingest(self, records, scanner_name: str, channel: str,
               batch_size: int = 1000,
               levels: set[str] | None = None) -> tuple[int, int]:
        """Post-process and write a record stream; returns (written, skipped).

        When `levels` is given, records at other levels are skipped to save
        storage — except records with no level at all (unparsed lines,
        journald entries without a priority), which are always kept."""
        batch, total, skipped = [], 0, 0
        for rec in records:
            if levels and rec.level and rec.level not in levels:
                skipped += 1
                continue
            if not rec.channel:
                rec.channel = channel
            if not rec.fingerprint:
                rec.fingerprint = fingerprint(rec.msg)
            batch.append((
                parse_epoch(rec.time), rec.time, rec.level, rec.service,
                rec.component, rec.channel, scanner_name, rec.source,
                rec.lineno, rec.logger, rec.file, rec.line, rec.func, rec.msg,
                rec.fingerprint, int(rec.parsed),
                json.dumps(rec.continuation) if rec.continuation else "[]",
                json.dumps(rec.extra) if rec.extra else "{}",
            ))
            if len(batch) >= batch_size:
                total += self._flush(batch)
                batch = []
        total += self._flush(batch)
        return total, skipped

    def _flush(self, batch) -> int:
        if not batch:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO records (ts_epoch, time, level, service, component,"
                " channel, scanner, source, lineno, logger, file, line, func,"
                " msg, fingerprint, parsed, continuation, extra)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            self._conn.commit()
        return len(batch)

    def ingest_documents(self, docs, source_id: str) -> int:
        """Replace a source's documents with a fresh set; returns count."""
        rows = [(doc.source or source_id, doc.path, doc.category, doc.format,
                 int(doc.scrubbed), int(doc.truncated), len(doc.content),
                 doc.content) for doc in docs]
        if not rows:
            # don't wipe another scanner's documents for the same target
            return 0
        # replace by the sources actually present in this batch — documents
        # may carry their own source (e.g. a flare root nested under the
        # target), and re-scans must overwrite those, not the bare target
        sources = {row[0] for row in rows} | {source_id}
        with self._lock:
            self._conn.execute(
                f"DELETE FROM documents WHERE source IN"
                f" ({','.join('?' * len(sources))})", tuple(sources))
            self._conn.executemany(
                "INSERT INTO documents (source, path, category, format,"
                " scrubbed, truncated, size, content)"
                " VALUES (?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()
        return len(rows)

    # --- queries ----------------------------------------------------------

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params)]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params=()):
        """Run a single committing write statement."""
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    # --- scan runs --------------------------------------------------------

    def create_run(self, scanners: list[str], root: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO scan_runs (started, status, scanners, root)"
                " VALUES (?, 'running', ?, ?)",
                (time_mod.time(), json.dumps(scanners), root))
            self._conn.commit()
            return cur.lastrowid

    def update_run(self, run_id: int, **fields):
        sets, params = [], []
        for key, value in fields.items():
            sets.append(f"{key} = ?")
            params.append(json.dumps(value) if isinstance(value, (list, dict)) else value)
        with self._lock:
            self._conn.execute(
                f"UPDATE scan_runs SET {', '.join(sets)} WHERE id = ?",
                (*params, run_id))
            self._conn.commit()

    def get_run(self, run_id: int) -> dict | None:
        row = self.one("SELECT * FROM scan_runs WHERE id = ?", (run_id,))
        if row:
            row["scanners"] = json.loads(row["scanners"])
            row["sources"] = json.loads(row["sources"])
        return row


class ScanBusyError(Exception):
    """Another scan is currently running; concurrent scans are serialized."""


_active_scan_lock = threading.Lock()
_active_run_id: int | None = None


def execute_scan(registry, store: Store, target: ScanTarget,
                 scanner_names: list[str] | None = None,
                 levels: list[str] | None = None) -> int:
    """Run discover+scan for the selected scanners in a background thread.
    Returns the run id immediately; progress is tracked in scan_runs.
    `levels` restricts which record levels get stored (None = all).
    Raises ScanBusyError if a scan is already in flight."""
    global _active_run_id
    names = scanner_names or [s.name for s in registry.scanners()]
    level_set = set(levels) if levels else None
    with _active_scan_lock:
        if _active_run_id is not None:
            raise ScanBusyError(f"scan {_active_run_id} is already running")
        run_id = store.create_run(names, target.root)
        _active_run_id = run_id
    thread = threading.Thread(
        target=_scan_worker,
        args=(registry, store, target, names, run_id, level_set),
        daemon=True)
    thread.start()
    return run_id


def _scan_worker(registry, store, target, names, run_id, levels=None):
    try:
        _scan_worker_inner(registry, store, target, names, run_id, levels)
    finally:
        global _active_run_id
        with _active_scan_lock:
            _active_run_id = None


def _scan_worker_inner(registry, store, target, names, run_id, levels=None):
    sources_status = []
    total = 0
    try:
        for name in names:
            try:
                scanner = registry.get(name)
            except KeyError:
                sources_status.append({"scanner": name, "source": "",
                                       "status": "failed",
                                       "error": "unknown scanner"})
                continue
            try:
                sources = scanner.discover(target)
            except ScannerError as exc:
                sources_status.append({"scanner": name, "source": "",
                                       "status": "failed", "error": str(exc)})
                store.update_run(run_id, sources=sources_status)
                continue
            for src in sources:
                entry = {"scanner": name, "source": src.source_id,
                         "label": src.label, "status": "running",
                         "records": 0, "skipped": 0, "error": None}
                sources_status.append(entry)
                store.update_run(run_id, sources=sources_status)
                try:
                    store.replace_source(src.source_id)
                    count, skipped = store.ingest(scanner.scan(src, target),
                                                  name, scanner.channel,
                                                  levels=levels)
                    entry["status"] = "done"
                    entry["records"] = count
                    entry["skipped"] = skipped
                    total += count
                except Exception as exc:  # ScannerError or anything else
                    entry["status"] = "failed"
                    entry["error"] = str(exc)
                store.update_run(run_id, sources=sources_status,
                                 record_count=total)
            # documents facet: once per scanner over the whole target
            try:
                n_docs = store.ingest_documents(scanner.documents(target),
                                                str(target.root))
                if n_docs:
                    sources_status.append(
                        {"scanner": name, "source": str(target.root),
                         "label": f"{name}: documents", "status": "done",
                         "records": 0, "skipped": 0, "documents": n_docs,
                         "error": None})
                    store.update_run(run_id, sources=sources_status)
            except Exception as exc:
                sources_status.append(
                    {"scanner": name, "source": str(target.root),
                     "label": f"{name}: documents", "status": "failed",
                     "records": 0, "skipped": 0, "documents": 0,
                     "error": str(exc)})
                store.update_run(run_id, sources=sources_status)
        store.update_run(run_id, status="done", finished=time_mod.time(),
                         sources=sources_status, record_count=total)
    except Exception as exc:
        store.update_run(run_id, status="failed", error=str(exc),
                         finished=time_mod.time())
