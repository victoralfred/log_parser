#!/usr/bin/env python3
"""CLI for parsing/classifying Datadog Agent log files.

Thin wrapper around logscope.core.datadog_format — the parsing logic lives in
the logscope package, where it also powers the agent-files scanner plugin.

Usage:
    datadog_log_parser.py [-o ndjson|summary] [--component-depth N] FILE...
    cat agent.log | datadog_log_parser.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logscope.core.datadog_format import emit_ndjson, emit_summary, parse_stream


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="*", help="log files (default: stdin)")
    ap.add_argument("-o", "--output", choices=("ndjson", "summary"),
                    default="ndjson")
    ap.add_argument("--component-depth", type=int, default=4,
                    help="path depth for component classification (default 4)")
    args = ap.parse_args(argv)

    def records():
        if not args.files:
            yield from parse_stream(sys.stdin, "<stdin>", args.component_depth)
        for path in args.files:
            with open(path, errors="replace") as f:
                yield from parse_stream(f, path, args.component_depth)

    emit = emit_summary if args.output == "summary" else emit_ndjson
    try:
        emit(records(), sys.stdout)
    except BrokenPipeError:
        sys.stderr.close()  # suppress the secondary flush error on exit
    return 0


if __name__ == "__main__":
    sys.exit(main())
