"""Relation verifier: an independent process over queued relation jobs.

Rules
-----
* A job is tied to a source snapshot id.  If that snapshot is no longer the
  document's current one, the job is superseded: it may leave history but can
  never apply a link.  This is what stops a late-finishing old task from
  reviving a withdrawn link.
* The target is fetched fresh, not read from another process's cache, and the
  exact target snapshot is recorded in the check.
* Only checks made against the *current* source and target snapshots can be
  used by analysis/publication.
* Rejections are explicit and evidence-backed; unresolved targets stay
  pending rather than being guessed at.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from corpus_audit import db
from corpus_audit.collector import Store
from corpus_audit.fetch import Client, FetchError
from corpus_audit.parse import ParseError, parse_document

MAX_ATTEMPTS = 3


def _add(reasons: list[str], value: str) -> None:
    if value not in reasons:
        reasons.append(value)


class RelationVerifier:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: Client,
        store: Store,
        *,
        actor: str = "relation-checker",
    ) -> None:
        self.conn = conn
        self.client = client
        self.store = store
        self.actor = actor

    def pending_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM relation_jobs WHERE status IN ('queued','running')"
        ).fetchone()
        return int(row["n"])

    def run(self, limit: int = 100) -> dict[str, Any]:
        db.begin_write(self.conn)
        try:
            run_id = int(
                self.conn.execute(
                    "INSERT INTO relation_runs(started_at, status, scope) "
                    "VALUES(?, 'running', 'queued jobs')",
                    (db.utcnow(),),
                ).lastrowid
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        applied = blocked = processed = 0
        failed = False
        attempted: set[int] = set()
        try:
            while processed < limit:
                if attempted:
                    placeholders = ",".join("?" for _ in attempted)
                    job = self.conn.execute(
                        "SELECT * FROM relation_jobs WHERE status='queued' "
                        f"AND job_id NOT IN ({placeholders}) ORDER BY job_id LIMIT 1",
                        tuple(attempted),
                    ).fetchone()
                else:
                    job = self.conn.execute(
                        "SELECT * FROM relation_jobs WHERE status='queued' "
                        "ORDER BY job_id LIMIT 1"
                    ).fetchone()
                if job is None:
                    break
                attempted.add(job["job_id"])
                processed += 1
                result = self.process_job(job)
                applied += 1 if result["applied"] else 0
                blocked += 1 if result["blocked"] else 0
                self.conn.commit()
        except Exception:
            failed = True
            self.conn.rollback()
            raise
        finally:
            db.begin_write(self.conn)
            try:
                self.conn.execute(
                    "UPDATE relation_runs SET finished_at=?, status=?, applied=?, blocked=? "
                    "WHERE run_id=?",
                    (
                        db.utcnow(),
                        "failed" if failed else "ok",
                        applied,
                        blocked,
                        run_id,
                    ),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
        return {
            "run_id": run_id,
            "processed": processed,
            "applied": applied,
            "blocked": blocked,
        }

    # ------------------------------------------------------------------ jobs
    def process_job(self, job: sqlite3.Row) -> dict[str, Any]:
        current = self.conn.execute(
            "SELECT current_snapshot_id FROM documents WHERE doc_id=?",
            (job["source_doc"],),
        ).fetchone()
        if current is None:
            self.finish_job(job, "skipped", "source_document_missing")
            return {"applied": False, "blocked": False}
        if current["current_snapshot_id"] != job["source_snapshot_id"]:
            self.conn.execute(
                "UPDATE relation_jobs SET started_at=COALESCE(started_at, ?) WHERE job_id=?",
                (db.utcnow(), job["job_id"]),
            )
            self.finish_job(job, "superseded", "newer_source_snapshot_exists")
            db.audit(
                self.conn,
                self.actor,
                "relation.job_superseded",
                {
                    "target_url": job["target_url"],
                    "queued_snapshot": job["source_snapshot_id"],
                    "current_snapshot": current["current_snapshot_id"],
                },
                item_id=job["item_id"],
                doc_id=job["source_doc"],
            )
            return {"applied": False, "blocked": False}

        snapshot = self.conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?",
            (job["source_snapshot_id"],),
        ).fetchone()
        if snapshot is None or snapshot["doc_id"] != job["source_doc"]:
            self.finish_job(job, "skipped", "source_snapshot_missing")
            return {"applied": False, "blocked": False}

        self.conn.execute(
            "UPDATE relation_jobs SET status='running', started_at=? WHERE job_id=?",
            (db.utcnow(), job["job_id"]),
        )
        self.conn.commit()

        reasons: list[str] = []
        evidence: dict[str, Any] = {
            "kind": job["kind"],
            "source_url": snapshot["url"],
            "source_document_id": json.loads(snapshot["parsed_json"]).get("document_id"),
            "source_language": snapshot["language"],
            "source_role": snapshot["role"],
            "source_version": snapshot["version"],
            "source_snapshot_id": snapshot["snapshot_id"],
            "target_url": job["target_url"],
        }
        target_doc = None
        target_snapshot = None
        parsed = None
        fetched = None
        try:
            fetched = self.client.get(job["target_url"])
        except FetchError as exc:
            _add(reasons, f"target_fetch_failed:{exc.code}")
            evidence["fetch_error"] = {"code": exc.code, "message": exc.message}
            self.record_evidence_failure(job, exc)

        if fetched is not None and fetched.body is not None:
            self.record_evidence(job, fetched, None)
            try:
                parsed = parse_document(
                    fetched.body.decode("utf-8", errors="replace"), fetched.url
                )
            except ParseError as exc:
                _add(reasons, f"target_parse_failed:{exc.code}")
                evidence["parse_error"] = {"code": exc.code, "message": exc.message}
            if parsed is not None:
                evidence.update(
                    {
                        "target_document_id": parsed.document_id,
                        "target_item_id": parsed.item_id,
                        "target_language": parsed.language,
                        "target_role": parsed.role,
                        "target_version": parsed.version,
                        "target_status": parsed.status,
                        "target_sha256": fetched.sha256,
                        "target_http_status": fetched.status,
                    }
                )
                self.record_evidence(job, fetched, parsed)
                target_row = self.conn.execute(
                    "SELECT doc_id, item_id, current_snapshot_id FROM documents WHERE url=?",
                    (job["target_url"],),
                ).fetchone()
                if target_row is not None:
                    target_doc = target_row["doc_id"]
                    target_snapshot = target_row["current_snapshot_id"]
                    evidence["target_doc_id"] = target_doc
                    snap_row = self.conn.execute(
                        "SELECT body_sha256 FROM snapshots WHERE snapshot_id=?",
                        (target_snapshot,),
                    ).fetchone()
                    if snap_row is not None and snap_row["body_sha256"] != fetched.sha256:
                        # The target page changed since the catalog collected it.
                        # Keep the evidence but do not tie the check to the
                        # current target snapshot; analysis will demand a
                        # re-check against the new snapshot.
                        evidence["target_changed_since_collection"] = True
                        target_snapshot = None
                else:
                    evidence["target_collected"] = False
                self.evaluate(job, snapshot, parsed, reasons, evidence)
                evidence["reasons"] = reasons
        if not reasons and parsed is None:
            _add(reasons, "no_target_evidence")
        # Unresolved-target outcomes are pending, not rejections: a transient
        # 5xx or a network failure must not permanently kill a relation.
        # The job is requeued with a bound number of attempts so a re-run of
        # the checker picks it up again.
        if reasons and all(
            reason.startswith(("target_fetch_failed:", "target_parse_failed:"))
            or reason == "no_target_evidence"
            for reason in reasons
        ):
            state = "pending"
        else:
            state = "verified" if not reasons else "rejected"
        attempts = int(job["attempts"]) + 1
        if state == "pending" and attempts >= MAX_ATTEMPTS:
            evidence["retry_exhausted"] = True
        evidence["reasons"] = reasons
        evidence["attempts"] = attempts
        self.record_check(
            job,
            state,
            reasons,
            evidence,
            target_doc=target_doc,
            target_snapshot=target_snapshot,
        )
        if state == "pending":
            if attempts >= MAX_ATTEMPTS:
                self.finish_job(
                    job, "exhausted", f"attempts={attempts}; " + ",".join(reasons)[:900]
                )
            else:
                # Back to the queue for a later checker run; this run has
                # already tried it once and must not spin on it.
                self.finish_job(
                    job, "queued", f"attempt {attempts}: " + ",".join(reasons)[:900]
                )
            self.conn.execute(
                "UPDATE relation_jobs SET attempts=? WHERE job_id=?",
                (attempts, job["job_id"]),
            )
            self.conn.commit()
            return {"applied": False, "blocked": False}
        self.finish_job(job, "done", None if not reasons else ",".join(reasons)[:1000])
        self.conn.commit()
        return {"applied": state == "verified", "blocked": state != "verified"}

    # ------------------------------------------------------------- evaluation
    def evaluate(
        self,
        job: sqlite3.Row,
        snapshot: sqlite3.Row,
        parsed,
        reasons: list[str],
        evidence: dict[str, Any],
    ) -> None:
        kind = job["kind"]
        source_role = snapshot["role"]
        source_language = snapshot["language"]
        if kind not in ("replaces", "replaced_by", "translation_of"):
            _add(reasons, "unknown_relation_kind")
            return
        if parsed.item_id != job["item_id"]:
            _add(reasons, "item_mismatch")
        if parsed.document_id and parsed.document_id == evidence.get("source_document_id"):
            _add(reasons, "self_relation")
        if kind == "translation_of":
            if parsed.language == source_language:
                _add(reasons, "same_language")
        elif parsed.version == snapshot["version"] and parsed.language == source_language:
            # Same-language replacement that keeps the same version number is
            # ambiguous: there is no way to tell which one is current.
            _add(reasons, "same_version")
        if kind == "translation_of":
            if source_role != "translation":
                _add(reasons, "source_is_not_translation")
            if parsed.role != "original":
                _add(reasons, "target_is_not_original")
            if parsed.status != "active":
                _add(reasons, "target_withdrawn")
            declared = snapshot["translation_of"]
            if not declared:
                _add(reasons, "source_missing_translation_of")
            elif declared != job["target_url"]:
                _add(reasons, "declaration_target_mismatch")
            declared_version = snapshot["source_version"]
            if not isinstance(declared_version, int):
                _add(reasons, "declared_source_version_missing")
            else:
                target_version = self._as_int(parsed.version)
                if target_version is None:
                    _add(reasons, "target_version_not_integer")
                elif declared_version > target_version:
                    _add(reasons, "declared_source_version_ahead")
            return

        # replacement relations
        if parsed.role != source_role:
            _add(reasons, "role_mismatch")
        if kind == "replaced_by" and parsed.status == "withdrawn" and snapshot["status"] == "active":
            # An active version may not claim that a withdrawn page replaces it.
            _add(reasons, "replacement_target_withdrawn")
        # Keep language homogeneous: replacing a zh document with an en page
        # would silently change which language the newer version is in.
        if parsed.language != source_language:
            _add(reasons, "replacement_language_mismatch")

        # Reciprocal declaration: desirable, recorded, but a source may
        # legitimately publish only one side.  Weak evidence is surfaced as a
        # warning in the analysis layer, not as a rejection here.
        if kind == "replaces":
            reciprocals = {self._norm(url) for url in parsed.replaced_by}
        else:
            reciprocals = {self._norm(url) for url in parsed.replaces}
        evidence["reciprocal"] = self._norm(job["target_url"]) in reciprocals
        evidence["evidence_strength"] = 2 if evidence["reciprocal"] else 1

    @staticmethod
    def _as_int(value: str) -> int | None:
        return int(value) if value.isdigit() else None

    @staticmethod
    def _norm(url: str | None) -> str:
        return (url or "").rstrip("/")

    # --------------------------------------------------------------- records
    def record_evidence(self, job, fetched, parsed) -> None:
        parsed_json = None
        if parsed is not None:
            parsed_json = json.dumps(
                {
                    "document_id": parsed.document_id,
                    "item_id": parsed.item_id,
                    "language": parsed.language,
                    "role": parsed.role,
                    "version": parsed.version,
                    "status": parsed.status,
                    "translation_of": parsed.translation_of,
                    "replaces": parsed.replaces,
                    "replaced_by": parsed.replaced_by,
                },
                ensure_ascii=False,
            )
        raw_path = None
        if fetched.body is not None:
            raw_path = self.store.save_evidence(job["source_doc"], job["job_id"], fetched.body)
        self.conn.execute(
            "INSERT INTO relation_evidence(source_doc, target_url, http_status, sha256, "
            "raw_path, parsed_json, error, fetched_at) VALUES(?, ?, ?, ?, ?, ?, NULL, ?) "
            "ON CONFLICT(source_doc, target_url) DO UPDATE SET http_status=excluded.http_status, "
            "sha256=excluded.sha256, raw_path=excluded.raw_path, parsed_json=excluded.parsed_json, "
            "error=NULL, fetched_at=excluded.fetched_at",
            (
                job["source_doc"],
                job["target_url"],
                fetched.status,
                fetched.sha256,
                raw_path,
                parsed_json,
                db.utcnow(),
            ),
        )

    def record_evidence_failure(self, job, exc: FetchError) -> None:
        self.conn.execute(
            "INSERT INTO relation_evidence(source_doc, target_url, http_status, sha256, "
            "raw_path, parsed_json, error, fetched_at) VALUES(?, ?, ?, NULL, NULL, NULL, ?, ?) "
            "ON CONFLICT(source_doc, target_url) DO UPDATE SET http_status=excluded.http_status, "
            "sha256=NULL, raw_path=NULL, parsed_json=NULL, error=excluded.error, "
            "fetched_at=excluded.fetched_at",
            (
                job["source_doc"],
                job["target_url"],
                exc.status,
                json.dumps({"code": exc.code, "message": exc.message}, ensure_ascii=False),
                db.utcnow(),
            ),
        )

    def record_check(
        self,
        job,
        state: str,
        reasons: list[str],
        evidence: dict[str, Any],
        *,
        target_doc: str | None,
        target_snapshot: int | None,
    ) -> None:
        previous = self.conn.execute(
            "SELECT state FROM relation_checks WHERE kind=? AND source_doc=? "
            "AND source_snapshot_id=? AND target_url=? ORDER BY check_id DESC LIMIT 1",
            (
                job["kind"],
                job["source_doc"],
                job["source_snapshot_id"],
                job["target_url"],
            ),
        ).fetchone()
        self.conn.execute(
            "INSERT INTO relation_checks(job_id, item_id, kind, source_doc, "
            "source_snapshot_id, target_url, target_doc, target_snapshot_id, state, "
            "reasons_json, evidence_json, checked_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job["job_id"],
                job["item_id"],
                job["kind"],
                job["source_doc"],
                job["source_snapshot_id"],
                job["target_url"],
                target_doc,
                target_snapshot,
                state,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False, default=str),
                db.utcnow(),
            ),
        )
        db.audit(
            self.conn,
            self.actor,
            "relation.checked",
            {
                "state": state,
                "kind": job["kind"],
                "target_url": job["target_url"],
                "source_snapshot_id": job["source_snapshot_id"],
                "reasons": reasons,
            },
            item_id=job["item_id"],
            doc_id=job["source_doc"],
        )
        # Only a change in the verdict needs a republish.  Otherwise a checker
        # re-run would needlessly mark every item dirty and defeat the
        # "advance only affected items" rule.
        if previous is None or previous["state"] != state:
            db.touch_item(self.conn, job["item_id"])

    def finish_job(self, job, status: str, reason: str | None) -> None:
        self.conn.execute(
            "UPDATE relation_jobs SET status=?, finished_at=?, reason=? WHERE job_id=?",
            (status, db.utcnow(), reason, job["job_id"]),
        )


def verify_pending(
    db_path: Path | str,
    base_url: str,
    data_root: Path | str,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    conn = db.connect(db_path)
    try:
        verifier = RelationVerifier(conn, Client(base_url), Store(Path(data_root)))
        return verifier.run(limit=limit)
    finally:
        conn.close()
