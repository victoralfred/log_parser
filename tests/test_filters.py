"""Regex search, multi-level filtering, and ingest-time level selection."""

import time

import pytest
from fastapi.testclient import TestClient

from logscope.web.app import create_app
from tests.conftest import SYNTH_ERRORS, SYNTH_TOTAL
from tests.test_api import wait_for_run


@pytest.fixture(scope="module")
def client(synth_logs):
    app = create_app(db_path=":memory:")
    with TestClient(app) as c:
        run = c.post("/api/scan", json={
            "scanners": ["agent-files"], "root": str(synth_logs)}).json()
        wait_for_run(c, run["run_id"])
        c.synth_root = str(synth_logs)
        yield c


def test_regex_simple(client):
    r = client.get("/api/records",
                   params={"regex": r"permission\s+denied"}).json()
    assert r["total"] == 50
    assert all("permission denied" in rec["msg"] for rec in r["records"][:20])


def test_regex_case_insensitive_default(client):
    r = client.get("/api/records", params={"regex": "PERMISSION DENIED"}).json()
    assert r["total"] == 50


def test_regex_complex(client):
    r = client.get("/api/records", params={
        "regex": r"Post \"https://intake\.example\.com/api/v\d+\":"
                 r" connection (refused|reset)"}).json()
    assert r["total"] == 10
    r2 = client.get("/api/records", params={
        "regex": r"connection (refused|reset)", "level": "ERROR",
        "service": "agent"}).json()
    assert r2["total"] == 10


def test_regex_invalid_is_400(client):
    r = client.get("/api/records", params={"regex": "[unclosed"})
    assert r.status_code == 400
    assert "invalid regex" in r.json()["detail"]
    r = client.get("/api/timeline", params={"regex": "(bad"})
    assert r.status_code == 400


def test_multi_level_filter(client):
    err = client.get("/api/records", params={"level": "ERROR"}).json()["total"]
    warn = client.get("/api/records", params={"level": "WARN"}).json()["total"]
    both = client.get("/api/records", params={"level": "ERROR,WARN"}).json()["total"]
    assert err == SYNTH_ERRORS and warn == 50
    assert both == err + warn
    rows = client.get("/api/records",
                      params={"level": "ERROR,WARN", "limit": 100}).json()["records"]
    assert {r["level"] for r in rows} == {"ERROR", "WARN"}


def test_ingest_level_filter(synth_logs):
    app = create_app(db_path=":memory:")
    with TestClient(app) as c:
        run = c.post("/api/scan", json={
            "scanners": ["agent-files"], "root": str(synth_logs),
            "levels": ["error", "critical"]}).json()  # case-insensitive
        run = wait_for_run(c, run["run_id"])
        assert run["status"] == "done"
        summary = c.get("/api/summary").json()
        assert summary["total"] == SYNTH_ERRORS
        skipped = sum(s["skipped"] for s in run["sources"])
        assert skipped == SYNTH_TOTAL - SYNTH_ERRORS
        assert {row["level"] for row in summary["matrix"]} == {"ERROR"}


def test_unknown_scan_level_rejected(synth_logs):
    app = create_app(db_path=":memory:")
    with TestClient(app) as c:
        r = c.post("/api/scan", json={"root": str(synth_logs),
                                      "levels": ["VERBOSE"]})
        assert r.status_code == 400
