import json

from logscope.scanners.journald import JournaldScanner, _classify_event

scanner = JournaldScanner()


def _entry(**kw):
    # 1780604840 = 2026-06-04 20:27:20 UTC
    base = {"__REALTIME_TIMESTAMP": "1780604840000000", "PRIORITY": "6",
            "_SYSTEMD_UNIT": "datadog-agent.service", "_PID": "1234",
            "_COMM": "agent", "MESSAGE": "hello"}
    base.update(kw)
    return json.dumps(base)


def test_basic_mapping():
    rec = scanner._map_entry(_entry(), "datadog-agent.service", "journald:datadog-agent.service", 1)
    assert rec.service == "agent"
    assert rec.level == "INFO"
    assert rec.msg == "hello"
    assert rec.extra["unit"] == "datadog-agent.service"
    assert rec.extra["pid"] == "1234"
    assert rec.time.startswith("2026-06-04")


def test_priority_mapping():
    rec = scanner._map_entry(_entry(PRIORITY="3"), "datadog-agent.service", "x", 1)
    assert rec.level == "ERROR"
    rec = scanner._map_entry(_entry(PRIORITY="2"), "datadog-agent.service", "x", 1)
    assert rec.level == "CRITICAL"


def test_agent_format_message_reparsed():
    msg = ("2026-06-04 20:27:20 CEST | CORE | WARN | "
           "(pkg/collector/corechecks/system/disk/diskv2/disk.go:691 in getPartitionUsage) | "
           "Unable to get disk metrics")
    rec = scanner._map_entry(_entry(MESSAGE=msg), "datadog-agent.service", "x", 1)
    assert rec.level == "WARN"
    assert rec.component == "pkg/collector/corechecks/system"
    assert rec.file.endswith("disk.go")


def test_lifecycle_events():
    assert _classify_event("panic: runtime error: index out of range") == "panic"
    assert _classify_event("Out of memory: Killed process 1234 (agent)") == "oom"
    assert _classify_event("datadog-agent.service: Main process exited, code=killed") == "restart"
    assert _classify_event("Started Datadog Agent.") == "lifecycle"
    assert _classify_event("just a normal log line") is None
    rec = scanner._map_entry(
        _entry(MESSAGE="Scheduled restart job, restart counter is at 5.",
               _COMM="systemd", PRIORITY="6"),
        "datadog-agent.service", "x", 1)
    assert rec.extra["event"] == "restart"


def test_non_json_line_is_unparsed_record():
    rec = scanner._map_entry("not json at all", "datadog-agent.service", "x", 7)
    assert rec.parsed is False
    assert rec.lineno == 7


def test_bytearray_message_decoded():
    rec = scanner._map_entry(
        _entry(MESSAGE=[104, 105]), "datadog-agent.service", "x", 1)
    assert rec.msg == "hi"
