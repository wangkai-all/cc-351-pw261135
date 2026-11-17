"""Controllable fictional source site for local testing.

Run:  python3 -m corpus_audit.source --port 8100

The home page lists items.  Each item page links its language variants with
rel=alternate.  Every document page carries a JSON-LD CorpusDocument block
with role/version/translationOf/withdrawal/replaces/replacedBy metadata.

Fault injection for tests is available at POST /__admin/... and only exists
when the server is started by the test harness (always, in this local tool).
"""

from __future__ import annotations

import argparse
import html
import json
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from corpus_audit.static_paths import normalize_path

ROLE_ORIGINAL = "original"
ROLE_TRANSLATION = "translation"


@dataclass
class Doc:
    document_id: str
    item_id: str
    item_title: str
    title: str
    language: str
    role: str
    version: str
    status: str = "active"
    translation_of: str | None = None
    translation_source_version: int | None = None
    replaces: list[str] = field(default_factory=list)
    replaced_by: list[str] = field(default_factory=list)
    withdrawal_reason: str | None = None
    withdrawn_at: str | None = None
    body: str = ""
    date_modified: str | None = None
    raw_override: str | None = None  # if set, served verbatim as HTML
    extra_jsonld: str | None = None  # appended as a second JSON-LD block


@dataclass
class Item:
    item_id: str
    title: str
    documents: list[Doc] = field(default_factory=list)

    def doc(self, doc_id: str) -> Doc:
        for document in self.documents:
            if document.document_id == doc_id:
                return document
        raise KeyError(doc_id)


class SiteState:
    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self.lock = threading.RLock()
        self.request_log: list[dict[str, Any]] = []
        self.etag_version = 0
        self.force_errors: dict[str, tuple[int, str]] = {}
        self.delay_seconds: dict[str, float] = {}

    # -- construction helpers -------------------------------------------------
    def add_item(self, item: Item) -> None:
        with self.lock:
            if item.item_id in self.items:
                raise ValueError(f"duplicate item {item.item_id}")
            self.items[item.item_id] = item
            self.etag_version += 1

    def replace_doc(self, item_id: str, doc: Doc) -> None:
        with self.lock:
            item = self.items[item_id]
            for index, existing in enumerate(item.documents):
                if existing.document_id == doc.document_id:
                    item.documents[index] = doc
                    self.etag_version += 1
                    return
            raise KeyError(doc.document_id)

    def remove_doc(self, item_id: str, doc_id: str) -> None:
        with self.lock:
            item = self.items[item_id]
            item.documents = [d for d in item.documents if d.document_id != doc_id]
            self.etag_version += 1

    # -- URLs -----------------------------------------------------------------
    def doc_url(self, item_id: str, doc_id: str) -> str:
        return f"/items/{item_id}/{doc_id}"

    def item_url(self, item_id: str) -> str:
        return f"/items/{item_id}/"

    def variant_urls(self, item: Item) -> list[tuple[str, str]]:
        return [(d.language, self.doc_url(item.item_id, d.document_id)) for d in item.documents]

    # -- rendering ------------------------------------------------------------
    def render_home(self) -> str:
        rows = []
        for item in self.items.values():
            langs = ", ".join(
                f'<a href="{html.escape(self.doc_url(item.item_id, d.document_id))}" '
                f'hreflang="{html.escape(d.language)}">{html.escape(d.language)}</a>'
                for d in item.documents
                if d.status == "active"
            )
            rows.append(
                f'<li><a rel="item" data-item-id="{html.escape(item.item_id)}" '
                f'data-item-title="{html.escape(item.title)}" '
                f'href="{html.escape(self.item_url(item.item_id))}">'
                f"{html.escape(item.title)}</a> — {langs}</li>"
            )
        return (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>虚构档案来源站</title></head><body><h1>虚构档案来源站</h1>"
            "<p>本站点全部内容为测试用虚构资料。</p><ul>"
            + "".join(rows)
            + "</ul></body></html>"
        )

    def render_item(self, item: Item) -> str:
        links = "".join(
            f'<li><a rel="alternate" hreflang="{html.escape(lang)}" '
            f'href="{html.escape(url)}">{html.escape(lang)}</a></li>'
            for lang, url in self.variant_urls(item)
        )
        return (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(item.title)}</title></head><body>"
            f"<h1>{html.escape(item.title)}</h1><ul>{links}</ul></body></html>"
        )

    def render_doc(self, doc: Doc) -> str:
        if doc.raw_override is not None:
            return doc.raw_override
        meta: dict[str, Any] = {
            "@context": "https://schema.example/corpus",
            "@type": "CorpusDocument",
            "documentId": doc.document_id,
            "itemId": doc.item_id,
            "itemTitle": doc.item_title,
            "name": doc.title,
            "inLanguage": doc.language,
            "role": doc.role,
            "version": doc.version,
            "status": doc.status,
        }
        if doc.date_modified:
            meta["dateModified"] = doc.date_modified
        if doc.translation_of:
            meta["translationOf"] = doc.translation_of
        if doc.translation_source_version is not None:
            meta["translationSourceVersion"] = doc.translation_source_version
        if doc.replaces:
            meta["replaces"] = doc.replaces
        if doc.replaced_by:
            meta["replacedBy"] = doc.replaced_by
        if doc.status == "withdrawn":
            meta["withdrawal"] = {"reason": doc.withdrawal_reason or ""}
            if doc.withdrawn_at:
                meta["withdrawal"]["withdrawnAt"] = doc.withdrawn_at
        blocks = [
            f'<script type="application/ld+json">{json.dumps(meta, ensure_ascii=False)}</script>'
        ]
        if doc.extra_jsonld:
            blocks.append(
                f'<script type="application/ld+json">{doc.extra_jsonld}</script>'
            )
        body = doc.body or f"这是 {doc.item_title} 的 {doc.language} 版本（{doc.role}）。"
        return (
            "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(doc.title)}</title>{''.join(blocks)}</head><body>"
            f"<article><h1>{html.escape(doc.title)}</h1><p>{html.escape(body)}</p>"
            f"<p>版本 {html.escape(doc.version)} · 语言 {html.escape(doc.language)} · "
            f"角色 {html.escape(doc.role)} · 状态 {html.escape(doc.status)}</p>"
            "</article></body></html>"
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "FictionalCorpus/1.0"
    state: SiteState

    def log_message(self, fmt, *args):  # keep test output calm
        pass

    def _send(self, status: int, body: str | bytes, content_type: str) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if status == HTTPStatus.OK:
            self.send_header("ETag", f'"v{self.state.etag_version}"')
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False), "application/json")

    def do_GET(self):
        path = normalize_path(urlsplit(self.path).path)
        with self.state.lock:
            self.state.request_log.append({"path": path, "method": "GET"})
            delay = self.state.delay_seconds.get(path)
            error = self.state.force_errors.get(path)
        if delay:
            import time

            time.sleep(delay)
        if error:
            status, message = error
            self._send(status, message, "text/plain; charset=utf-8")
            return
        try:
            self._route_get(path)
        except Exception as exc:  # pragma: no cover - defensive
            self._json(500, {"error": str(exc)})

    def _route_get(self, path: str) -> None:
        if path in ("/", "/index.html"):
            self._send(200, self.state.render_home(), "text/html; charset=utf-8")
            return
        if path == "/robots.txt":
            self._send(200, "User-agent: *\nDisallow:\n", "text/plain; charset=utf-8")
            return
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] == "items":
            item_id = parts[1]
            item = self.state.items.get(item_id)
            if not item:
                self._send(404, "not found", "text/plain; charset=utf-8")
                return
            if len(parts) == 2 or (len(parts) == 3 and parts[2] == ""):
                self._send(200, self.state.render_item(item), "text/html; charset=utf-8")
                return
            if len(parts) == 3:
                for doc in item.documents:
                    if doc.document_id == parts[2]:
                        self._send(200, self.state.render_doc(doc), "text/html; charset=utf-8")
                        return
        if path == "/__state":
            with self.state.lock:
                self._json(
                    200,
                    {
                        "items": {
                            item.item_id: {
                                "title": item.title,
                                "documents": [
                                    {
                                        "document_id": d.document_id,
                                        "language": d.language,
                                        "role": d.role,
                                        "version": d.version,
                                        "status": d.status,
                                    }
                                    for d in item.documents
                                ],
                            }
                            for item in self.state.items.values()
                        },
                        "requests": list(self.state.request_log),
                    },
                )
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = normalize_path(urlsplit(self.path).path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not path.startswith("/__admin/"):
            self._json(404, {"error": "not found"})
            return
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        if path == "/__admin/error":
            self.state.force_errors[payload["path"]] = (
                int(payload.get("status", 500)),
                payload.get("message", "injected error"),
            )
            self._json(200, {"ok": True})
            return
        if path == "/__admin/clear-error":
            self.state.force_errors.pop(payload["path"], None)
            self._json(200, {"ok": True})
            return
        if path == "/__admin/delay":
            self.state.delay_seconds[payload["path"]] = float(payload["seconds"])
            self._json(200, {"ok": True})
            return
        if path == "/__admin/clear-delay":
            self.state.delay_seconds.pop(payload["path"], None)
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})


def build_fixture(site: SiteState) -> SiteState:
    """Populate the fictional corpus used by tests and the demo."""

    def doc(
        doc_id: str,
        item_id: str,
        item_title: str,
        title: str,
        language: str,
        role: str,
        version: str,
        **kwargs,
    ) -> Doc:
        return Doc(
            document_id=doc_id,
            item_id=item_id,
            item_title=item_title,
            title=title,
            language=language,
            role=role,
            version=version,
            **kwargs,
        )

    site.add_item(
        Item(
            "harbor-charter",
            "港湾通行章程",
            [
                doc("harbor-charter-zh", "harbor-charter", "港湾通行章程", "港湾通行章程", "zh", ROLE_ORIGINAL, "3"),
                doc(
                    "harbor-charter-en",
                    "harbor-charter",
                    "港湾通行章程",
                    "Harbor Passage Charter",
                    "en",
                    ROLE_TRANSLATION,
                    "2",
                    translation_of="/items/harbor-charter/harbor-charter-zh",
                    translation_source_version=3,
                ),
                doc(
                    "harbor-charter-fr",
                    "harbor-charter",
                    "港湾通行章程",
                    "Charte de passage portuaire",
                    "fr",
                    ROLE_TRANSLATION,
                    "1",
                    translation_of="/items/harbor-charter/harbor-charter-zh",
                    translation_source_version=3,
                ),
            ],
        )
    )

    # Withdrawn French translation, replacement English translation published.
    site.add_item(
        Item(
            "signal-manual",
            "信号灯维护手册",
            [
                doc("signal-manual-zh", "signal-manual", "信号灯维护手册", "信号灯维护手册", "zh", ROLE_ORIGINAL, "5"),
                doc(
                    "signal-manual-en",
                    "signal-manual",
                    "信号灯维护手册",
                    "Signal Lamp Maintenance Manual",
                    "en",
                    ROLE_TRANSLATION,
                    "3",
                    translation_of="/items/signal-manual/signal-manual-zh",
                    translation_source_version=4,
                ),
                doc(
                    "signal-manual-fr",
                    "signal-manual",
                    "信号灯维护手册",
                    "Manuel de maintenance des signaux",
                    "fr",
                    ROLE_TRANSLATION,
                    "2",
                    translation_of="/items/signal-manual/signal-manual-zh",
                    translation_source_version=4,
                    status="withdrawn",
                    withdrawal_reason="译文与原文第 5 版不一致，来源撤回该译文。",
                    withdrawn_at="2026-10-02",
                ),
            ],
        )
    )
    # Old French doc points at its replacement.
    french_old = site.items["signal-manual"].doc("signal-manual-fr")
    french_old.replaced_by = ["/items/signal-manual/signal-manual-fr-v2"]
    site.items["signal-manual"].documents.append(
        doc(
            "signal-manual-fr-v2",
            "signal-manual",
            "信号灯维护手册",
            "Manuel de maintenance des signaux (édition 2026)",
            "fr",
            ROLE_TRANSLATION,
            "1",
            translation_of="/items/signal-manual/signal-manual-zh",
            translation_source_version=5,
            replaces=["/items/signal-manual/signal-manual-fr"],
        )
    )

    site.add_item(
        Item(
            "tide-tables",
            "潮汐表说明",
            [
                doc("tide-tables-zh", "tide-tables", "潮汐表说明", "潮汐表说明", "zh", ROLE_ORIGINAL, "1"),
                doc(
                    "tide-tables-de",
                    "tide-tables",
                    "潮汐表说明",
                    "Gezeitenkalender Hinweise",
                    "de",
                    ROLE_TRANSLATION,
                    "1",
                    translation_of="/items/tide-tables/tide-tables-zh",
                    translation_source_version=1,
                ),
            ],
        )
    )

    site.add_item(
        Item(
            "beacon-register",
            "灯塔登记册",
            [
                doc("beacon-register-zh", "beacon-register", "灯塔登记册", "灯塔登记册", "zh", ROLE_ORIGINAL, "1"),
                doc(
                    "beacon-register-ja",
                    "beacon-register",
                    "灯塔登记册",
                    "灯台登録簿",
                    "ja",
                    ROLE_TRANSLATION,
                    "1",
                    translation_of="/items/beacon-register/beacon-register-zh",
                    translation_source_version=1,
                ),
            ],
        )
    )
    return site


def make_server(port: int = 0, *, fixture: bool = True) -> ThreadingHTTPServer:
    state = SiteState()
    if fixture:
        build_fixture(state)
    handler = type("BoundHandler", (Handler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.state = state  # type: ignore[attr-defined]
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fictional source site")
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--state-file", type=Path, help="unused, reserved")
    args = parser.parse_args()
    server = make_server(args.port)
    print(f"fictional source listening on http://127.0.0.1:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
