"""Collector: discover the catalog, fetch every language version, snapshot it.

Independent from relation verification and from publication.  It records what
the source said at observation time; it never decides whether relations are
sound.  Every observation gets a monotonic sequence number so late-finishing
relation jobs can be compared against it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from corpus_audit import db
from corpus_audit.fetch import Client, FetchError
from corpus_audit.parse import (
    ParseError,
    DocMetadata,
    parse_catalog,
    parse_document,
    parse_item_page,
)

MAX_ITEMS_PER_RUN = 500
MAX_DOCS_PER_ITEM = 50
MAX_PAGES = 2000


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def raw_path(self, doc_id: str, seq: int) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in doc_id)[:120]
        directory = self.root / safe
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{seq:08d}.html"

    def save(self, doc_id: str, seq: int, body: bytes) -> str:
        path = self.raw_path(doc_id, seq)
        path.write_bytes(body)
        return str(path)

    def save_evidence(self, key: str, seq: int, body: bytes) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)[:120]
        directory = self.root / "_evidence" / safe
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{seq:08d}.html"
        path.write_bytes(body)
        return str(path)


class Collector:
    def __init__(
        self,
        conn: sqlite3.Connection,
        client: Client,
        store: Store,
        *,
        actor: str = "collector",
    ) -> None:
        self.conn = conn
        self.client = client
        self.store = store
        self.actor = actor
        self.run_id: int | None = None

    # -- run lifecycle -------------------------------------------------------
    def start_run(self) -> int:
        db.begin_write(self.conn)
        try:
            cur = self.conn.execute(
                "INSERT INTO collect_runs(started_at, status) VALUES(?, 'running')",
                (db.utcnow(),),
            )
            self.run_id = int(cur.lastrowid)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.run_id

    def finish_run(self, status: str, stats: dict[str, int]) -> None:
        assert self.run_id is not None
        db.begin_write(self.conn)
        try:
            self.conn.execute(
                "UPDATE collect_runs SET finished_at=?, status=?, seen_items=?, "
                "seen_docs=?, new_snapshots=? WHERE run_id=?",
                (
                    db.utcnow(),
                    status,
                    stats.get("items", 0),
                    stats.get("docs", 0),
                    stats.get("new_snapshots", 0),
                    self.run_id,
                ),
            )
            db.audit(
                self.conn,
                self.actor,
                "collect.finish",
                {"status": status, **stats},
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def issue(
        self,
        code: str,
        message: str,
        *,
        item_id: str | None = None,
        doc_id: str | None = None,
        url: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        assert self.run_id is not None
        self.conn.execute(
            "INSERT INTO collect_issues(run_id, item_id, doc_id, url, code, message, "
            "detail, at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.run_id,
                item_id,
                doc_id,
                url,
                code,
                message,
                json.dumps(detail or {}, ensure_ascii=False, default=str),
                db.utcnow(),
            ),
        )

    # -- main entry ----------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self.start_run()
        stats = {"items": 0, "docs": 0, "new_snapshots": 0, "errors": 0}
        status = "ok"
        try:
            pages = 0
            seen_items: set[str] = set()
            try:
                home = self.client.get("/")
                pages += 1
                html_text = home.body.decode("utf-8", errors="replace")
                entries = parse_catalog(html_text)
            except (FetchError, ParseError) as exc:
                raise ValueError(f"目录抓取失败: {exc}") from exc

            if len(entries) > MAX_ITEMS_PER_RUN:
                self.issue(
                    "catalog_too_large",
                    f"目录条目 {len(entries)} 超过上限 {MAX_ITEMS_PER_RUN}，本轮只处理前 {MAX_ITEMS_PER_RUN}",
                )
                entries = entries[:MAX_ITEMS_PER_RUN]

            for entry in entries:
                stats["items"] += 1
                seen_items.add(entry.item_id)
                try:
                    fetched_docs, used = self.collect_item(entry, pages)
                    pages += used
                    stats["docs"] += len(fetched_docs)
                    stats["new_snapshots"] += sum(1 for d in fetched_docs if d)
                    if pages > MAX_PAGES:
                        self.issue("too_many_pages", "本轮抓取页数超过上限，提前结束")
                        status = "partial"
                        break
                except (FetchError, ParseError, ValueError) as exc:
                    stats["errors"] += 1
                    self.issue("item_failed", str(exc), item_id=entry.item_id, url=entry.url)
                    status = "partial"
                self.conn.commit()

            # Items that the source no longer lists are delisted as a whole.
            # A partial run must not delist anything: the missing catalog
            # entries may simply not have been reached yet.
            known_items = {r["item_id"] for r in self.conn.execute("SELECT item_id FROM items")}
            missing_items = sorted(known_items - seen_items) if status == "ok" else []
            for missing in missing_items:
                rows = self.conn.execute(
                    "SELECT doc_id, url FROM documents WHERE item_id=? AND currently_listed=1",
                    (missing,),
                ).fetchall()
                if not rows:
                    continue
                for row in rows:
                    self.conn.execute(
                        "UPDATE documents SET currently_listed=0 WHERE doc_id=?",
                        (row["doc_id"],),
                    )
                    db.audit(
                        self.conn,
                        self.actor,
                        "document.delisted",
                        {"url": row["url"], "reason": "item no longer listed in source catalog"},
                        item_id=missing,
                        doc_id=row["doc_id"],
                    )
                db.touch_item(self.conn, missing)
                self.issue(
                    "item_delisted",
                    "资料已从来源目录移除，其语言版本全部停止展示。",
                    item_id=missing,
                )
                self.conn.commit()

            self.finish_run(status, stats)
        except Exception as exc:
            self.conn.rollback()
            try:
                self.finish_run("failed", stats)
            finally:
                pass
            raise
        return {"run_id": self.run_id, "status": status, **stats}

    # -- item ----------------------------------------------------------------
    def collect_item(self, entry, pages: int) -> tuple[list[bool], int]:
        item_url = self.client.absolute(entry.url)
        page = self.client.get(item_url)
        pages += 1
        item_title, links = parse_item_page(
            page.body.decode("utf-8", errors="replace"), page.url
        )
        self.upsert_item(entry.item_id, item_title, item_url)

        seen_docs: list[tuple[str, str]] = []
        attempted: set[str] = set()
        added: list[bool] = []
        if len(links) > MAX_DOCS_PER_ITEM:
            self.issue(
                "too_many_variants",
                f"资料 {entry.item_id} 的语言版本超过 {MAX_DOCS_PER_ITEM}，已截断",
                item_id=entry.item_id,
            )
            links = links[:MAX_DOCS_PER_ITEM]

        for link in links:
            try:
                url = self.client.absolute(link.url)
            except FetchError as exc:
                self.issue(
                    "variant_url_rejected",
                    self._one_line(exc),
                    item_id=entry.item_id,
                    url=link.url,
                )
                continue
            try:
                snapshot_created = self.collect_document(
                    entry.item_id, url, discovered_via="item-page"
                )
            except (FetchError, ParseError, ValueError) as exc:
                added.append(False)
                attempted.add(url)
                self.issue(
                    "document_failed",
                    self._one_line(exc),
                    item_id=entry.item_id,
                    url=url,
                )
                self.mark_document_error(url, exc)
                continue
            added.append(snapshot_created)
            attempted.add(url)
            seen_docs.append((url, link.language))

        # Any document URL that belonged to this item but was not linked now
        # is delisted, not deleted; snapshots remain auditable.  A document
        # that was linked but failed to fetch stays listed so its last good
        # snapshot is not silently dropped.
        listed = {url for url, _ in seen_docs} | attempted
        rows = self.conn.execute(
            "SELECT doc_id, url FROM documents WHERE item_id=? AND currently_listed=1",
            (entry.item_id,),
        ).fetchall()
        for row in rows:
            if row["url"] not in listed:
                self.conn.execute(
                    "UPDATE documents SET currently_listed=0 WHERE doc_id=?", (row["doc_id"],)
                )
                # Do not rewrite snapshot.status (its meaning is "what the
                # source said"); delisting is a document-level fact.
                db.audit(
                    self.conn,
                    self.actor,
                    "document.delisted",
                    {"url": row["url"], "reason": "no longer linked from item page"},
                    item_id=entry.item_id,
                    doc_id=row["doc_id"],
                )
        return added, pages

    @staticmethod
    def _one_line(exc: Exception) -> str:
        text = getattr(exc, "message", None) or str(exc)
        return " ".join(text.split())[:1000]

    def upsert_item(self, item_id: str, title: str, url: str) -> None:
        now = db.utcnow()
        existing = self.conn.execute(
            "SELECT item_id FROM items WHERE item_id=?", (item_id,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE items SET title=?, url=?, updated_at=? WHERE item_id=?",
                (title, url, now, item_id),
            )
        else:
            self.conn.execute(
                "INSERT INTO items(item_id, title, original_language, url, created_at, updated_at) "
                "VALUES(?, ?, '', ?, ?, ?)",
                (item_id, title, url, now, now),
            )
            db.audit(self.conn, self.actor, "item.created", {"title": title}, item_id=item_id)

    # -- document ------------------------------------------------------------
    def collect_document(self, item_id: str, url: str, *, discovered_via: str) -> bool:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE url=?", (url,)
        ).fetchone()
        last_checked = db.utcnow()
        etag = row["last_etag"] if row else None
        last_modified = row["last_modified"] if row else None
        try:
            fetched = self.client.get(url, etag=etag, last_modified=last_modified)
        except FetchError as exc:
            self.mark_document_error(url, exc, last_checked=last_checked)
            raise

        if fetched.not_modified:
            if row is None:
                # A conditional request for an unknown document is a bug or a
                # cache weirdness; re-fetch unconditionally rather than guess.
                fetched = self.client.get(url)
            else:
                self.conn.execute(
                    "UPDATE documents SET last_checked_at=?, last_http_status=304, "
                    "last_error=NULL WHERE doc_id=?",
                    (last_checked, row["doc_id"]),
                )
                return False

        assert fetched.body is not None
        # A 200 with byte-identical content is not a new observation.  ETags
        # can change for unrelated reasons (or be absent), so compare content.
        if row is not None and row["current_snapshot_id"] is not None:
            current = self.conn.execute(
                "SELECT body_sha256 FROM snapshots WHERE snapshot_id=?",
                (row["current_snapshot_id"],),
            ).fetchone()
            if current is not None and current["body_sha256"] == fetched.sha256:
                self.conn.execute(
                    "UPDATE documents SET last_checked_at=?, last_http_status=?, "
                    "last_etag=?, last_modified=?, last_error=NULL WHERE doc_id=?",
                    (
                        last_checked,
                        fetched.status,
                        fetched.etag,
                        fetched.last_modified,
                        row["doc_id"],
                    ),
                )
                return False
        html_text = fetched.body.decode("utf-8", errors="replace")
        seq = db.next_sequence(self.conn)
        doc_id = row["doc_id"] if row else None
        try:
            meta = parse_document(html_text, fetched.url)
        except ParseError as exc:
            # The source returned a page we cannot trust.  Record the response
            # as an invalid snapshot so the previous good snapshot is not
            # silently kept as current -- a withdrawal with a missing basis
            # must not leave the old translation published.
            if doc_id is None:
                raise
            self.record_invalid_snapshot(
                doc_id=doc_id,
                item_id=item_id,
                fetched=fetched,
                seq=seq,
                last_checked=last_checked,
                error=exc,
            )
            raise

        if row is None:
            doc_id = self._doc_id(meta.document_id)
        assert doc_id is not None
        if row and row["item_id"] != item_id:
            raise ValueError(
                f"文档 {doc_id} 已属于资料 {row['item_id']}，但被资料 {item_id} 链接"
            )
        if meta.item_id != item_id:
            self.record_invalid_snapshot(
                doc_id=doc_id,
                item_id=item_id,
                fetched=fetched,
                seq=seq,
                last_checked=last_checked,
                error=ParseError(
                    "item_id_mismatch",
                    f"文档声明属于 {meta.item_id!r}，但被资料 {item_id!r} 链接",
                ),
            )
            raise ValueError(f"文档 {doc_id} 声明的 itemId 与采集资料不一致")
        if doc_id != self._doc_id(meta.document_id):
            self.record_invalid_snapshot(
                doc_id=doc_id,
                item_id=item_id,
                fetched=fetched,
                seq=seq,
                last_checked=last_checked,
                error=ParseError(
                    "document_id_changed",
                    f"同一地址上的 documentId 从 {doc_id.removeprefix('doc-')!r} "
                    f"变为 {meta.document_id!r}",
                ),
            )
            raise ValueError(f"文档 {doc_id} 的 documentId 发生变化")
        if row and row["language"] != meta.language:
            # Language changing under a stable document URL is a source error.
            raise ValueError(
                f"文档 {doc_id} 之前声明语言 {row['language']}，现在声明 {meta.language}"
            )

        raw_path = self.store.save(doc_id, seq, fetched.body)
        parsed = asdict(meta)
        parsed["withdrawal"] = (
            asdict(meta.withdrawal) if meta.withdrawal else None
        )
        translation_of_url = (
            self.client.absolute(meta.translation_of) if meta.translation_of else None
        )
        parsed["translation_of_url"] = translation_of_url
        parsed["replaces_urls"] = [self.client.absolute(u) for u in meta.replaces]
        parsed["replaced_by_urls"] = [self.client.absolute(u) for u in meta.replaced_by]

        # The snapshot references its document, so create the document row
        # first.  current_snapshot_id has no FK and is filled in after the
        # snapshot insert; a reader in between sees a document with no
        # snapshot and simply skips it.
        if row is None:
            self.conn.execute(
                "INSERT INTO documents(doc_id, item_id, language, role, url, currently_listed, "
                "discovered_via, current_snapshot_id, first_seen_seq, last_seen_seq, "
                "last_checked_at, last_http_status, last_etag, last_modified, last_error) "
                "VALUES(?, ?, ?, ?, ?, 1, ?, NULL, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    doc_id,
                    item_id,
                    meta.language,
                    meta.role,
                    fetched.url,
                    discovered_via,
                    seq,
                    seq,
                    last_checked,
                    fetched.status,
                    fetched.etag,
                    fetched.last_modified,
                ),
            )
            db.audit(
                self.conn,
                self.actor,
                "document.created",
                {"language": meta.language, "role": meta.role, "version": meta.version},
                item_id=item_id,
                doc_id=doc_id,
            )

        cur = self.conn.execute(
            "INSERT INTO snapshots(doc_id, item_id, url, language, role, title, version, "
            "status, translation_of, source_version, observed_seq, fetched_at, http_status, "
            "etag, last_modified, body_sha256, raw_path, parsed_json) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                item_id,
                fetched.url,
                meta.language,
                meta.role,
                meta.title,
                meta.version,
                meta.status,
                translation_of_url,
                meta.translation_source_version,
                seq,
                last_checked,
                fetched.status,
                fetched.etag,
                fetched.last_modified,
                fetched.sha256,
                raw_path,
                json.dumps(parsed, ensure_ascii=False),
            ),
        )
        snapshot_id = int(cur.lastrowid)

        if row is not None:
            self.conn.execute(
                "UPDATE documents SET current_snapshot_id=?, last_seen_seq=?, "
                "last_checked_at=?, last_http_status=?, last_etag=?, last_modified=?, "
                "last_error=NULL, currently_listed=1, language=?, role=? WHERE doc_id=?",
                (
                    snapshot_id,
                    seq,
                    last_checked,
                    fetched.status,
                    fetched.etag,
                    fetched.last_modified,
                    meta.language,
                    meta.role,
                    doc_id,
                ),
            )
        else:
            self.conn.execute(
                "UPDATE documents SET current_snapshot_id=? WHERE doc_id=?",
                (snapshot_id, doc_id),
            )

        if meta.role == "original":
            self.conn.execute(
                "UPDATE items SET original_language=?, title=?, updated_at=? WHERE item_id=?",
                (meta.language, meta.item_title, db.utcnow(), item_id),
            )

        self.conn.execute(
            "INSERT INTO withdrawal_evidence(snapshot_id, doc_id, item_id, url, http_status, "
            "sha256, raw_path, fetched_at, error) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                snapshot_id,
                doc_id,
                item_id,
                fetched.url,
                fetched.status,
                fetched.sha256,
                raw_path,
                last_checked,
            ),
        )
        db.touch_item(self.conn, item_id, seq)
        db.audit(
            self.conn,
            self.actor,
            "snapshot.observed",
            {
                "snapshot_id": snapshot_id,
                "version": meta.version,
                "status": meta.status,
                "seq": seq,
            },
            item_id=item_id,
            doc_id=doc_id,
        )
        self.queue_relation_jobs(doc_id, item_id, snapshot_id, meta, translation_of_url)
        self.queue_dependent_rechecks(fetched.url, seq)
        return True

    def queue_dependent_rechecks(self, target_url: str, seq: int) -> None:
        """Re-check documents whose current claims point at a changed page.

        A relation check is tied to target snapshots; when the target moves,
        the old check is stale.  Queueing a fresh job here means a changed
        original or replacement page triggers re-verification of every claim
        about it, without re-checking unrelated documents.
        """
        needle = target_url.replace("\\", "\\\\")
        rows = self.conn.execute(
            """
            SELECT d.doc_id, d.item_id, d.current_snapshot_id, s.parsed_json
            FROM documents d
            JOIN snapshots s ON s.snapshot_id = d.current_snapshot_id
            WHERE s.parsed_json LIKE ? ESCAPE '\\'
              AND d.current_snapshot_id IS NOT NULL
            """,
            (f'%{needle}%',),
        ).fetchall()
        for row in rows:
            parsed = json.loads(row["parsed_json"])
            claims: list[tuple[str, str]] = []
            for kind, key in (
                ("replaces", "replaces_urls"),
                ("replaced_by", "replaced_by_urls"),
            ):
                for url in parsed.get(key) or []:
                    if url == target_url:
                        claims.append((kind, url))
            if parsed.get("translation_of_url") == target_url:
                claims.append(("translation_of", target_url))
            for kind, url in claims:
                self.conn.execute(
                    "INSERT OR IGNORE INTO relation_jobs(item_id, kind, source_doc, "
                    "source_snapshot_id, target_url, input_seq, status, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, 'queued', ?)",
                    (
                        row["item_id"],
                        kind,
                        row["doc_id"],
                        row["current_snapshot_id"],
                        url,
                        seq,
                        db.utcnow(),
                    ),
                )

    @staticmethod
    def _doc_id(source_document_id: str) -> str:
        return "doc-" + source_document_id

    def record_invalid_snapshot(
        self,
        *,
        doc_id: str,
        item_id: str,
        fetched,
        seq: int,
        last_checked: str,
        error: Exception,
    ) -> int:
        """Record a response that cannot be trusted as the document's current state.

        Keeping the previous good snapshot current would be wrong: if the
        source withdrew a translation but the withdrawal page is unparsable,
        the old translation must not stay published as if nothing happened.
        """
        previous = self.conn.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = "
            "(SELECT current_snapshot_id FROM documents WHERE doc_id=?)",
            (doc_id,),
        ).fetchone()
        raw_path = self.store.save(doc_id, seq, fetched.body or b"")
        detail = {
            "code": getattr(error, "code", "invalid_snapshot"),
            "message": str(error),
        }
        cur = self.conn.execute(
            "INSERT INTO snapshots(doc_id, item_id, url, language, role, title, version, "
            "status, translation_of, source_version, observed_seq, fetched_at, http_status, "
            "etag, last_modified, body_sha256, raw_path, parsed_json) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'invalid', NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                item_id,
                fetched.url,
                previous["language"] if previous else "",
                previous["role"] if previous else "",
                previous["title"] if previous else "",
                previous["version"] if previous else "?",
                seq,
                last_checked,
                fetched.status,
                fetched.etag,
                fetched.last_modified,
                fetched.sha256,
                raw_path,
                json.dumps({"invalid": detail}, ensure_ascii=False),
            ),
        )
        snapshot_id = int(cur.lastrowid)
        self.conn.execute(
            "UPDATE documents SET current_snapshot_id=?, last_checked_at=?, "
            "last_http_status=?, last_etag=?, last_modified=?, last_error=? WHERE doc_id=?",
            (
                snapshot_id,
                last_checked,
                fetched.status,
                fetched.etag,
                fetched.last_modified,
                json.dumps(detail, ensure_ascii=False),
                doc_id,
            ),
        )
        db.touch_item(self.conn, item_id, seq)
        db.audit(
            self.conn,
            self.actor,
            "snapshot.invalid",
            {"snapshot_id": snapshot_id, "error": detail, "seq": seq},
            item_id=item_id,
            doc_id=doc_id,
        )
        return snapshot_id

    def mark_document_error(
        self, url: str, exc: Exception, *, last_checked: str | None = None
    ) -> None:
        row = self.conn.execute("SELECT doc_id FROM documents WHERE url=?", (url,)).fetchone()
        detail = {
            "code": getattr(exc, "code", "error"),
            "message": self._one_line(exc),
            "status": getattr(exc, "status", None),
        }
        if row:
            self.conn.execute(
                "UPDATE documents SET last_checked_at=?, last_http_status=?, last_error=? "
                "WHERE doc_id=?",
                (
                    last_checked or db.utcnow(),
                    detail["status"],
                    json.dumps(detail, ensure_ascii=False),
                    row["doc_id"],
                ),
            )
        if self.run_id is not None:
            self.conn.execute(
                "INSERT INTO fetch_errors(run_id, item_id, doc_id, url, code, message, "
                "attempted_at) VALUES(?, NULL, ?, ?, ?, ?, ?)",
                (
                    self.run_id,
                    row["doc_id"] if row else None,
                    url,
                    detail["code"],
                    detail["message"][:1000],
                    db.utcnow(),
                ),
            )

    def queue_relation_jobs(
        self,
        doc_id: str,
        item_id: str,
        snapshot_id: int,
        meta: DocMetadata,
        translation_of_url: str | None,
    ) -> None:
        targets: list[tuple[str, str]] = []
        for url in meta.replaces:
            targets.append(("replaces", self.client.absolute(url)))
        for url in meta.replaced_by:
            targets.append(("replaced_by", self.client.absolute(url)))
        if translation_of_url:
            targets.append(("translation_of", translation_of_url))
        for kind, target in targets:
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO relation_jobs(item_id, kind, source_doc, "
                    "source_snapshot_id, target_url, input_seq, status, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, 'queued', ?)",
                    (
                        item_id,
                        kind,
                        doc_id,
                        snapshot_id,
                        target,
                        db.next_sequence(self.conn),
                        db.utcnow(),
                    ),
                )
            except sqlite3.IntegrityError:
                # Duplicate queued job for the same snapshot/target.
                pass
