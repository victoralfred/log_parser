"""Safe extraction of uploaded tar archives into a managed directory.

Web users typically have no filesystem access on the server, so they upload
a tar of their log directory instead. Each upload gets its own directory
under the uploads root; the returned path is then used as a ScanTarget root.
"""

import shutil
import tarfile
import uuid
import zipfile
from pathlib import Path

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024        # compressed upload cap
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024  # tar-bomb guard
MAX_MEMBERS = 10_000


class UploadError(Exception):
    """User-facing rejection (bad archive, too large, unsafe members)."""


def extract_archive(archive_path: Path, uploads_root: Path,
                    label: str = "upload") -> dict:
    """Extract a tar(.gz/.bz2/.xz) or zip archive into a fresh directory.

    Tar uses the stdlib "data" extraction filter, which rejects path
    traversal, absolute paths, links escaping the tree, and device nodes;
    zip members are path-validated individually. Returns
    {"root": str, "files": int, "bytes": int}.
    """
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise UploadError(
            f"archive exceeds {MAX_ARCHIVE_BYTES // 2**20} MiB limit")
    if zipfile.is_zipfile(archive_path):
        return _extract_zip(archive_path, uploads_root, label)
    try:
        tar = tarfile.open(archive_path, mode="r:*")
    except tarfile.TarError as exc:
        raise UploadError(f"not a readable tar or zip archive: {exc}")

    with tar:
        members = []
        total = 0
        for member in tar:
            if len(members) >= MAX_MEMBERS:
                raise UploadError(f"archive has more than {MAX_MEMBERS} members")
            if member.isfile():
                total += member.size
                if total > MAX_EXTRACTED_BYTES:
                    raise UploadError(
                        f"extracted size exceeds {MAX_EXTRACTED_BYTES // 2**30} GiB limit")
            members.append(member)

        dest = uploads_root / f"{_slug(label)}-{uuid.uuid4().hex[:8]}"
        dest.mkdir(parents=True, exist_ok=False)
        try:
            tar.extractall(dest, members=members, filter="data")
        except tarfile.TarError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise UploadError(f"unsafe or corrupt archive member: {exc}")

    files = sum(1 for p in dest.rglob("*") if p.is_file())
    return {"root": str(dest), "files": files, "bytes": total}


def _extract_zip(archive_path: Path, uploads_root: Path, label: str) -> dict:
    """Datadog flares ship as zip — extract with per-member path validation."""
    try:
        zf = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise UploadError(f"not a readable zip archive: {exc}")
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise UploadError(f"archive has more than {MAX_MEMBERS} members")
        total = sum(i.file_size for i in infos if not i.is_dir())
        if total > MAX_EXTRACTED_BYTES:
            raise UploadError(
                f"extracted size exceeds {MAX_EXTRACTED_BYTES // 2**30} GiB limit")

        dest = uploads_root / f"{_slug(label)}-{uuid.uuid4().hex[:8]}"
        dest.mkdir(parents=True, exist_ok=False)
        dest_resolved = dest.resolve()
        try:
            for info in infos:
                if info.is_dir():
                    continue
                target = (dest / info.filename).resolve()
                if not target.is_relative_to(dest_resolved):
                    raise UploadError(
                        f"unsafe archive member: {info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        except UploadError:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        except (OSError, zipfile.BadZipFile) as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise UploadError(f"corrupt archive member: {exc}")

    files = sum(1 for p in dest.rglob("*") if p.is_file())
    return {"root": str(dest), "files": files, "bytes": total}


def _slug(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    return (safe.strip("-") or "upload")[:40]
