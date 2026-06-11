"""Tar-archive upload: extraction, scan of extracted root, rejection cases."""

import io
import tarfile

import pytest
from fastapi.testclient import TestClient

from logscope.web.app import create_app
from tests.conftest import SYNTH_TOTAL, write_synth_logs
from tests.test_api import wait_for_run


def make_tar(directory, compress="gz"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=f"w:{compress}") as tar:
        for path in sorted(directory.iterdir()):
            tar.add(path, arcname=f"logs/{path.name}")
    buf.seek(0)
    return buf


@pytest.fixture()
def client(tmp_path):
    app = create_app(db_path=":memory:", uploads_root=tmp_path / "uploads")
    with TestClient(app) as c:
        yield c


def test_upload_extract_and_scan(client, synth_logs, tmp_path):
    tar_buf = make_tar(synth_logs)
    r = client.post("/api/upload",
                    files={"file": ("mylogs.tar.gz", tar_buf, "application/gzip")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["files"] == 2
    assert "mylogs" in body["root"]

    run = client.post("/api/scan", json={
        "scanners": ["agent-files"], "root": body["root"]}).json()
    run = wait_for_run(client, run["run_id"])
    assert run["status"] == "done"
    assert run["record_count"] == SYNTH_TOTAL


def test_upload_plain_tar(client, synth_logs):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(synth_logs / "agent.log", arcname="agent.log")
    buf.seek(0)
    r = client.post("/api/upload", files={"file": ("x.tar", buf)})
    assert r.status_code == 200
    assert r.json()["files"] == 1


def test_upload_rejects_non_tar(client):
    r = client.post("/api/upload",
                    files={"file": ("notes.txt", io.BytesIO(b"hello world"))})
    assert r.status_code == 400
    assert "not a readable tar" in r.json()["detail"]


def test_upload_rejects_path_traversal(client, tmp_path):
    evil = tmp_path / "evil.log"
    evil.write_text("malicious\n")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(evil, arcname="../../escape.log")
    buf.seek(0)
    r = client.post("/api/upload", files={"file": ("evil.tar.gz", buf)})
    assert r.status_code == 400
    assert "unsafe" in r.json()["detail"]
    # nothing escaped the uploads root
    assert not (tmp_path / "escape.log").exists()


def test_upload_rejects_absolute_member(client, tmp_path):
    evil = tmp_path / "abs.log"
    evil.write_text("x\n")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tar.gettarinfo(evil, arcname="/tmp/absolute-escape.log")
        with open(evil, "rb") as f:
            tar.addfile(info, f)
    buf.seek(0)
    r = client.post("/api/upload", files={"file": ("abs.tar", buf)})
    # "data" filter strips leading slashes or rejects — either way nothing
    # lands outside the upload dir
    import pathlib
    assert not pathlib.Path("/tmp/absolute-escape.log").exists()
    assert r.status_code in (200, 400)
