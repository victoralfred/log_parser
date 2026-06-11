"""Message-template fingerprinting for cross-channel dedup.

Lives in core and is applied uniformly by the ingest engine so the same
message produces the same fingerprint regardless of which scanner emitted it.
Scanners never compute fingerprints themselves.
"""

import hashlib
import re

# Ordered: quoted strings first (so IDs inside quotes collapse with them),
# then paths (so IDs inside path segments collapse with the path), then
# specific token shapes, then bare numbers last.
_SUBSTITUTIONS = [
    (re.compile(r'"[^"]*"'), "<S>"),
    (re.compile(r"'[^']*'"), "<S>"),
    (re.compile(r"/[\w@.+-]+(?:/[\w@.+-]+)+"), "<PATH>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<ID>"),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<ID>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"), "<TS>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h)\b"), "<DUR>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<N>"),
]


def template(msg: str) -> str:
    """Normalize a message to its template form (variable parts replaced)."""
    out = msg
    for pattern, repl in _SUBSTITUTIONS:
        out = pattern.sub(repl, out)
    return out


def fingerprint(msg: str) -> str:
    """Stable 12-hex-char key for the message's template."""
    return hashlib.sha1(template(msg).encode("utf-8", "replace")).hexdigest()[:12]
