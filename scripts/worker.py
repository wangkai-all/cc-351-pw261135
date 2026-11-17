#!/usr/bin/env python3
"""Long-running worker for the local demo.

Runs collection, relation checking and publication on independent cadences,
exactly like the three separate services in production.  Every step opens its
own connection, so a stuck step cannot hold a transaction for the others.

    python3 scripts/worker.py --source http://127.0.0.1:8100 --once
    python3 scripts/worker.py --source http://127.0.0.1:8100 --interval 30
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from corpus_audit import db  # noqa: E402
from corpus_audit.collector import Collector, Store  # noqa: E402
from corpus_audit.fetch import Client  # noqa: E402
from corpus_audit.publish import verify_and_publish  # noqa: E402
from corpus_audit.relations import RelationVerifier  # noqa: E402


def step(name: str, fn) -> None:
    started = time.time()
    try:
        result = fn()
        print(
            f"[{name}] {time.time() - started:.2f}s "
            + json.dumps(result, ensure_ascii=False),
            flush=True,
        )
    except Exception:
        print(f"[{name}] failed after {time.time() - started:.2f}s", file=sys.stderr)
        traceback.print_exc()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="http://127.0.0.1:8100")
    parser.add_argument("--db", default="data/catalog.db")
    parser.add_argument("--data", default="data")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    db.init_db(args.db)
    while True:
        conn = db.connect(args.db)
        try:
            step(
                "collect",
                lambda: Collector(conn, Client(args.source), Store(Path(args.data))).run(),
            )
        finally:
            conn.close()

        conn = db.connect(args.db)
        try:
            step(
                "relations",
                lambda: RelationVerifier(
                    conn, Client(args.source), Store(Path(args.data))
                ).run(limit=500),
            )
        finally:
            conn.close()

        step("publish", lambda: verify_and_publish(args.db))

        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
