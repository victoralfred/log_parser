"""Run the logscope dashboard: python -m logscope [--port N] [--db PATH]"""

import argparse

import uvicorn

from logscope.web.app import create_app


def main():
    ap = argparse.ArgumentParser(prog="logscope",
                                 description="Datadog Agent log-analysis dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--db", default="logscope.db",
                    help="SQLite db path (default logscope.db)")
    args = ap.parse_args()
    uvicorn.run(create_app(db_path=args.db), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
