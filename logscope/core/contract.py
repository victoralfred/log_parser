"""The Scanner contract — the interface every log-channel plugin implements.

A scanner owns one log channel (agent files, journald, container stdout,
tracer logs, ...). It does two things: discover() what sources it can read
under a target, and scan() one source as a lazy stream of LogRecords.

Error-handling rules for plugin authors:
  - Line-level garbage must NOT raise: yield LogRecord(parsed=False, msg=raw)
    instead, so nothing is silently dropped and the UI can show the count.
  - Source-level failure (unreadable file, journalctl exit != 0) raises
    ScannerError; the engine marks that source failed and continues.
  - Any other exception escaping scan() is caught by the engine and reported
    as a failed source in the scanner's status.

scan() MUST be a generator. This is the live-mode seam: a future follow mode
keeps yielding instead of returning, with no contract change.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator

from logscope.core.record import LogRecord


@dataclass(frozen=True)
class ScanTarget:
    """What the user asked to scan: a root location plus optional hints."""

    root: str                      # directory for file scanners; ignored by host scanners
    since: str | None = None       # ISO timestamp lower bound (used by journald)
    options: dict = field(default_factory=dict)  # scanner-specific knobs


@dataclass(frozen=True)
class SourceInfo:
    """One scannable source discovered by a scanner."""

    scanner: str                   # owning scanner name
    source_id: str                 # stable id: file path, "journald:<unit>", ...
    label: str                     # human-readable label for the UI
    size_hint: int | None = None   # bytes or estimated record count, for progress


@dataclass
class Document:
    """A non-record artifact a scanner extracts: config files, metadata
    dumps, diagnostics. Stored in the documents table and browsed in the
    UI's "Flare files" section."""

    path: str                  # relative path within the target, e.g. "etc/datadog.yaml"
    category: str              # "config" | "metadata" | "log-other" | "other"
    format: str                # "yaml" | "json" | "text"
    content: str               # raw text, size-capped by the producer
    source: str = ""           # SourceInfo.source_id of the producing scan
    scrubbed: bool = False     # content contains '****' redaction markers
    truncated: bool = False    # content was cut at the size cap


@dataclass(frozen=True)
class PanelSpec:
    """An optional UI panel a scanner contributes to the dashboard.

    The frontend renders panels generically by kind:
      "table" -> data_url returns {"columns": [...], "rows": [[...], ...]}
      "stat"  -> data_url returns {"stats": [{"label": ..., "value": ...}]}
      "html"  -> data_url returns {"html": "..."}
    """

    panel_id: str
    title: str
    kind: str                      # "table" | "stat" | "html"
    data_url: str                  # e.g. /api/scanners/journald/panels/lifecycle


class ScannerError(Exception):
    """Fatal failure for a whole source or scanner — never for single lines."""


class Scanner(ABC):
    """Base class for log-channel scanners. Subclass, set the metadata class
    attributes, implement discover() and scan(), drop the file in the
    scanners/ directory — the registry picks it up at startup."""

    # --- required metadata (class attributes) ---
    name: str = ""                 # unique scanner id, e.g. "agent-files"
    channel: str = ""              # channel tag stamped on records, e.g. "file"
    description: str = ""

    # --- capability flags ---
    supports_follow: bool = False  # True once the scanner can tail live
    needs_host_access: bool = False  # needs host tools (journalctl, docker, ...)

    @abstractmethod
    def discover(self, target: ScanTarget) -> list[SourceInfo]:
        """Return the sources this scanner can read under `target`.

        Must be cheap and side-effect free. An empty list means "nothing
        applicable here" (not an error). Raise ScannerError only when the
        scanner itself is unusable (e.g. required host tool missing)."""

    @abstractmethod
    def scan(self, source: SourceInfo, target: ScanTarget) -> Iterator[LogRecord]:
        """Stream normalized records from one source. Must be a generator."""

    # --- optional documents facet ---
    def documents(self, target: ScanTarget) -> Iterator[Document]:
        """Optional: yield non-record artifacts (configs, metadata files,
        diagnostics) found under `target`. Default: nothing. Same error
        rules as scan(): per-file problems should be skipped or degraded,
        only source-level failure raises ScannerError."""
        return iter(())

    # --- optional UI contribution ---
    def panels(self) -> list[PanelSpec]:
        return []

    def panel_data(self, panel_id: str, store) -> dict:
        """Return the JSON payload backing one of this scanner's panels.
        `store` is the record store, for querying ingested records."""
        raise KeyError(panel_id)

    def validate(self) -> None:
        """Sanity-check metadata; called by the registry after instantiation."""
        if not self.name:
            raise ScannerError(f"{type(self).__name__}: 'name' must be set")
        if not self.channel:
            raise ScannerError(f"{type(self).__name__}: 'channel' must be set")
