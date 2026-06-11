"""Flare ingestion: document extraction, categorization, API, zip upload."""

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from logscope.core.analysis import flatten
from logscope.core.contract import ScanTarget
from logscope.scanners.flare import MAX_DOC_BYTES, FlareScanner
from logscope.web.app import create_app
from tests.conftest import make_line
from tests.test_api import wait_for_run

DATADOG_YAML = """api_key: '****abcd'
site: datadoghq.com
env: dev
tags:
  - team:platform
apm_config:
  enabled: true
"""

HOST_JSON = '{"agentVersion": "7.79.1", "os": "linux", "cpuCores": 4}'


def make_flare(root):
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "datadog.yaml").write_text(DATADOG_YAML)
    (root / "runtime_config_dump.yaml").write_text("log_level: info\n")
    (root / "metadata" / "inventory").mkdir(parents=True)
    (root / "metadata" / "host.json").write_text(HOST_JSON)
    (root / "health.yaml").write_text("healthy:\n  - forwarder\nunhealthy: []\n")
    (root / "flare_creation.log").write_text("Flare creation time: 2026-06-11\n")
    (root / "logs").mkdir()
    (root / "logs" / "agent.log").write_text(
        "\n".join(make_line("CORE", "INFO", i) for i in range(5)) + "\n")
    (root / "telemetry.log").write_text("aggregator__channel_size 0\n")
    (root / "empty.yaml").write_text("")                       # skipped
    (root / "remote-config.db").write_bytes(b"\x00" * 64)      # skipped
    (root / "big.txt").write_text("x" * (MAX_DOC_BYTES + 100))  # truncated
    return root


@pytest.fixture()
def flare_dir(tmp_path):
    return make_flare(tmp_path / "myflare")


def test_scanner_discovers_flare(flare_dir, tmp_path):
    scanner = FlareScanner()
    assert scanner.discover(ScanTarget(root=str(flare_dir)))
    notflare = tmp_path / "plain"
    notflare.mkdir()
    assert scanner.discover(ScanTarget(root=str(notflare))) == []


def test_documents_extraction(flare_dir):
    scanner = FlareScanner()
    docs = {d.path: d for d in scanner.documents(ScanTarget(root=str(flare_dir)))}

    assert docs["etc/datadog.yaml"].category == "config"
    assert docs["etc/datadog.yaml"].scrubbed is True
    assert docs["runtime_config_dump.yaml"].category == "config"
    assert docs["metadata/host.json"].category == "metadata"
    assert docs["metadata/host.json"].format == "json"
    assert docs["health.yaml"].category == "metadata"
    assert docs["telemetry.log"].category == "log-other"
    assert docs["big.txt"].truncated is True
    assert len(docs["big.txt"].content) == MAX_DOC_BYTES

    assert "logs/agent.log" not in docs        # records, not documents
    assert "remote-config.db" not in docs      # binary
    assert "empty.yaml" not in docs            # empty


def test_flatten():
    pairs = dict(flatten({"apm_config": {"enabled": True},
                          "tags": ["a", "b"], "x": None}))
    assert pairs["apm_config.enabled"] == "true"
    assert pairs["tags[0]"] == "a"
    assert pairs["x"] == "null"


def test_full_scan_and_documents_api(flare_dir):
    app = create_app(db_path=":memory:")
    with TestClient(app) as c:
        run = c.post("/api/scan", json={
            "scanners": ["agent-files", "flare"],
            "root": str(flare_dir)}).json()
        run = wait_for_run(c, run["run_id"])
        assert run["status"] == "done"
        assert run["record_count"] == 5     # logs/agent.log via agent-files
        doc_entries = [s for s in run["sources"] if s.get("documents")]
        assert doc_entries and doc_entries[0]["documents"] >= 6

        listing = c.get("/api/documents", params={"category": "config"}).json()
        paths = {d["path"] for d in listing["documents"]}
        assert paths == {"etc/datadog.yaml", "runtime_config_dump.yaml"}
        assert listing["counts"]["metadata"] >= 3

        doc_id = next(d["id"] for d in listing["documents"]
                      if d["path"] == "etc/datadog.yaml")
        doc = c.get(f"/api/documents/{doc_id}").json()
        parsed = dict(map(tuple, doc["parsed"]))
        assert parsed["api_key"] == "****abcd"
        assert parsed["apm_config.enabled"] == "true"
        assert doc["scrubbed"]

        raw = c.get(f"/api/documents/{doc_id}/raw")
        assert raw.text == DATADOG_YAML

        # content search
        hit = c.get("/api/documents", params={"q": "agentVersion"}).json()
        assert {d["path"] for d in hit["documents"]} == {"metadata/host.json"}


def test_zip_upload_of_flare(flare_dir, tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in flare_dir.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=p.relative_to(flare_dir).as_posix())
    buf.seek(0)
    app = create_app(db_path=":memory:", uploads_root=tmp_path / "up")
    with TestClient(app) as c:
        r = c.post("/api/upload", files={"file": ("flare.zip", buf)})
        assert r.status_code == 200, r.text
        root = r.json()["root"]
        run = c.post("/api/scan", json={
            "scanners": ["agent-files", "flare"], "root": root}).json()
        run = wait_for_run(c, run["run_id"])
        assert run["status"] == "done"
        assert run["record_count"] == 5
        docs = c.get("/api/documents").json()
        assert docs["counts"].get("config") == 2


def test_wrapper_dir_detection(tmp_path):
    """Real flares unzip into a hostname wrapper dir — detect one level down."""
    wrapper = tmp_path / "extracted"
    make_flare(wrapper / "voseghale-HP")
    scanner = FlareScanner()
    sources = scanner.discover(ScanTarget(root=str(wrapper)))
    assert len(sources) == 1
    assert sources[0].label == "flare: voseghale-HP"
    docs = list(scanner.documents(ScanTarget(root=str(wrapper))))
    assert {d.path for d in docs} >= {"etc/datadog.yaml", "metadata/host.json"}
    # paths stay relative to the flare root, not the wrapper
    assert all(not d.path.startswith("voseghale-HP/") for d in docs)


def test_wrapped_zip_upload_and_rescan_idempotent(tmp_path):
    """Zip with a wrapper dir (the real flare layout) ingests documents,
    and scanning twice doesn't duplicate them."""
    wrapper = tmp_path / "stage"
    flare = make_flare(wrapper / "myhost-HP")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in flare.rglob("*"):
            if p.is_file():
                zf.write(p, arcname=f"myhost-HP/{p.relative_to(flare).as_posix()}")
    buf.seek(0)
    app = create_app(db_path=":memory:", uploads_root=tmp_path / "up")
    with TestClient(app) as c:
        root = c.post("/api/upload",
                      files={"file": ("flare.zip", buf)}).json()["root"]
        for _ in range(2):   # second scan must replace, not duplicate
            run = c.post("/api/scan", json={
                "scanners": ["agent-files", "flare"], "root": root}).json()
            run = wait_for_run(c, run["run_id"])
            assert run["status"] == "done"
        counts = c.get("/api/documents").json()["counts"]
        assert counts["config"] == 2
        assert run["record_count"] == 5   # wrapped logs/ still found by rglob


def test_zip_traversal_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../zip-escape.log", "evil")
    buf.seek(0)
    app = create_app(db_path=":memory:", uploads_root=tmp_path / "up")
    with TestClient(app) as c:
        r = c.post("/api/upload", files={"file": ("evil.zip", buf)})
        assert r.status_code == 400
        assert "unsafe" in r.json()["detail"]
    assert not (tmp_path / "zip-escape.log").exists()
