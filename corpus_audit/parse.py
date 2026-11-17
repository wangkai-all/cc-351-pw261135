"""Parsing of the fictional source's HTML.

The source exposes a machine-readable CorpusDocument block in JSON-LD plus
ordinary navigation links.  The collector treats every response as untrusted:
missing or contradictory metadata is rejected rather than guessed at.
"""

from __future__ import annotations

import html.parser
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
DATE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
ROLES = {"original", "translation"}
STATUSES = {"active", "withdrawn"}


class ParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Withdrawal:
    reason: str
    withdrawn_at: str | None = None


@dataclass
class DocMetadata:
    document_id: str
    item_id: str
    item_title: str
    title: str
    language: str
    role: str
    version: str
    status: str
    date_modified: str | None
    translation_of: str | None
    translation_source_version: int | None
    replaces: list[str] = field(default_factory=list)
    replaced_by: list[str] = field(default_factory=list)
    withdrawal: Withdrawal | None = None
    body_text: str = ""


@dataclass(frozen=True)
class CatalogEntry:
    item_id: str
    title: str
    url: str


@dataclass(frozen=True)
class VariantLink:
    language: str
    url: str


class _DocumentParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.article_parts: list[str] = []
        self.json_blocks: list[str] = []
        self._in_title = False
        self._in_script = False
        self._in_article = False
        self._script_type = ""
        self._json_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "script":
            self._script_type = (attrs.get("type") or "").lower()
            if "ld+json" in self._script_type:
                self._in_script = True
                self._json_buf = []
        elif tag == "article":
            self._in_article = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script" and self._in_script:
            self._in_script = False
            self.json_blocks.append("".join(self._json_buf))
            self._json_buf = []
        elif tag == "article":
            self._in_article = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_script:
            self._json_buf.append(data)
        elif self._in_article:
            self.article_parts.append(data)


def _one_meta(blocks: list[str]) -> dict:
    metas = []
    for raw in blocks:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ParseError("bad_jsonld", f"JSON-LD 无法解析: {exc}") from exc
        if isinstance(value, dict) and value.get("@type") == "CorpusDocument":
            metas.append(value)
        elif isinstance(value, list):
            metas.extend(
                item
                for item in value
                if isinstance(item, dict) and item.get("@type") == "CorpusDocument"
            )
    if not metas:
        raise ParseError("missing_jsonld", "页面缺少 CorpusDocument JSON-LD")
    if len(metas) > 1:
        raise ParseError("duplicate_jsonld", "页面包含多个 CorpusDocument JSON-LD")
    return metas[0]


def _text(value, field_name: str, *, max_len: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ParseError("bad_metadata", f"字段 {field_name} 缺失或不是字符串")
    value = value.strip()
    if len(value) > max_len:
        raise ParseError("bad_metadata", f"字段 {field_name} 过长")
    return value


def _link_list(value, field_name: str) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ParseError("bad_metadata", f"字段 {field_name} 必须是字符串或字符串数组")


def parse_document(html_text: str, page_url: str) -> DocMetadata:
    parser = _DocumentParser()
    try:
        parser.feed(html_text)
    except Exception as exc:  # html.parser can reject malformed markup
        raise ParseError("bad_html", f"HTML 无法解析: {exc}") from exc
    meta = _one_meta(parser.json_blocks)

    document_id = _text(meta.get("documentId"), "documentId", max_len=200)
    item_id = _text(meta.get("itemId"), "itemId", max_len=200)
    item_title = _text(meta.get("itemTitle"), "itemTitle", max_len=300)
    title = _text(meta.get("name"), "name", max_len=300)
    language = _text(meta.get("inLanguage"), "inLanguage", max_len=35)
    role = _text(meta.get("role"), "role", max_len=20)
    status = _text(meta.get("status"), "status", max_len=20)
    version = str(meta.get("version", "")).strip()
    if not version or len(version) > 100:
        raise ParseError("bad_metadata", "字段 version 缺失或非法")
    if not LANGUAGE_RE.match(language):
        raise ParseError("bad_metadata", f"语言代码非法: {language!r}")
    if role not in ROLES:
        raise ParseError("bad_metadata", f"role 必须是 original 或 translation，实际 {role!r}")
    if status not in STATUSES:
        raise ParseError("bad_metadata", f"status 必须是 active 或 withdrawn，实际 {status!r}")

    date_modified = meta.get("dateModified")
    if date_modified is not None:
        date_modified = _text(date_modified, "dateModified", max_len=40)
        if not DATE_RE.match(date_modified):
            raise ParseError("bad_metadata", f"dateModified 格式非法: {date_modified!r}")

    translation_of = meta.get("translationOf")
    if translation_of is not None:
        translation_of = urljoin(page_url, _text(translation_of, "translationOf", max_len=2000))
    if role == "translation" and not translation_of:
        raise ParseError("bad_metadata", "译文必须声明 translationOf")
    if role == "original" and translation_of:
        raise ParseError("bad_metadata", "原文不应声明 translationOf")

    source_version = meta.get("translationSourceVersion")
    if source_version is not None:
        if isinstance(source_version, bool) or not isinstance(source_version, int) or source_version < 1:
            raise ParseError("bad_metadata", "translationSourceVersion 必须是正整数")
    if role == "original" and source_version is not None:
        raise ParseError("bad_metadata", "原文不应声明 translationSourceVersion")
    if role == "translation" and source_version is None:
        raise ParseError("bad_metadata", "译文必须声明 translationSourceVersion")

    replaces = [
        urljoin(page_url, value)
        for value in _link_list(meta.get("replaces"), "replaces")
    ]
    replaced_by = [
        urljoin(page_url, value)
        for value in _link_list(meta.get("replacedBy"), "replacedBy")
    ]
    if page_url in replaces or page_url in replaced_by:
        raise ParseError("self_relation", "页面声明了指向自身的替代关系")

    withdrawal = None
    raw_withdrawal = meta.get("withdrawal")
    if status == "withdrawn":
        if not isinstance(raw_withdrawal, dict):
            raise ParseError("withdrawal_basis_missing", "已撤回页面缺少 withdrawal 依据")
        reason = _text(raw_withdrawal.get("reason"), "withdrawal.reason", max_len=1000)
        withdrawn_at = raw_withdrawal.get("withdrawnAt")
        if withdrawn_at is not None:
            withdrawn_at = _text(withdrawn_at, "withdrawal.withdrawnAt", max_len=40)
            if not DATE_RE.match(withdrawn_at):
                raise ParseError("bad_metadata", "withdrawnAt 格式非法")
        withdrawal = Withdrawal(reason=reason, withdrawn_at=withdrawn_at)
    elif raw_withdrawal:
        raise ParseError("bad_metadata", "active 文档不应携带 withdrawal 依据")

    body_text = " ".join(" ".join(parser.article_parts).split())[:20000]
    if not body_text:
        body_text = " ".join(" ".join(parser.title_parts).split())

    return DocMetadata(
        document_id=document_id,
        item_id=item_id,
        item_title=item_title,
        title=title,
        language=language,
        role=role,
        version=version,
        status=status,
        date_modified=date_modified,
        translation_of=translation_of,
        translation_source_version=source_version,
        replaces=replaces,
        replaced_by=replaced_by,
        withdrawal=withdrawal,
        body_text=body_text,
    )


class _CatalogParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[CatalogEntry] = []
        self.title_parts: list[str] = []
        self._in_title = False
        self._anchor: dict | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            rels = (attrs.get("rel") or "").lower().split()
            if "item" in rels:
                self._anchor = attrs
                self._text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._anchor is not None:
            href = self._anchor.get("href")
            item_id = self._anchor.get("data-item-id")
            title = self._anchor.get("data-item-title") or " ".join(self._text).strip()
            if href and item_id and title:
                self.entries.append(
                    CatalogEntry(item_id=item_id.strip(), title=title.strip(), url=href.strip())
                )
            self._anchor = None

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif self._anchor is not None:
            self._text.append(data)


def parse_catalog(html_text: str) -> list[CatalogEntry]:
    parser = _CatalogParser()
    try:
        parser.feed(html_text)
    except Exception as exc:
        raise ParseError("bad_html", f"目录 HTML 无法解析: {exc}") from exc
    seen: dict[str, CatalogEntry] = {}
    for entry in parser.entries:
        if entry.item_id in seen:
            raise ParseError("duplicate_item", f"目录重复列出资料 {entry.item_id!r}")
        seen[entry.item_id] = entry
    return list(seen.values())


class _ItemPageParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[VariantLink] = []
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "a":
            rels = (attrs.get("rel") or "").lower().split()
            href = attrs.get("href")
            lang = attrs.get("hreflang")
            if "alternate" in rels and href and lang:
                self.links.append(VariantLink(language=lang.strip(), url=href.strip()))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
        elif self._in_h1:
            self.h1_parts.append(data)


def parse_item_page(html_text: str, page_url: str) -> tuple[str, list[VariantLink]]:
    parser = _ItemPageParser()
    try:
        parser.feed(html_text)
    except Exception as exc:
        raise ParseError("bad_html", f"资料页 HTML 无法解析: {exc}") from exc
    title = " ".join("".join(parser.h1_parts).split()) or " ".join(
        "".join(parser.title_parts).split()
    )
    links = []
    seen = set()
    for link in parser.links:
        url = urljoin(page_url, link.url)
        key = (link.language, url)
        if key in seen:
            continue
        seen.add(key)
        links.append(VariantLink(language=link.language, url=url))
    return title, links


def iso_now() -> str:
    from corpus_audit.db import utcnow

    return utcnow()


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
