"""Plugin discovery: load Scanner implementations from drop-in directories.

Any .py file in logscope/scanners/ (plus directories listed in the
LOGSCOPE_PLUGIN_DIRS env var, colon-separated) that defines a concrete
Scanner subclass is loaded at startup. Broken plugins never crash the app —
they are recorded with their error and shown greyed-out in the UI.
"""

import importlib.util
import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from logscope.core.contract import Scanner, ScannerError

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "scanners"


@dataclass
class ScannerStatus:
    name: str                      # scanner name, or file stem if load failed
    module: str                    # source file path
    ok: bool
    error: str | None = None
    channel: str = ""
    description: str = ""
    supports_follow: bool = False
    needs_host_access: bool = False
    panels: list = field(default_factory=list)


def _plugin_dirs(extra_dirs=None):
    dirs = [BUILTIN_DIR]
    env = os.environ.get("LOGSCOPE_PLUGIN_DIRS", "")
    for d in list(extra_dirs or []) + [p for p in env.split(":") if p]:
        dirs.append(Path(d))
    return dirs


class Registry:
    def __init__(self, plugin_dirs=None):
        self._dirs = _plugin_dirs(plugin_dirs)
        self._scanners: dict[str, Scanner] = {}
        self._statuses: list[ScannerStatus] = []

    def load(self):
        self._scanners.clear()
        self._statuses.clear()
        for directory in self._dirs:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("_"):
                    continue
                self._load_file(path)

    def _load_file(self, path: Path):
        try:
            spec = importlib.util.spec_from_file_location(
                "logscope_plugin_" + path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            self._statuses.append(ScannerStatus(
                name=path.stem, module=str(path), ok=False,
                error=_tail(traceback.format_exc())))
            return

        classes = [obj for obj in vars(module).values()
                   if isinstance(obj, type) and issubclass(obj, Scanner)
                   and obj is not Scanner]
        if not classes:
            return  # helper module, not a plugin — silently ignore
        for cls in classes:
            try:
                scanner = cls()
                scanner.validate()
                if scanner.name in self._scanners:
                    raise ScannerError(
                        f"duplicate scanner name '{scanner.name}' "
                        f"(already provided by another plugin)")
                self._scanners[scanner.name] = scanner
                self._statuses.append(ScannerStatus(
                    name=scanner.name, module=str(path), ok=True,
                    channel=scanner.channel, description=scanner.description,
                    supports_follow=scanner.supports_follow,
                    needs_host_access=scanner.needs_host_access,
                    panels=[vars(p) for p in scanner.panels()]))
            except Exception:
                self._statuses.append(ScannerStatus(
                    name=getattr(cls, "name", "") or cls.__name__,
                    module=str(path), ok=False,
                    error=_tail(traceback.format_exc())))

    def scanners(self) -> list[Scanner]:
        return list(self._scanners.values())

    def statuses(self) -> list[ScannerStatus]:
        return list(self._statuses)

    def get(self, name: str) -> Scanner:
        return self._scanners[name]


def _tail(tb: str, lines: int = 6) -> str:
    return "\n".join(tb.strip().splitlines()[-lines:])
