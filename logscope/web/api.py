"""REST API for the logscope dashboard and any programmatic consumers."""

import json
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, UploadFile
from pydantic import BaseModel

from logscope.core import analysis
from logscope.core.contract import ScannerError, ScanTarget
from logscope.core.store import execute_scan
from logscope.web.uploads import MAX_ARCHIVE_BYTES, UploadError, extract_archive

router = APIRouter(prefix="/api")

KNOWN_LEVELS = {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"}


def _ctx(request: Request):
    return request.app.state.registry, request.app.state.store


class ScanRequest(BaseModel):
    scanners: list[str] | None = None   # None = all loaded scanners
    root: str = "."
    since: str | None = None
    options: dict = {}
    levels: list[str] | None = None     # store only these levels (None = all);
                                        # level-less records are always kept


@router.get("/scanners")
def list_scanners(request: Request, root: str | None = None):
    registry, _ = _ctx(request)
    statuses = [vars(s) for s in registry.statuses()]
    if root:
        target = ScanTarget(root=root)
        for status in statuses:
            if not status["ok"]:
                continue
            try:
                sources = registry.get(status["name"]).discover(target)
                status["sources"] = [vars(src) for src in sources]
            except ScannerError as exc:
                status["sources"] = []
                status["discover_error"] = str(exc)
    return {"scanners": statuses}


@router.post("/scan")
def start_scan(request: Request, body: ScanRequest):
    registry, store = _ctx(request)
    target = ScanTarget(root=body.root, since=body.since,
                        options=body.options)
    known = {s.name for s in registry.scanners()}
    names = body.scanners or sorted(known)
    unknown = [n for n in names if n not in known]
    if unknown:
        raise HTTPException(400, f"unknown scanners: {', '.join(unknown)}")
    if body.levels:
        bad = [l for l in body.levels if l.upper() not in KNOWN_LEVELS]
        if bad:
            raise HTTPException(400, f"unknown levels: {', '.join(bad)}")
    levels = [l.upper() for l in body.levels] if body.levels else None
    run_id = execute_scan(registry, store, target, names, levels=levels)
    return {"run_id": run_id}


@router.get("/scan/{run_id}")
def scan_status(request: Request, run_id: int):
    _, store = _ctx(request)
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return run


@router.get("/records")
def get_records(request: Request, service: str | None = None,
                level: str | None = None, component: str | None = None,
                fingerprint: str | None = None, channel: str | None = None,
                source: str | None = None, q: str | None = None,
                regex: str | None = None,
                since: float | None = None, until: float | None = None,
                parsed: bool | None = None, limit: int = 200,
                offset: int = 0, order: str = "desc"):
    """`level` accepts a single level or a comma list ("ERROR,CRITICAL");
    `q` is substring match, `regex` a case-insensitive regular expression."""
    _, store = _ctx(request)
    filters = dict(service=service, level=level, component=component,
                   fp=fingerprint, channel=channel, source=source, q=q,
                   regex=regex, since=since, until=until, parsed=parsed)
    try:
        rows = analysis.records(store, limit=min(limit, 1000), offset=offset,
                                order=order, **filters)
    except re.error as exc:
        raise HTTPException(400, f"invalid regex: {exc}")
    for row in rows:
        row["continuation"] = json.loads(row["continuation"] or "[]")
        row["extra"] = json.loads(row["extra"] or "{}")
    return {"total": analysis.record_count(store, **filters), "records": rows}


@router.get("/records/{record_id}")
def get_record(request: Request, record_id: int):
    _, store = _ctx(request)
    row = store.one("SELECT * FROM records WHERE id = ?", (record_id,))
    if row is None:
        raise HTTPException(404, "no such record")
    row["continuation"] = json.loads(row["continuation"] or "[]")
    row["extra"] = json.loads(row["extra"] or "{}")
    return row


@router.get("/summary")
def get_summary(request: Request):
    _, store = _ctx(request)
    return analysis.summary(store)


@router.get("/fingerprints")
def get_fingerprints(request: Request, limit: int = 50,
                     level: str | None = None, service: str | None = None,
                     min_level: str | None = None):
    _, store = _ctx(request)
    return {"fingerprints": analysis.fingerprints(
        store, limit=min(limit, 500), level=level, service=service,
        min_level=min_level)}


@router.get("/timeline")
def get_timeline(request: Request, bucket: int = 60,
                 service: str | None = None, level: str | None = None,
                 fingerprint: str | None = None, regex: str | None = None):
    _, store = _ctx(request)
    try:
        points = analysis.timeline(store, bucket=bucket, service=service,
                                   level=level, fp=fingerprint, regex=regex)
    except re.error as exc:
        raise HTTPException(400, f"invalid regex: {exc}")
    return {"bucket": bucket, "points": points}


@router.get("/gaps")
def get_gaps(request: Request, threshold: float = 300.0):
    _, store = _ctx(request)
    return {"threshold": threshold, "gaps": analysis.gaps(store, threshold)}


@router.get("/panels")
def get_panels(request: Request):
    registry, _ = _ctx(request)
    panels = []
    for status in registry.statuses():
        if status.ok:
            for panel in status.panels:
                panels.append({**panel, "scanner": status.name})
    return {"panels": panels}


@router.post("/upload")
async def upload_archive(request: Request, file: UploadFile):
    """Accept a tar(.gz/.bz2/.xz) of log files, extract it server-side, and
    return the directory path to use as the scan root."""
    uploads_root = request.app.state.uploads_root
    name = file.filename or "upload.tar"
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        written = 0
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_ARCHIVE_BYTES:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(
                    413, f"archive exceeds {MAX_ARCHIVE_BYTES // 2**20} MiB limit")
            tmp.write(chunk)
    try:
        result = extract_archive(tmp_path, uploads_root,
                                 label=Path(name).stem.removesuffix(".tar"))
    except UploadError as exc:
        raise HTTPException(400, str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)
    return result


@router.get("/scanners/{name}/panels/{panel_id}")
def get_panel_data(request: Request, name: str, panel_id: str):
    registry, store = _ctx(request)
    try:
        scanner = registry.get(name)
    except KeyError:
        raise HTTPException(404, "no such scanner")
    try:
        return scanner.panel_data(panel_id, store)
    except KeyError:
        raise HTTPException(404, "no such panel")
