"""Command line: ``python -m ledger serve | migrate | check``."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .check import check
from .db import Database, migrate

DEFAULT_DB = os.environ.get("LEDGER_DB", "ledger.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ledger", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--db", default=DEFAULT_DB)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    mig = sub.add_parser("migrate", help="create or upgrade the database schema")
    mig.add_argument("--db", default=DEFAULT_DB)

    audit = sub.add_parser("check", help="audit the ledger invariants; exit 1 on any violation")
    audit.add_argument("--db", default=DEFAULT_DB)

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        from .api import create_app

        logging.basicConfig(level=logging.INFO, format="%(message)s")
        # One process: SQLite serialises writers anyway, and a single process keeps
        # every write on the same file lock with no cross-process contention.
        uvicorn.run(create_app(args.db), host=args.host, port=args.port, access_log=False)
        return 0

    db = Database(args.db)
    try:
        if args.command == "migrate":
            applied = migrate(db)
            print("applied: " + ", ".join(applied) if applied else "schema is up to date")
            return 0
        if not os.path.exists(args.db):
            print(f"no database at {args.db}", file=sys.stderr)
            return 2
        problems = check(db)
        for problem in problems:
            print(problem)
        print(f"{len(problems)} problem(s)" if problems else "ledger is consistent")
        return 1 if problems else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
