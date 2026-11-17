"""Verification and publication.

Both steps are pure reads of the database plus the shared analysis module.
Publication only advances items whose dirty watermark moved, so fixing one
item never republishes unrelated groups.

Conflict policies
-----------------
``quarantine``   A group with any conflict is not offered for use; the public
                 page still shows the conflict, the source URLs and evidence
                 so the group can be fixed, but no language is presented as
                 current.
``independent``  Per-language records stay available; only verified relations
                 are exposed and conflicting relations are shown as disputed.
                 Completeness of correspondence drops, availability is kept.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from corpus_audit import db
from corpus_audit.analyze import POLICIES, analyze_item

ACTOR = "publisher"


def _dirty_items(conn: sqlite3.Connection, *, force: bool) -> list[sqlite3.Row]:
    condition = "" if force else (
        "WHERE COALESCE(st.dirty_seq, 0) > COALESCE(st.published_seq, 0)"
    )
    return conn.execute(
        f"""
        SELECT i.item_id, i.title, COALESCE(st.dirty_seq, 0) AS dirty_seq,
               COALESCE(st.published_seq, 0) AS published_seq
        FROM items i
        LEFT JOIN item_state st ON st.item_id = i.item_id
        {condition}
        ORDER BY i.item_id
        """
    ).fetchall()


def _payload_hash(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def verify_and_publish(
    db_path: str | Path,
    *,
    policy: str | None = None,
    item_ids: list[str] | None = None,
    force: bool = False,
    actor: str = ACTOR,
) -> dict[str, Any]:
    init_needed = not Path(db_path).exists()
    if init_needed:
        db.init_db(db_path)
    conn = db.connect(db_path)
    results: list[dict[str, Any]] = []
    run_id: int | None = None
    try:
        policy = policy or db.get_setting(conn, "conflict_policy") or "quarantine"
        if policy not in POLICIES:
            raise ValueError(f"unknown conflict policy {policy!r}")
        stored_policy = db.get_setting(conn, "conflict_policy")
        if policy != stored_policy:
            # Every group's presentation depends on the policy; mark all of
            # them changed so the new rules are actually applied.
            db.begin_write(conn)
            try:
                db.set_setting(conn, "conflict_policy", policy)
                for item_row in conn.execute("SELECT item_id FROM items"):
                    db.touch_item(conn, item_row["item_id"])
                db.audit(
                    conn,
                    actor,
                    "policy.applied",
                    {"from": stored_policy, "to": policy},
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        db.begin_write(conn)
        try:
            run_id = int(
                conn.execute(
                    "INSERT INTO verification_runs(started_at, status, scope, policy) "
                    "VALUES(?, 'running', ?, ?)",
                    (db.utcnow(), "explicit" if item_ids else "dirty", policy),
                ).lastrowid
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        candidates: list[sqlite3.Row] = []
        if item_ids:
            for item_id in item_ids:
                row = conn.execute(
                    "SELECT i.item_id, i.title, COALESCE(st.dirty_seq, 0) AS dirty_seq, "
                    "COALESCE(st.published_seq, 0) AS published_seq FROM items i "
                    "LEFT JOIN item_state st ON st.item_id=i.item_id WHERE i.item_id=?",
                    (item_id,),
                ).fetchone()
                if row is None:
                    results.append({"item_id": item_id, "status": "not_found"})
                else:
                    candidates.append(row)
        else:
            candidates = list(_dirty_items(conn, force=force))

        affected: list[str] = []
        for row in candidates:
            item_id = row["item_id"]
            if not force and row["dirty_seq"] <= row["published_seq"]:
                results.append({"item_id": item_id, "status": "unchanged"})
                continue
            try:
                analysis = analyze_item(conn, item_id, policy)
                outcome = publish_item(
                    conn,
                    analysis,
                    run_id=run_id,
                    policy=policy,
                    actor=actor,
                    force=force,
                )
                results.append(outcome)
                affected.append(item_id)
            except Exception as exc:
                results.append({"item_id": item_id, "status": "error", "error": str(exc)})

        db.begin_write(conn)
        try:
            conn.execute(
                "UPDATE verification_runs SET finished_at=?, status=?, affected_items=? "
                "WHERE run_id=?",
                (
                    db.utcnow(),
                    "ok",
                    json.dumps(affected, ensure_ascii=False),
                    run_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    return {"run_id": run_id, "policy": policy, "results": results}


def publish_item(
    conn: sqlite3.Connection,
    analysis: dict[str, Any],
    *,
    run_id: int,
    policy: str,
    actor: str,
    force: bool,
) -> dict[str, Any]:
    item_id = analysis["item_id"]
    state = conn.execute(
        "SELECT * FROM item_state WHERE item_id=?", (item_id,)
    ).fetchone()
    revision = (state["published_rev"] if state else 0) + 1

    blocked = policy == "quarantine" and analysis["state"] == "conflict"
    if blocked:
        public_state = "blocked"
    elif analysis["state"] == "conflict":
        public_state = "conflict_independent"
    elif analysis["state"] == "degraded":
        public_state = "degraded"
    else:
        public_state = "ok"

    payload = {
        "item_id": item_id,
        "title": analysis["title"],
        "original_language": analysis["original_language"],
        "public_state": public_state,
        "policy": policy,
        "generated_at": db.utcnow(),
        "verification_run_id": run_id,
        "revision": revision,
        "conflicts": analysis["conflicts"],
        "warnings": analysis["warnings"],
        "findings": analysis["findings"],
        "languages": analysis["languages"] if not blocked else [],
        "documents": analysis["documents"],
        "relation_counts": analysis["relation_counts"],
        "impact": analysis["impact"]
        or {
            "availability": "资料可用；关系核对未发现冲突。",
            "completeness": "通过核对的关系已展示；带警告的关系会注明证据强度。",
            "action": "无需处理。",
        },
        "availability": analysis["availability"],
    }

    # Persist findings for this run (replacing this item's previous findings is
    # unnecessary: findings are historical, publication embeds the latest set).
    db.begin_write(conn)
    try:
        conn.execute(
            "DELETE FROM findings WHERE run_id=? AND item_id=?", (run_id, item_id)
        )
        now = db.utcnow()
        for finding in analysis["findings"]:
            conn.execute(
                "INSERT INTO findings(run_id, item_id, doc_id, code, severity, message, "
                "evidence, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    item_id,
                    finding.get("doc_id"),
                    finding["code"],
                    finding["severity"],
                    finding["message"],
                    json.dumps(finding["evidence"], ensure_ascii=False, default=str),
                    now,
                ),
            )

        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        digest = _payload_hash(payload)
        verified_seq = int(
            conn.execute(
                "SELECT COALESCE(MAX(value), '0') AS v FROM meta WHERE key='sequence'"
            ).fetchone()["v"]
        )
        conn.execute(
            "INSERT INTO publications(item_id, revision, state, policy, payload_sha, "
            "payload_json, verified_seq, published_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET revision=excluded.revision, "
            "state=excluded.state, policy=excluded.policy, payload_sha=excluded.payload_sha, "
            "payload_json=excluded.payload_json, verified_seq=excluded.verified_seq, "
            "published_at=excluded.published_at",
            (
                item_id,
                revision,
                public_state,
                policy,
                digest,
                payload_json,
                verified_seq,
                now,
            ),
        )
        dirty_seq = int(
            conn.execute(
                "SELECT COALESCE(dirty_seq, 0) AS v FROM item_state WHERE item_id=?",
                (item_id,),
            ).fetchone()["v"]
        )
        conn.execute(
            "UPDATE item_state SET published_seq=?, published_rev=?, public_state=?, "
            "last_published_at=?, verified_seq=?, last_verified_run=?, last_verified_at=? "
            "WHERE item_id=?",
            (
                dirty_seq,
                revision,
                public_state,
                now,
                verified_seq,
                run_id,
                now,
                item_id,
            ),
        )
        db.audit(
            conn,
            actor,
            "item.published",
            {
                "revision": revision,
                "state": public_state,
                "policy": policy,
                "conflicts": analysis["conflicts"],
                "warnings": analysis["warnings"],
                "payload_sha": digest,
            },
            item_id=item_id,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "item_id": item_id,
        "status": "blocked" if blocked else "published",
        "public_state": public_state,
        "revision": revision,
        "conflicts": analysis["conflicts"],
        "warnings": analysis["warnings"],
        "payload_sha": digest,
    }


def set_policy(db_path: str | Path, policy: str, *, actor: str = "admin") -> None:
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}; choose from {', '.join(POLICIES)}")
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        db.begin_write(conn)
        try:
            old = db.get_setting(conn, "conflict_policy")
            db.set_setting(conn, "conflict_policy", policy)
            if old != policy:
                # Policy changes presentation for every group, so every item
                # needs to be re-published under the new rules.
                for row in conn.execute("SELECT item_id FROM items"):
                    db.touch_item(conn, row["item_id"])
            db.audit(conn, actor, "policy.changed", {"from": old, "to": policy})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
