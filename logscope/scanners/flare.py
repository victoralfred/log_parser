"""Built-in scanner: Datadog Agent flare archives (extracted).

A flare bundles configurations (etc/, runtime config dumps), logs (logs/),
and metadata (metadata/, expvar/, health, inventory, diagnostics). The log
files are ingested as records by the agent-files scanner; this scanner
extracts everything else as Documents, categorized for the UI's
Configurations / Metadata / Other-logs tabs.
"""

import json
from pathlib import Path

from logscope.core.contract import Document, Scanner, ScanTarget, SourceInfo

MAX_DOC_BYTES = 2 * 1024 * 1024

# Markers used to recognize an extracted flare directory.
_FLARE_MARKERS = ("flare_creation.log", "runtime_config_dump.yaml",
                  "metadata", "etc", "logs", "expvar")

# Ordered (match, category) rules; first hit wins. A rule matches when the
# relative path equals it, starts with it + "/", or — for "*name" rules —
# ends with the suffix.
_CATEGORY_RULES = [
    ("etc", "config"),
    ("*runtime_config_dump.yaml", "config"),
    ("envvars.log", "config"),
    ("secrets.log", "config"),
    ("config-check.log", "config"),
    ("metadata", "metadata"),
    ("expvar", "metadata"),
    ("sbom", "metadata"),
    ("connectivity", "metadata"),
    ("health.yaml", "metadata"),
    ("version-history.json", "metadata"),
    ("install_info.log", "metadata"),
    ("tagger-list.json", "metadata"),
    ("registry.json", "metadata"),
    ("status.log", "metadata"),
    ("diagnose.log", "metadata"),
    ("permissions.log", "metadata"),
    ("flare_creation.log", "metadata"),
    ("docker_ps.log", "metadata"),
    ("workload-list.log", "metadata"),
    ("workload-filter.log", "metadata"),
    ("runtime_debug_info.log", "metadata"),
    ("remote-config-state.log", "metadata"),
    ("agent_open_files.txt", "metadata"),
    ("non_scrubbed_files.json", "metadata"),
    ("version info", "metadata"),
    ("health", "metadata"),
]


def _categorize(rel_path: str) -> str | None:
    """Category for a flare member, or None to skip it entirely."""
    if rel_path.startswith("logs/"):
        return None                      # ingested as records by agent-files
    if rel_path.endswith((".db", ".sqlite")):
        return None                      # binary
    for rule, category in _CATEGORY_RULES:
        if rule.startswith("*"):
            if rel_path.endswith(rule[1:]):
                return category
        elif rel_path == rule or rel_path.startswith(rule + "/"):
            return category
    if rel_path.endswith((".log", ".txt")):
        return "log-other"               # otel, host-profiler, goroutine dumps...
    return "other"


def _detect_format(rel_path: str, content: str) -> str:
    if rel_path.endswith((".json",)):
        return "json"
    if rel_path.endswith((".yaml", ".yml")):
        return "yaml"
    head = content.lstrip()[:1]
    if head in ("{", "["):
        try:
            json.loads(content)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass
    return "text"


class FlareScanner(Scanner):
    name = "flare"
    channel = "flare"
    description = ("Datadog Agent flare (extracted): configurations, "
                   "metadata, and diagnostics as browsable documents")

    def discover(self, target: ScanTarget) -> list[SourceInfo]:
        root = Path(target.root).expanduser()
        if not root.is_dir():
            return []
        hits = sum(1 for marker in _FLARE_MARKERS if (root / marker).exists())
        if hits < 2:
            return []
        return [SourceInfo(scanner=self.name, source_id=str(root),
                           label=f"flare: {root.name}")]

    def scan(self, source: SourceInfo, target: ScanTarget):
        # Log records come from the agent-files scanner; nothing to add here.
        return iter(())

    def documents(self, target: ScanTarget):
        root = Path(target.root).expanduser()
        if not self.discover(target):
            return
        source_id = str(root)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            category = _categorize(rel)
            if category is None:
                continue
            try:
                size = path.stat().st_size
                if size == 0:
                    continue
                with open(path, errors="replace") as f:
                    content = f.read(MAX_DOC_BYTES + 1)
            except OSError:
                continue
            truncated = len(content) > MAX_DOC_BYTES
            if truncated:
                content = content[:MAX_DOC_BYTES]
            yield Document(
                path=rel, category=category,
                format=_detect_format(rel, content), content=content,
                source=source_id, scrubbed="****" in content,
                truncated=truncated)
