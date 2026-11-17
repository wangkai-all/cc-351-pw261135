"""Command line for the multilingual corpus audit.

Subcommands mirror the three independent services:

  source      run the controllable fictional source site
  collect     run the collector once (or as a loop)
  relations   run the independent relation checker over queued jobs
  publish     verify + publish dirty items (conflict policy applies)
  policy      inspect or change the conflict policy
  serve       run the read-only public catalog
  demo        one-shot local end-to-end run with the fictional source
  inspect     dump the analysis of one item as JSON (read only)
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from corpus_audit import db
from corpus_audit.analyze import analyze_item
from corpus_audit.collector import Collector, Store
from corpus_audit.fetch import Client
from corpus_audit.publish import set_policy, verify_and_publish
from corpus_audit.relations import RelationVerifier

DEFAULT_DB = "data/catalog.db"
DEFAULT_DATA = "data"


def _prepare(db_path: str, data_root: str) -> None:
    db.init_db(db_path)
    Store(Path(data_root)).root.mkdir(parents=True, exist_ok=True)


def cmd_collect(args) -> int:
    _prepare(args.db, args.data)
    conn = db.connect(args.db)
    try:
        collector = Collector(conn, Client(args.source), Store(Path(args.data)))
        result = collector.run()
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] != "failed" else 1


def cmd_relations(args) -> int:
    _prepare(args.db, args.data)
    conn = db.connect(args.db)
    try:
        verifier = RelationVerifier(conn, Client(args.source), Store(Path(args.data)))
        result = verifier.run(limit=args.limit)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_publish(args) -> int:
    result = verify_and_publish(
        args.db,
        policy=args.policy,
        item_ids=args.item or None,
        force=args.force,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = [r for r in result["results"] if r.get("status") == "error"]
    return 1 if failed else 0


def cmd_policy(args) -> int:
    if args.set:
        set_policy(args.db, args.set)
    db.init_db(args.db)
    conn = db.connect(args.db, readonly=True)
    try:
        print(
            json.dumps(
                {"conflict_policy": db.get_setting(conn, "conflict_policy")},
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        conn.close()
    return 0


def cmd_inspect(args) -> int:
    conn = db.connect(args.db, readonly=True)
    try:
        policy = args.policy or db.get_setting(conn, "conflict_policy") or "quarantine"
        analysis = analyze_item(conn, args.item, policy)
    except KeyError:
        print(f"item not found: {args.item}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(json.dumps(analysis, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_serve(args) -> int:
    from corpus_audit.web import make_server

    db.init_db(args.db)
    server = make_server(args.db, args.data, args.port)
    print(f"catalog: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_source(args) -> int:
    from corpus_audit.source import make_server

    server = make_server(args.port)
    print(f"source: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_demo(args) -> int:
    """Start source + catalog in-process and run collect/verify/publish."""
    from corpus_audit.source import make_server as make_source
    from corpus_audit.web import make_server as make_catalog

    _prepare(args.db, args.data)
    source = make_source(0)
    threading.Thread(target=source.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{source.server_port}"
    catalog = make_catalog(args.db, args.data, args.port)
    threading.Thread(target=catalog.serve_forever, daemon=True).start()
    print(f"source:  {base}/")
    print(f"catalog: http://127.0.0.1:{catalog.server_port}/")

    conn = db.connect(args.db)
    try:
        collect = Collector(conn, Client(base), Store(Path(args.data))).run()
        print("collect:", json.dumps(collect, ensure_ascii=False))
        relations = RelationVerifier(conn, Client(base), Store(Path(args.data))).run()
        print("relations:", json.dumps(relations, ensure_ascii=False))
    finally:
        conn.close()
    published = verify_and_publish(args.db, force=True)
    print("publish:", json.dumps(published, ensure_ascii=False))
    if args.once:
        return 0
    print("press Ctrl+C to stop the servers")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        source.shutdown()
        catalog.shutdown()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="corpus-audit", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", default=DEFAULT_DB)
        p.add_argument("--data", default=DEFAULT_DATA)

    p = sub.add_parser("init", help="initialize the database")
    add_common(p)
    p.set_defaults(func=lambda a: (_prepare(a.db, a.data), 0)[1])

    p = sub.add_parser("collect", help="run the collector once")
    add_common(p)
    p.add_argument("--source", default="http://127.0.0.1:8100", help="source base URL")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("relations", help="run the relation checker")
    add_common(p)
    p.add_argument("--source", default="http://127.0.0.1:8100", help="source base URL")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_relations)

    p = sub.add_parser("publish", help="verify and publish dirty items")
    add_common(p)
    p.add_argument("--policy", choices=["quarantine", "independent"])
    p.add_argument("--item", action="append", help="only these item ids")
    p.add_argument("--force", action="store_true", help="republish unchanged items")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("policy", help="show or set the conflict policy")
    add_common(p)
    p.add_argument("--set", choices=["quarantine", "independent"])
    p.set_defaults(func=cmd_policy)

    p = sub.add_parser("inspect", help="print the analysis for one item")
    add_common(p)
    p.add_argument("item")
    p.add_argument("--policy", choices=["quarantine", "independent"])
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("serve", help="run the read-only catalog")
    add_common(p)
    p.add_argument("--port", type=int, default=8200)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("source", help="run the fictional source site")
    p.add_argument("--port", type=int, default=8100)
    p.set_defaults(func=cmd_source)

    p = sub.add_parser("demo", help="run the full local demo")
    add_common(p)
    p.add_argument("--port", type=int, default=8200)
    p.add_argument("--once", action="store_true", help="publish then exit")
    p.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
