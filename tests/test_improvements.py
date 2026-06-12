"""FTS search, upload dedupe, scan serialization, run housekeeping."""

import io
import tarfile
import threading

import pytest
from fastapi.testclient import TestClient

from logscope.core import store as store_mod
from logscope.core.analysis import _fts_query
from logscope.core.store import Store
from logscope.web.app import create_app
from tests.test_api import wait_for_run


@pytest.fixture()
def client(synth_logs, tmp_path):
    app = create_app(db_path=":memory:", uploads_root=tmp_path / "up")
    with TestClient(app) as c:
        run = c.post("/api/scan", json={
            "scanners": ["agent-files"], "root": str(synth_logs)}).json()
        wait_for_run(c, run["run_id"])
        yield c


# --- FTS ---

def test_fts_query_builder():
    assert _fts_query("permission denied") == '"permission"* "denied"*'
    assert _fts_query("100% broken") is None        # special chars -> LIKE
    assert _fts_query("") is None


def test_fts_enabled_and_matches_like(client):
    store = client.app.state.store
    assert store.fts_enabled
    fts_total = client.get("/api/records", params={"q": "permission denied"}).json()["total"]
    assert fts_total == 50
    # LIKE fallback path (special char) still works
    like_total = client.get("/api/records", params={"q": "denied for /run"}).json()["total"]
    assert like_total == 50


def test_fts_survives_rescan(client, synth_logs):
    run = client.post("/api/scan", json={
        "scanners": ["agent-files"], "root": str(synth_logs)}).json()
    wait_for_run(client, run["run_id"])
    total = client.get("/api/records", params={"q": "permission denied"}).json()["total"]
    assert total == 50  # triggers kept the index in sync through replace


# --- upload dedupe ---

def _tar_of(directory):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(directory.iterdir()):
            tar.add(p, arcname=p.name)
    return buf.getvalue()


def test_upload_dedupe(client, synth_logs):
    data = _tar_of(synth_logs)
    r1 = client.post("/api/upload", files={"file": ("a.tar.gz", io.BytesIO(data))}).json()
    assert "deduplicated" not in r1
    r2 = client.post("/api/upload", files={"file": ("b.tar.gz", io.BytesIO(data))}).json()
    assert r2["deduplicated"] is True
    assert r2["root"] == r1["root"]


# --- scan serialization ---

def test_concurrent_scan_rejected(client, synth_logs, monkeypatch):
    # hold the active-scan slot as if a scan were in flight
    monkeypatch.setattr(store_mod, "_active_run_id", 42)
    r = client.post("/api/scan", json={
        "scanners": ["agent-files"], "root": str(synth_logs)})
    assert r.status_code == 409
    assert "already running" in r.json()["detail"]


# --- run housekeeping ---

def test_stale_running_runs_failed_on_restart(tmp_path):
    db = tmp_path / "x.db"
    s1 = Store(str(db))
    run_id = s1.create_run(["agent-files"], "/tmp")
    assert s1.get_run(run_id)["status"] == "running"
    s2 = Store(str(db))   # simulates server restart
    assert s2.get_run(run_id)["status"] == "failed"
    assert "interrupted" in s2.get_run(run_id)["error"]
