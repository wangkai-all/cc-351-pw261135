"""SQLite storage for the multilingual corpus audit.

The collector, relation checker and public catalog are separate processes.
Every connection uses WAL with a busy timeout; writers take BEGIN IMMEDIATE
so the collector, relation checker and publish step cannot interleave.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    item_id           TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    original_language TEXT NOT NULL,
    url               TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    doc_id              TEXT PRIMARY KEY,
    item_id             TEXT NOT NULL,
    language            TEXT NOT NULL,
    role                TEXT NOT NULL,
    url                 TEXT NOT NULL UNIQUE,
    currently_listed    INTEGER NOT NULL DEFAULT 1,
    discovered_via      TEXT NOT NULL DEFAULT 'catalog',
    current_snapshot_id INTEGER,
    first_seen_seq      INTEGER NOT NULL,
    last_seen_seq       INTEGER NOT NULL,
    last_checked_at     TEXT,
    last_http_status    INTEGER,
    last_etag           TEXT,
    last_modified       TEXT,
    last_error          TEXT,
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_item ON documents(item_id);
CREATE INDEX IF NOT EXISTS idx_documents_language ON documents(language);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id         TEXT NOT NULL,
    item_id        TEXT NOT NULL,
    url            TEXT NOT NULL,
    language       TEXT NOT NULL,
    role           TEXT NOT NULL,
    title          TEXT NOT NULL,
    version        TEXT NOT NULL,
    status         TEXT NOT NULL,
    translation_of TEXT,
    source_version INTEGER,
    observed_seq   INTEGER NOT NULL UNIQUE,
    fetched_at     TEXT NOT NULL,
    http_status    INTEGER NOT NULL,
    etag           TEXT,
    last_modified  TEXT,
    body_sha256    TEXT NOT NULL,
    raw_path       TEXT NOT NULL,
    parsed_json    TEXT NOT NULL,
    FOREIGN KEY (doc_id) REFERENCES documents(doc_id)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_doc ON snapshots(doc_id, observed_seq);
CREATE INDEX IF NOT EXISTS idx_snapshots_status ON snapshots(status);
CREATE INDEX IF NOT EXISTS idx_snapshots_item ON snapshots(item_id, observed_seq);

CREATE TABLE IF NOT EXISTS relation_evidence (
    source_doc   TEXT NOT NULL,
    target_url   TEXT NOT NULL,
    http_status  INTEGER,
    sha256       TEXT,
    raw_path     TEXT,
    parsed_json  TEXT,
    error        TEXT,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (source_doc, target_url)
);

CREATE TABLE IF NOT EXISTS relation_jobs (
    job_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id            TEXT NOT NULL,
    kind               TEXT NOT NULL,
    source_doc         TEXT NOT NULL,
    source_snapshot_id INTEGER NOT NULL,
    target_url         TEXT NOT NULL,
    input_seq          INTEGER NOT NULL,
    attempts           INTEGER NOT NULL DEFAULT 0,
    status             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    reason             TEXT,
    FOREIGN KEY (source_doc) REFERENCES documents(doc_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON relation_jobs(status, job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON relation_jobs(source_doc);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_queued_unique
    ON relation_jobs(source_snapshot_id, kind, target_url) WHERE status = 'queued';

CREATE TABLE IF NOT EXISTS relation_checks (
    check_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             INTEGER NOT NULL,
    item_id            TEXT NOT NULL,
    kind               TEXT NOT NULL,
    source_doc         TEXT NOT NULL,
    source_snapshot_id INTEGER NOT NULL,
    target_url         TEXT NOT NULL,
    target_doc         TEXT,
    target_snapshot_id INTEGER,
    state              TEXT NOT NULL,
    reasons_json       TEXT NOT NULL,
    evidence_json      TEXT NOT NULL,
    checked_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_target
    ON relation_checks(target_doc, target_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_checks_source ON relation_checks(source_doc, source_snapshot_id);
CREATE INDEX IF NOT EXISTS idx_checks_item ON relation_checks(item_id);

CREATE TABLE IF NOT EXISTS item_state (
    item_id           TEXT PRIMARY KEY,
    dirty_seq         INTEGER NOT NULL DEFAULT 0,
    verified_seq      INTEGER,
    published_seq     INTEGER,
    published_rev     INTEGER NOT NULL DEFAULT 0,
    public_state      TEXT,
    last_verified_at  TEXT,
    last_published_at TEXT,
    last_verified_run INTEGER,
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE TABLE IF NOT EXISTS collect_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    seen_items  INTEGER NOT NULL DEFAULT 0,
    seen_docs   INTEGER NOT NULL DEFAULT 0,
    new_snapshots INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fetch_errors (
    run_id      INTEGER NOT NULL,
    item_id     TEXT,
    doc_id      TEXT,
    url         TEXT NOT NULL,
    code        TEXT NOT NULL,
    message     TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES collect_runs(run_id)
);

CREATE TABLE IF NOT EXISTS collect_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id   INTEGER NOT NULL,
    item_id  TEXT,
    doc_id   TEXT,
    url      TEXT,
    code     TEXT NOT NULL,
    message  TEXT NOT NULL,
    detail   TEXT NOT NULL,
    at       TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES collect_runs(run_id)
);

CREATE TABLE IF NOT EXISTS withdrawal_evidence (
    snapshot_id INTEGER PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    url         TEXT NOT NULL,
    http_status INTEGER,
    sha256      TEXT,
    raw_path    TEXT,
    fetched_at  TEXT NOT NULL,
    error       TEXT,
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
);

CREATE TABLE IF NOT EXISTS relation_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    scope       TEXT NOT NULL,
    applied     INTEGER NOT NULL DEFAULT 0,
    blocked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS verification_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    scope        TEXT NOT NULL,
    policy       TEXT NOT NULL,
    affected_items TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS findings (
    finding_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL,
    item_id     TEXT NOT NULL,
    doc_id      TEXT,
    code        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    evidence    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES verification_runs(run_id)
);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
CREATE INDEX IF NOT EXISTS idx_findings_item ON findings(item_id);

CREATE TABLE IF NOT EXISTS publications (
    item_id      TEXT PRIMARY KEY,
    revision     INTEGER NOT NULL,
    state        TEXT NOT NULL,
    policy       TEXT NOT NULL,
    payload_sha  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    verified_seq INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    item_id  TEXT,
    doc_id   TEXT,
    detail   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_item ON audit_log(item_id);
CREATE INDEX IF NOT EXISTS idx_audit_doc ON audit_log(doc_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: str | Path, *, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        conn = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro", uri=True, timeout=20.0
        )
    else:
        conn = sqlite3.connect(str(path), timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 20000")
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        with conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES('conflict_policy', 'quarantine')"
            )
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def next_sequence(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(value), '0') AS v FROM meta WHERE key = 'sequence'"
    ).fetchone()
    value = int(row["v"]) + 1
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('sequence', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(value),),
    )
    return value


def audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    detail: dict[str, Any] | str,
    *,
    item_id: str | None = None,
    doc_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, item_id, doc_id, detail) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (
            utcnow(),
            actor,
            action,
            item_id,
            doc_id,
            detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False),
        ),
    )


def touch_item(conn: sqlite3.Connection, item_id: str, seq: int | None = None) -> int:
    """Mark an item for re-verification/publication using the global sequence."""
    seq = seq if seq is not None else next_sequence(conn)
    conn.execute(
        "INSERT INTO item_state(item_id, dirty_seq) VALUES(?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET dirty_seq = MAX(dirty_seq, excluded.dirty_seq)",
        (item_id, seq),
    )
    return seq


def begin_write(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
