import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from logscope.web.app import create_app
from tests.conftest import SYNTH_TOTAL

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def client(synth_logs):
    app = create_app(db_path=":memory:", plugin_dirs=[FIXTURES])
    with TestClient(app) as c:
        c.synth_root = str(synth_logs)
        yield c


def wait_for_run(client, run_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/scan/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.2)
    raise TimeoutError("scan did not finish")


@pytest.fixture(scope="module")
def scanned(client):
    r = client.post("/api/scan",
                    json={"scanners": ["agent-files"], "root": client.synth_root})
    assert r.status_code == 200
    return wait_for_run(client, r.json()["run_id"])


def test_scan_completes_with_expected_totals(scanned):
    assert scanned["status"] == "done"
    assert scanned["record_count"] == SYNTH_TOTAL
    assert all(s["status"] == "done" for s in scanned["sources"])


def test_summary(client, scanned):
    s = client.get("/api/summary").json()
    assert s["total"] == SYNTH_TOTAL
    assert s["unparsed"] == 0
    services = {row["service"] for row in s["matrix"]}
    assert services == {"agent", "trace-agent"}


def test_filtered_records(client, scanned):
    r = client.get("/api/records",
                   params={"service": "trace-agent", "level": "ERROR"}).json()
    assert r["total"] == 5
    assert all(rec["service"] == "trace-agent" and rec["level"] == "ERROR"
               for rec in r["records"])


def test_fingerprints_dedup(client, scanned):
    fps = client.get("/api/fingerprints", params={"limit": 5}).json()["fingerprints"]
    by_count = {f["count"] for f in fps}
    # CORE + TRACE INFO share one template (100 + 20); the permission-denied
    # WARNs collapse to one fingerprint despite 50 unique paths
    assert 120 in by_count
    assert 50 in by_count


def test_timeline_buckets_sum_to_total(client, scanned):
    tl = client.get("/api/timeline", params={"bucket": 3600}).json()
    assert sum(p["n"] for p in tl["points"]) == SYNTH_TOTAL


def test_record_detail(client, scanned):
    rec = client.get("/api/records", params={"limit": 1}).json()["records"][0]
    detail = client.get(f"/api/records/{rec['id']}").json()
    assert detail["id"] == rec["id"]
    assert isinstance(detail["continuation"], list)


def test_scanners_endpoint_reports_broken_plugin(client):
    scanners = client.get("/api/scanners").json()["scanners"]
    by_name = {s["name"]: s for s in scanners}
    assert by_name["agent-files"]["ok"]
    assert by_name["toy"]["ok"]
    assert not by_name["broken_plugin"]["ok"]


def test_unknown_scanner_rejected(client):
    r = client.post("/api/scan", json={"scanners": ["nope"], "root": "."})
    assert r.status_code == 400


def test_panels_listed(client):
    panels = client.get("/api/panels").json()["panels"]
    assert any(p["panel_id"] == "lifecycle" for p in panels)
