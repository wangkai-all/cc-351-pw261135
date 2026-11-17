"""End-to-end tests over a controllable local source.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from corpus_audit import db  # noqa: E402
from corpus_audit.analyze import analyze_item  # noqa: E402
from corpus_audit.collector import Collector, Store  # noqa: E402
from corpus_audit.fetch import Client  # noqa: E402
from corpus_audit.parse import parse_document  # noqa: E402
from corpus_audit.publish import verify_and_publish  # noqa: E402
from corpus_audit.relations import RelationVerifier  # noqa: E402
from corpus_audit.source import (  # noqa: E402
    Doc,
    Item,
    SiteState,
    build_fixture,
    make_server,
)
from corpus_audit.web import make_server as make_catalog  # noqa: E402


class SiteCase(unittest.TestCase):
    """Each test gets a fresh source site, database and archive directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "catalog.db"
        self.data = self.root / "archive"
        self.source = make_server(0)
        self.base = f"http://127.0.0.1:{self.source.server_port}"
        self.thread = threading.Thread(target=self.source.serve_forever, daemon=True)
        self.thread.start()
        db.init_db(self.db_path)

    def tearDown(self) -> None:
        self.source.shutdown()
        self.source.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    @property
    def state(self) -> SiteState:
        return self.source.state  # type: ignore[attr-defined]

    def connect(self):
        return db.connect(self.db_path)

    def collect(self) -> dict:
        conn = self.connect()
        try:
            return Collector(conn, Client(self.base), Store(self.data)).run()
        finally:
            conn.close()

    def relations(self, limit: int = 100) -> dict:
        conn = self.connect()
        try:
            return RelationVerifier(conn, Client(self.base), Store(self.data)).run(limit=limit)
        finally:
            conn.close()

    def publish(self, **kwargs) -> dict:
        return verify_and_publish(self.db_path, **kwargs)

    def run_all(self, **kwargs) -> dict:
        self.collect()
        self.relations()
        return self.publish(force=True, **kwargs)

    def analysis(self, item_id: str, policy: str = "quarantine") -> dict:
        conn = self.connect()
        try:
            return analyze_item(conn, item_id, policy)
        finally:
            conn.close()

    def fetch_page(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(self.base + path) as response:
            return response.status, response.read().decode()

    def get(self, item_id: str, doc_id: str) -> Doc:
        return self.state.items[item_id].doc(doc_id)

    def add_doc(self, item_id: str, doc: Doc) -> None:
        with self.state.lock:
            self.state.items[item_id].documents.append(doc)
            self.state.etag_version += 1

    def codes(self, item_id: str, policy: str = "quarantine") -> set[str]:
        return {f["code"] for f in self.analysis(item_id, policy)["findings"]}


class ParsingTests(SiteCase):
    def test_metadata_is_parsed_from_jsonld(self):
        _, html = self.fetch_page("/items/harbor-charter/harbor-charter-en")
        meta = parse_document(html, self.base + "/items/harbor-charter/harbor-charter-en")
        self.assertEqual(meta.language, "en")
        self.assertEqual(meta.role, "translation")
        self.assertEqual(meta.translation_source_version, 3)
        self.assertTrue(meta.translation_of.endswith("/harbor-charter-zh"))

    def test_invalid_metadata_is_rejected_not_guessed(self):
        doc = self.get("tide-tables", "tide-tables-de")
        doc.extra_jsonld = json.dumps(
            {"@type": "CorpusDocument", "documentId": "dup", "itemId": "tide-tables"}
        )
        # Duplicate JSON-LD blocks are ambiguous by construction.
        _, html = self.fetch_page("/items/tide-tables/tide-tables-de")
        with self.assertRaises(Exception):
            parse_document(html, self.base + "/items/tide-tables/tide-tables-de")


class PublicationFlowTests(SiteCase):
    def test_full_flow_publishes_and_is_idempotent(self):
        result = self.run_all()
        states = {r["item_id"]: r["public_state"] for r in result["results"]}
        self.assertEqual(states["harbor-charter"], "ok")
        self.assertEqual(states["signal-manual"], "degraded")
        again = self.publish()
        self.assertTrue(all(r["status"] == "unchanged" for r in again["results"]))

    def test_withdrawal_and_replacement_are_both_visible(self):
        self.run_all()
        analysis = self.analysis("signal-manual")
        docs = {d["doc_id"]: d for d in analysis["documents"]}
        old = docs["doc-signal-manual-fr"]
        new = docs["doc-signal-manual-fr-v2"]
        self.assertEqual(old["state"], "withdrawn")
        self.assertIn("来源撤回该译文", old["withdrawal"]["reason"])
        self.assertEqual(old["withdrawal"]["basis_snapshot_id"], old["snapshot_id"])
        self.assertTrue(old["withdrawal"]["archived"])
        types = {(v["kind"], v["relation_state"]) for v in old["replacements"]}
        self.assertIn(("replaced_by", "verified"), types)
        types_new = {(v["kind"], v["relation_state"]) for v in new["replacements"]}
        self.assertIn(("replaces", "verified"), types_new)
        langs = {lang["language"]: lang for lang in analysis["languages"]}
        self.assertIsNone(langs["fr"]["original"])
        active_fr = [t for t in langs["fr"]["translations"] if t["state"] == "active"]
        self.assertEqual([d["doc_id"] for d in active_fr], ["doc-signal-manual-fr-v2"])
        self.assertEqual(
            [t["state"] for t in langs["fr"]["translations"]].count("withdrawn"), 1
        )

    def test_translation_update_is_not_an_original_update(self):
        """Bumping a translation must leave the original row untouched."""
        self.run_all()
        before = self.analysis("harbor-charter")
        english = self.get("harbor-charter", "harbor-charter-en")
        english.version = "3"
        english.translation_source_version = 3
        self.collect()
        self.relations()
        after = self.analysis("harbor-charter")
        originals_before = [
            d for d in before["documents"] if d["role"] == "original"
        ]
        originals_after = [d for d in after["documents"] if d["role"] == "original"]
        self.assertEqual(originals_before[0]["snapshot_id"], originals_after[0]["snapshot_id"])
        self.assertEqual(originals_before[0]["version"], originals_after[0]["version"])
        translations = [d for d in after["documents"] if d["doc_id"] == "doc-harbor-charter-en"]
        self.assertEqual(translations[0]["version"], "3")
        self.assertEqual(translations[0]["translation"]["target_version"], "3")

    def test_translation_pointing_at_translation_is_a_chain_conflict(self):
        self.run_all()
        doc = self.get("harbor-charter", "harbor-charter-fr")
        doc.translation_of = "/items/harbor-charter/harbor-charter-en"
        doc.translation_source_version = 2
        result = self.collect()
        self.assertEqual(result["status"], "ok")
        self.relations()
        analysis = self.analysis("harbor-charter")
        self.assertIn("translation.chain", {f["code"] for f in analysis["findings"]})
        self.assertEqual(analysis["state"], "conflict")

    def test_old_relation_task_cannot_revive_withdrawn_link(self):
        """A queued job for an old snapshot must be superseded, not applied."""
        self.collect()
        conn = self.connect()
        old_snapshot = None
        old_job = None
        try:
            # Queue a job for the withdrawn French page pointing at the old
            # French page, then let the source publish a newer version under
            # the same URL before the worker runs.
            current_fr = conn.execute(
                "SELECT current_snapshot_id FROM documents WHERE doc_id='doc-signal-manual-fr'"
            ).fetchone()[0]
            source = conn.execute(
                "SELECT * FROM snapshots WHERE snapshot_id=?", (current_fr,)
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO relation_jobs(item_id, kind, source_doc, source_snapshot_id, "
                "target_url, input_seq, status, created_at) "
                "VALUES('signal-manual', 'replaced_by', 'doc-signal-manual-fr', ?, ?, ?, "
                "'queued', ?)",
                (
                    current_fr,
                    "http://127.0.0.1:1/items/signal-manual/signal-manual-fr-v2",
                    db.next_sequence(conn),
                    db.utcnow(),
                ),
            )
            old_job = int(cur.lastrowid)
            old_snapshot = current_fr
            conn.commit()
            fr = self.get("signal-manual", "signal-manual-fr")
            fr.version = "3"
            fr.withdrawn_at = "2026-11-01"
            fr.replacement_url = None
            fr.replaced_by = []
            self.collect()
        finally:
            conn.close()
        self.relations()
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT status, reason FROM relation_jobs WHERE job_id=?", (old_job,)
            ).fetchone()
            self.assertEqual(row["status"], "superseded")
            # Even if a worker had managed to write a verified check for the
            # old snapshot, analysis must not use it.
            conn.execute(
                "INSERT INTO relation_checks(job_id, item_id, kind, source_doc, "
                "source_snapshot_id, target_url, target_doc, target_snapshot_id, state, "
                "reasons_json, evidence_json, checked_at) "
                "VALUES(?, 'signal-manual', 'replaced_by', 'doc-signal-manual-fr', ?, ?, "
                "'doc-signal-manual-fr-v2', NULL, 'verified', '[]', '{}', ?)",
                (old_job, old_snapshot, "http://localhost/old-target", db.utcnow()),
            )
            conn.commit()
        finally:
            conn.close()
        analysis = self.analysis("signal-manual")
        docs = {d["doc_id"]: d for d in analysis["documents"]}
        self.assertEqual(docs["doc-signal-manual-fr"]["state"], "withdrawn")
        for view in docs["doc-signal-manual-fr"]["replacements"]:
            self.assertNotEqual(
                view["relation_state"],
                "verified",
                "a check tied to a superseded snapshot must not be used",
            )

    def test_late_job_is_marked_superseded(self):
        self.collect()
        conn = self.connect()
        try:
            doc = self.get("tide-tables", "tide-tables-de")
            doc.version = "2"
            job = conn.execute(
                "SELECT job_id, source_snapshot_id FROM relation_jobs "
                "WHERE source_doc='doc-tide-tables-de' AND kind='translation_of' "
                "AND status='queued' ORDER BY job_id LIMIT 1"
            ).fetchone()
            old_job, old_snapshot = job["job_id"], job["source_snapshot_id"]
            self.collect()
            conn.close()
            self.relations()
            conn = self.connect()
            row = conn.execute(
                "SELECT status, reason FROM relation_jobs WHERE job_id=?", (old_job,)
            ).fetchone()
            self.assertEqual(row["status"], "superseded")
            self.assertEqual(row["reason"], "newer_source_snapshot_exists")
            checks = conn.execute(
                "SELECT 1 FROM relation_checks WHERE source_snapshot_id=?",
                (old_snapshot,),
            ).fetchall()
            self.assertEqual(checks, [])
        finally:
            conn.close()

    def test_cycle_blocks_group_and_is_reported(self):
        self.run_all()
        # Two same-language versions that each claim to replace the other.
        # The source metadata is syntactically valid, so the verifier accepts
        # each claim; only whole-group analysis can see the cycle.
        self.add_doc(
            "tide-tables",
            Doc(
                document_id="tide-tables-zh-v2",
                item_id="tide-tables",
                item_title="潮汐表说明",
                title="潮汐表说明（第二版）",
                language="zh",
                role="original",
                version="2",
                replaces=["/items/tide-tables/tide-tables-zh-v3"],
            ),
        )
        self.add_doc(
            "tide-tables",
            Doc(
                document_id="tide-tables-zh-v3",
                item_id="tide-tables",
                item_title="潮汐表说明",
                title="潮汐表说明（第三版）",
                language="zh",
                role="original",
                version="3",
                replaces=["/items/tide-tables/tide-tables-zh-v2"],
            ),
        )
        self.collect()
        self.relations()
        analysis = self.analysis("tide-tables")
        codes = {f["code"] for f in analysis["findings"]}
        self.assertIn("relation.cycle", codes)
        self.assertEqual(analysis["state"], "conflict")
        published = self.publish(force=True)
        row = next(r for r in published["results"] if r["item_id"] == "tide-tables")
        self.assertEqual(row["public_state"], "blocked")

    def test_quarantine_vs_independent_have_different_availability(self):
        self.run_all()
        ja = self.get("beacon-register", "beacon-register-ja")
        ja.language = "zh"
        ja.role = "original"
        ja.translation_of = None
        ja.translation_source_version = None
        ja.version = "2"
        ja.replaces = ["/items/beacon-register/beacon-register-zh"]
        zh = self.get("beacon-register", "beacon-register-zh")
        zh.version = "3"
        zh.replaces = ["/items/beacon-register/beacon-register-ja"]
        self.collect()
        self.relations()
        quarantined = self.analysis("beacon-register", "quarantine")
        independent = self.analysis("beacon-register", "independent")
        self.assertEqual(quarantined["state"], "conflict")
        self.assertEqual(independent["state"], "conflict")
        self.assertIn("整组资料暂停对外发布", quarantined["impact"]["availability"])
        self.assertIn("保留各语言的独立记录", independent["impact"]["availability"])
        published_q = verify_and_publish(self.db_path, policy="quarantine", force=True)
        row_q = next(r for r in published_q["results"] if r["item_id"] == "beacon-register")
        published_i = verify_and_publish(self.db_path, policy="independent", force=True)
        row_i = next(r for r in published_i["results"] if r["item_id"] == "beacon-register")
        self.assertEqual(row_q["public_state"], "blocked")
        self.assertEqual(row_i["public_state"], "conflict_independent")

    def test_fixing_one_item_only_republishes_that_item(self):
        self.run_all()
        self.get("tide-tables", "tide-tables-de").version = "2"
        self.collect()
        self.relations()
        result = self.publish()
        touched = [r["item_id"] for r in result["results"] if r["status"] == "published"]
        self.assertEqual(touched, ["tide-tables"])
    def test_corrupt_withdrawal_page_invalidates_instead_of_keeping_old(self):
        self.run_all()
        fr = self.get("signal-manual", "signal-manual-fr")
        fr.raw_override = "<html><body><h1>withdrawn but broken</h1></body></html>"
        self.collect()
        self.relations()
        analysis = self.analysis("signal-manual")
        docs = {d["doc_id"]: d for d in analysis["documents"]}
        self.assertEqual(docs["doc-signal-manual-fr"]["state"], "invalid")
        self.assertIn("document.invalid_snapshot", {f["code"] for f in analysis["findings"]})
        self.assertEqual(analysis["state"], "conflict")

    def test_unreachable_target_is_pending_and_retried(self):
        self.collect()
        self.state.force_errors["/items/harbor-charter/harbor-charter-zh"] = (500, "down")
        self.relations()
        analysis = self.analysis("harbor-charter")
        # An unreachable target is a transient condition: the group stays
        # available with a warning, and no correspondence is claimed.
        self.assertEqual(analysis["state"], "degraded")
        pending = [
            d for d in analysis["documents"]
            if d["translation"] and d["translation"]["relation_state"] == "pending"
        ]
        self.assertEqual(len(pending), 2)
        self.assertNotIn(
            "verified", {d["translation"]["relation_state"] for d in analysis["documents"] if d["translation"]}
        )
        conn = self.connect()
        try:
            queued = conn.execute(
                "SELECT COUNT(*) FROM relation_jobs WHERE status='queued' "
                "AND target_url LIKE '%harbor-charter-zh'"
            ).fetchone()[0]
            self.assertGreater(queued, 0)
        finally:
            conn.close()
        # Once the target recovers, the retry verifies it without new snapshots.
        self.state.force_errors.pop("/items/harbor-charter/harbor-charter-zh")
        result = self.relations()
        self.assertGreater(result["processed"], 0)
        healed = self.analysis("harbor-charter")
        self.assertEqual(healed["state"], "ok")
        self.assertEqual(
            {d["translation"]["relation_state"] for d in healed["documents"] if d["translation"]},
            {"verified"},
        )

    def test_gone_source_page_is_not_presented_as_current(self):
        self.run_all()
        # The item page still links the German version, but the page itself
        # now returns 404.  The archived snapshot must not stay current.
        self.state.force_errors["/items/tide-tables/tide-tables-de"] = (404, "gone")
        self.collect()
        self.relations()
        analysis = self.analysis("tide-tables")
        docs = {d["doc_id"]: d for d in analysis["documents"]}
        self.assertEqual(docs["doc-tide-tables-de"]["state"], "gone")
        self.assertIn("document.source_gone", {f["code"] for f in analysis["findings"]})
        self.assertEqual(analysis["state"], "conflict")
        self.assertNotIn(
            "de",
            [lang["language"] for lang in analysis["languages"] if lang["translations"]],
        )

    def test_relation_retry_exhausts_after_bound_attempts(self):
        self.collect()
        self.state.force_errors["/items/tide-tables/tide-tables-zh"] = (503, "down")
        for _ in range(3):
            self.relations()
        analysis = self.analysis("tide-tables")
        codes = {f["code"] for f in analysis["findings"]}
        self.assertIn("relation.pending_exhausted", codes)


class ConcurrencyTests(SiteCase):
    def test_reader_sees_consistent_publication_during_writes(self):
        """A read-only catalog connection must never block a writer's commit."""
        self.run_all()
        stop = threading.Event()
        errors: list[BaseException] = []

        def reader():
            try:
                while not stop.is_set():
                    conn = self.connect()
                    try:
                        conn.execute("PRAGMA query_only = ON")
                        for row in conn.execute(
                            "SELECT payload_json FROM publications"
                        ):
                            json.loads(row["payload_json"])
                    finally:
                        conn.close()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for index in range(3):
                self.get("tide-tables", "tide-tables-de").version = str(index + 2)
                self.collect()
                self.relations()
                self.publish()
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertEqual(errors, [])

    def test_readonly_catalog_cannot_mutate(self):
        self.run_all()
        conn = self.connect()
        try:
            conn.execute("PRAGMA query_only = ON")
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM publications")
        finally:
            conn.close()


class WebTests(SiteCase):
    def setUp(self) -> None:
        super().setUp()
        self.catalog = make_catalog(self.db_path, self.data, 0)
        self.catalog_thread = threading.Thread(
            target=self.catalog.serve_forever, daemon=True
        )
        self.catalog_thread.start()
        self.catalog_base = f"http://127.0.0.1:{self.catalog.server_port}"
        self.run_all()

    def tearDown(self) -> None:
        self.catalog.shutdown()
        self.catalog.server_close()
        self.catalog_thread.join(timeout=5)
        super().tearDown()

    def get_html(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(self.catalog_base + path) as response:
            return response.status, response.read().decode()

    def test_index_shows_usable_and_state(self):
        status, body = self.get_html("/")
        self.assertEqual(status, 200)
        self.assertIn("港湾通行章程", body)
        self.assertIn("信号灯维护手册", body)

    def test_item_page_shows_withdrawal_basis_languages_and_snapshots(self):
        status, body = self.get_html("/items/signal-manual")
        self.assertEqual(status, 200)
        self.assertIn("撤回依据", body)
        self.assertIn("来源撤回该译文", body)
        self.assertIn("语言对应", body)
        self.assertIn("替代与撤回链", body)
        self.assertIn("全部来源快照", body)
        self.assertIn("信号灯维护手册", body)
        self.assertIn("SHA-256", body)

    def test_raw_snapshot_is_inert_text(self):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT doc_id, snapshot_id FROM snapshots WHERE doc_id='doc-signal-manual-fr'"
            ).fetchone()
        finally:
            conn.close()
        status, body = self.get_html(f"/source/{row['doc_id']}/{row['snapshot_id']}")
        self.assertEqual(status, 200)
        self.assertIn("抓取原文", body)
        self.assertNotIn("<script type=\"application/ld+json\">", body)

    def test_status_page_renders(self):
        status, body = self.get_html("/status")
        self.assertEqual(status, 200)
        self.assertIn("等待推进的资料", body)
        self.assertIn("采集问题", body)
        self.assertIn("未完成的关系任务", body)
        self.assertIn("最近发布核对", body)

    def test_raw_download_is_text_and_matches_snapshot_hash(self):
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT snapshot_id, body_sha256 FROM snapshots "
                "WHERE doc_id='doc-harbor-charter-zh'"
            ).fetchone()
        finally:
            conn.close()
        with urllib.request.urlopen(
            f"{self.catalog_base}/raw/{'doc-harbor-charter-zh'}/{row['snapshot_id']}"
        ) as response:
            data = response.read()
            self.assertTrue(response.headers["Content-Type"].startswith("text/plain"))
            self.assertEqual(response.headers["X-Snapshot-SHA256"], row["body_sha256"])
        import hashlib

        self.assertEqual(hashlib.sha256(data).hexdigest(), row["body_sha256"])
        # It is the original archived bytes, but the response is inert: text
        # and sandboxed, never rendered as a page of this catalog.
        self.assertTrue(data.lower().startswith(b"<!doctype html>"))

    def test_unknown_pages_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_html("/items/no-such-item")
        self.assertEqual(ctx.exception.code, 404)

    def test_no_write_endpoints(self):
        request = urllib.request.Request(
            self.catalog_base + "/items/signal-manual", method="POST", data=b"{}"
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertIn(ctx.exception.code, (404, 405, 501))


class ConflictPolicyTests(SiteCase):
    def test_policy_switch_rerenders_availability(self):
        self.collect()
        ja = self.get("beacon-register", "beacon-register-ja")
        ja.replaces = ["/items/beacon-register/beacon-register-zh"]
        zh = self.get("beacon-register", "beacon-register-zh")
        zh.replaces = ["/items/beacon-register/beacon-register-ja"]
        self.collect()
        self.relations()
        self.publish(policy="quarantine", force=True)
        catalogue = make_catalog(self.db_path, self.data, 0)
        thread = threading.Thread(target=catalogue.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{catalogue.server_port}/"
            ) as response:
                body = response.read().decode()
            self.assertIn("隔离区", body)
            self.assertIn("灯塔登记册", body)
            self.assertIn("整组资料因关系冲突暂停对外提供", body)
        finally:
            catalogue.shutdown()
            catalogue.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
