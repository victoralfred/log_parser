"""Flare health report: parsers and the assembled report API."""

import pytest
from fastapi.testclient import TestClient

from logscope.core.flare_report import (parse_config_errors, parse_diagnose,
                                        parse_health)
from logscope.web.app import create_app
from tests.test_api import wait_for_run
from tests.test_flare import make_flare

# verbatim shape from the real flare's diagnose.log
DIAGNOSE_TEXT = """=== Starting diagnose ===
==============
Suite: connectivity-datadog-autodiscovery
1. --------------
  PASS Docker availability
  Diagnosis: Successfully connected to Docker availability environment

2. --------------
  WARNING port check
  Diagnosis: Required port 5012 is already in use

3. --------------
  FAIL Datadog intake connectivity
  Diagnosis: Post "https://intake.example.com": connection refused

-------------------------
  Total:3, Success:1, Warning:1"""


def test_parse_diagnose():
    d = parse_diagnose(DIAGNOSE_TEXT)
    assert d["total"] == 3
    assert d["success"] == 1
    assert d["warning"] == 1
    assert d["fail"] == 1
    by_status = {e["status"]: e for e in d["entries"]}
    assert "PASS" not in by_status
    assert by_status["FAIL"]["name"] == "Datadog intake connectivity"
    assert "connection refused" in by_status["FAIL"]["diagnosis"]
    assert by_status["WARNING"]["diagnosis"].startswith("Required port")


def test_parse_health():
    h = parse_health("healthy:\n  - forwarder\n  - forwarder\n  - aggregator\n"
                     "unhealthy:\n  - tagger-store\n")
    assert h["healthy_count"] == 2          # deduped
    assert h["unhealthy"] == ["tagger-store"]
    assert parse_health(": bad yaml :")["unhealthy"] == []


def test_parse_config_errors():
    text = ("=== Configuration errors ===\n\n"
            "mcp_server: Configuration file contains no valid instances\n\n"
            "=== container check ===\nfine\n")
    errors = parse_config_errors(text)
    assert errors == [{"name": "mcp_server",
                       "error": "Configuration file contains no valid instances"}]


@pytest.fixture()
def problem_flare(tmp_path):
    """Synthetic flare with one FAIL, one unhealthy component, one config
    error — the report must say 'problems found'."""
    root = make_flare(tmp_path / "sickflare")
    (root / "diagnose.log").write_text(DIAGNOSE_TEXT)
    (root / "health.yaml").write_text(
        "healthy:\n  - forwarder\nunhealthy:\n  - tagger-store\n")
    (root / "config-check.log").write_text(
        "=== Configuration errors ===\n\nmcp_server: no valid instances\n\n"
        "=== container check ===\n")
    (root / "version-history.json").write_text(
        '{"entries":[{"version":"7.79.1","timestamp":"2026-05-28T13:06:58Z",'
        '"install_method":{"tool":"install_script"}}]}')
    (root / "install_info.log").write_text(
        "install_method:\n  tool: install_script\n")
    return root


def test_report_api(problem_flare):
    app = create_app(db_path=":memory:")
    with TestClient(app) as c:
        run = c.post("/api/scan", json={
            "scanners": ["agent-files", "flare"],
            "root": str(problem_flare)}).json()
        wait_for_run(c, run["run_id"])

        sources = c.get("/api/flare/sources").json()["sources"]
        assert len(sources) == 1
        source = sources[0]["source"]

        r = c.get("/api/flare/report", params={"source": source}).json()
        assert r["verdict"] == "problems found"
        assert r["problems"] >= 3   # FAIL + unhealthy + config error
        assert r["diagnose"]["fail"] == 1
        assert r["health"]["unhealthy"] == ["tagger-store"]
        assert r["config_errors"][0]["name"] == "mcp_server"
        assert r["agent"]["version_history"][0]["version"] == "7.79.1"
        assert r["agent"]["install_method"] == "install_script"
        assert r["log_levels"].get("INFO") == 5   # logs/agent.log records

        assert c.get("/api/flare/report",
                     params={"source": "/nope"}).status_code == 404
