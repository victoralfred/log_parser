"""Built-in scanner: Datadog Agent log files (text or JSON format).

Discovers *.log and rotated *.log.N files under the target root and claims
only files whose first non-blank line matches a known agent log format, so
other channels' files (e.g. tracer logs) are left for their own scanners.
"""

from pathlib import Path

from logscope.core.contract import Scanner, ScanTarget, SourceInfo
from logscope.core.datadog_format import (TEXT_LINE_RE, parse_json_line,
                                          parse_stream)


def _sniff(path: Path) -> bool:
    """True if the first non-blank line looks like an agent log line."""
    try:
        with open(path, errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    return False
                line = line.strip()
                if not line:
                    continue
                if line.startswith("{"):
                    return parse_json_line(line) is not None
                return TEXT_LINE_RE.match(line) is not None
    except OSError:
        return False
    return False


class AgentFilesScanner(Scanner):
    name = "agent-files"
    channel = "file"
    description = ("Datadog Agent text/JSON log files "
                   "(agent.log, trace-agent.log, rotations, ...)")

    def discover(self, target: ScanTarget) -> list[SourceInfo]:
        root = Path(target.root).expanduser()
        if not root.is_dir():
            return []
        candidates = sorted(
            p for p in root.rglob("*")
            if p.is_file() and (p.suffix == ".log" or
                                (p.suffixes[:-1] and p.suffixes[-2] == ".log"
                                 and p.suffix[1:].isdigit())))
        return [
            SourceInfo(scanner=self.name, source_id=str(p),
                       label=str(p.relative_to(root)),
                       size_hint=p.stat().st_size)
            for p in candidates if _sniff(p)
        ]

    def scan(self, source: SourceInfo, target: ScanTarget):
        depth = target.options.get("component_depth", 4)
        with open(source.source_id, errors="replace") as f:
            yield from parse_stream(f, source.source_id, depth)
