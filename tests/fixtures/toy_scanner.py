"""Test fixture: a minimal valid scanner plugin."""

from logscope.core.contract import Scanner, SourceInfo
from logscope.core.record import LogRecord


class ToyScanner(Scanner):
    name = "toy"
    channel = "toy"
    description = "emits three synthetic records"

    def discover(self, target):
        return [SourceInfo(scanner=self.name, source_id="toy:1", label="toy source")]

    def scan(self, source, target):
        for i in range(3):
            yield LogRecord(time="2026-06-01 00:00:0%d" % i, level="INFO",
                            msg=f"toy record {i}", service="toy-service",
                            component="toy/component", lineno=i + 1,
                            source=source.source_id)
