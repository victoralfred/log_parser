import io

from logscope.core.datadog_format import (classify_component, classify_service,
                                          parse_stream, parse_text_line)

TEXT_LINE = ("2026-06-04 20:27:20 CEST | CORE | WARN | "
             "(pkg/collector/corechecks/system/disk/diskv2/disk.go:691 in getPartitionUsage) | "
             "Unable to get disk metrics for /run/user/1000/doc: permission denied")


def test_parse_text_line():
    rec = parse_text_line(TEXT_LINE)
    assert rec.logger == "CORE"
    assert rec.level == "WARN"
    assert rec.file == "pkg/collector/corechecks/system/disk/diskv2/disk.go"
    assert rec.line == 691
    assert rec.func == "getPartitionUsage"
    assert rec.msg.startswith("Unable to get disk metrics")


def test_message_with_pipes_not_split():
    line = ("2026-06-04 20:27:20 CEST | CORE | ERROR | (pkg/x/y.go:1 in f) | "
            "check:redisdb | Error running check: foo | bar")
    rec = parse_text_line(line)
    assert rec.msg == "check:redisdb | Error running check: foo | bar"


def test_suffixed_logger_names():
    assert classify_service("TRACE-LOADER") == "trace-agent"
    assert classify_service("SYS-PROBE-LITE") == "system-probe"
    assert classify_service("INSTALLER") == "installer"
    assert classify_service("SOMETHING-NEW") == "something-new"


def test_classify_component_rust_and_fallbacks():
    assert classify_component(
        "pkg/discovery/module/rust/src/main.rs", "", 4) == "pkg/discovery/module/rust"
    assert classify_component("value.go", "", 4) == "external/value.go"
    assert classify_component("pkg/util/log/log.go", "Set('proxy.no_proxy'): x", 4) == \
        "config/runtime-settings"
    assert classify_component("pkg/util/log/log.go", "anything else", 4) == "unattributed"


def test_stream_continuation_and_orphans():
    text = ("orphan garbage\n"
            "2026-06-04 20:27:20 CEST | TRACE | ERROR | (pkg/trace/api/api.go:10 in serve) | boom\n"
            "  stack frame 1\n"
            "  stack frame 2\n"
            "2026-06-04 20:27:21 CEST | TRACE | INFO | (pkg/trace/api/api.go:11 in serve) | ok\n")
    recs = list(parse_stream(io.StringIO(text), "test", 4))
    assert len(recs) == 3
    assert recs[0].parsed is False and recs[0].msg == "orphan garbage"
    assert recs[1].continuation == ["  stack frame 1", "  stack frame 2"]
    assert recs[2].msg == "ok" and recs[2].continuation == []


def test_json_format_line():
    text = ('{"agent":"core","time":"2026-06-04T20:27:20Z","level":"WARN",'
            '"file":"pkg/collector/foo/check.go","line":42,"func":"run","msg":"hello"}\n')
    recs = list(parse_stream(io.StringIO(text), "test", 4))
    assert len(recs) == 1
    assert recs[0].service == "agent"
    assert recs[0].component == "pkg/collector/foo"
    assert recs[0].level == "WARN"
