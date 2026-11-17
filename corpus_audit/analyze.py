"""Read-only analysis of one item, shared by verification and publication.

All facts are derived from *current* snapshots plus relation checks that were
made against those exact snapshots.  Checks produced for an older snapshot are
kept as history but can never revive a document or assert a link.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

POLICIES = ("quarantine", "independent")

SEVERITY_CONFLICT = "conflict"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"


def _int_version(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _load_docs(conn: sqlite3.Connection, item_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT d.doc_id, d.item_id, d.language AS doc_language, d.role AS doc_role,
               d.url, d.currently_listed, d.current_snapshot_id, d.last_checked_at,
               d.last_http_status, d.last_error,
               s.snapshot_id, s.title, s.version, s.status AS snap_status,
               s.translation_of, s.source_version, s.observed_seq, s.fetched_at,
               s.http_status, s.etag, s.last_modified, s.body_sha256, s.raw_path,
               s.parsed_json
        FROM documents d
        JOIN snapshots s ON s.snapshot_id = d.current_snapshot_id
        WHERE d.item_id = ?
        ORDER BY d.language, d.first_seen_seq
        """,
        (item_id,),
    ).fetchall()
    docs = []
    for row in rows:
        doc = dict(row)
        doc["parsed"] = json.loads(doc["parsed_json"])
        if doc["snap_status"] == "invalid":
            doc["state"] = "invalid"
            # The stored metadata is untrusted; keep only what the document
            # row still knows, so view code never reads invented fields.
            doc["parsed"] = {
                "language": doc["doc_language"],
                "role": doc["doc_role"],
                "withdrawal": None,
                "translation_of_url": None,
                "replaces_urls": [],
                "replaced_by_urls": [],
                "invalid": doc["parsed"].get("invalid", {}),
            }
        elif not doc["currently_listed"] or doc["snap_status"] == "superseded":
            doc["state"] = "unlisted"
        elif doc["snap_status"] == "withdrawn":
            doc["state"] = "withdrawn"
        elif doc["last_http_status"] in (404, 410):
            # The page the snapshot came from no longer exists.  The archived
            # snapshot stays auditable, but it cannot be presented as current.
            doc["state"] = "gone"
        else:
            doc["state"] = "active"
        docs.append(doc)
    return docs


def _latest_checks(conn: sqlite3.Connection, item_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM relation_checks WHERE item_id=? ORDER BY check_id",
        (item_id,),
    ).fetchall()
    latest: dict[tuple, dict] = {}
    for row in rows:
        check = dict(row)
        key = (
            check["kind"],
            check["source_doc"],
            check["source_snapshot_id"],
            check["target_url"],
        )
        latest[key] = check
    return list(latest.values())


def _by_current_claim(
    checks: list[dict[str, Any]], key: tuple[str, str, int, str]
) -> dict[str, Any] | None:
    return checks.get(key)


def _finding(
    findings: list[dict[str, Any]],
    code: str,
    severity: str,
    message: str,
    evidence: dict[str, Any],
    *,
    doc_id: str | None = None,
    item_id: str | None = None,
) -> None:
    for existing in findings:
        if existing["code"] == code and existing.get("doc_id") == doc_id:
            return
    findings.append(
        {
            "code": code,
            "severity": severity,
            "message": message,
            "evidence": evidence,
            "doc_id": doc_id,
            "item_id": item_id,
        }
    )


def _find_cycle(edges: list[tuple[str, str]]) -> list[str] | None:
    graph: dict[str, list[str]] = {}
    for source, target in edges:
        graph.setdefault(source, []).append(target)
        graph.setdefault(target, [])
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for neighbour in sorted(graph.get(node, [])):
            if state.get(neighbour, 0) == 0:
                found = visit(neighbour)
                if found:
                    return found
            elif state.get(neighbour) == 1:
                index = stack.index(neighbour)
                return stack[index:] + [neighbour]
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(graph):
        if state.get(node, 0) == 0:
            found = visit(node)
            if found:
                return found
    return None


def analyze_item(
    conn: sqlite3.Connection, item_id: str, policy: str
) -> dict[str, Any]:
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    item = conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
    if item is None:
        raise KeyError(item_id)
    docs = _load_docs(conn, item_id)
    doc_by_url = {doc["url"]: doc for doc in docs}
    doc_by_id = {doc["doc_id"]: doc for doc in docs}
    checks = _latest_checks(conn, item_id)
    check_index = {
        (c["kind"], c["source_doc"], c["source_snapshot_id"], c["target_url"]): c
        for c in checks
    }
    findings: list[dict[str, Any]] = []

    def add(code, severity, message, evidence, doc_id=None):
        _finding(
            findings,
            code,
            severity,
            message,
            evidence,
            doc_id=doc_id,
            item_id=item_id,
        )

    def check_for(doc, kind, target_url):
        return _by_current_claim(
            check_index,
            (kind, doc["doc_id"], doc["snapshot_id"], target_url),
        )

    active_docs = [doc for doc in docs if doc["state"] == "active"]
    if docs and not active_docs and all(
        doc["state"] in ("unlisted", "gone") for doc in docs
    ):
        add(
            "item.unlisted",
            SEVERITY_CONFLICT,
            "该资料已不再由来源目录提供，全部语言版本停止展示；历史快照仅作审计。",
            {
                "documents": [
                    {"doc_id": d["doc_id"], "state": d["state"]} for d in docs
                ]
            },
        )
    originals = [
        doc
        for doc in active_docs
        if doc["snap_status"] == "active" and doc["parsed"]["role"] == "original"
    ]
    translations = [
        doc
        for doc in active_docs
        if doc["parsed"]["role"] == "translation"
    ]
    withdrawn_originals = [
        doc for doc in docs if doc["state"] == "withdrawn" and doc["parsed"]["role"] == "original"
    ]
    for doc in docs:
        if doc["state"] == "gone":
            add(
                "document.source_gone",
                SEVERITY_CONFLICT,
                "来源页面返回 404/410，当前状态无法确认；旧快照只保留为历史证据，不能当作可用版本。",
                {
                    "doc_id": doc["doc_id"],
                    "http_status": doc["last_http_status"],
                    "last_checked_at": doc["last_checked_at"],
                    "snapshot_id": doc["snapshot_id"],
                    "raw_url": f"/raw/{doc['doc_id']}/{doc['snapshot_id']}",
                },
                doc_id=doc["doc_id"],
            )
    for doc in docs:
        if doc["state"] != "invalid":
            continue
        add(
            "document.invalid_snapshot",
            SEVERITY_CONFLICT,
            "最新抓取无法解析为可信元数据；该语言版本当前状态不可确认。",
            {
                "doc_id": doc["doc_id"],
                "snapshot_id": doc["snapshot_id"],
                "error": doc["parsed"].get("invalid", {}),
                "sha256": doc["body_sha256"],
                "raw_url": f"/raw/{doc['doc_id']}/{doc['snapshot_id']}",
            },
            doc_id=doc["doc_id"],
        )

    if not originals:
        if translations:
            add(
                "original.missing",
                SEVERITY_CONFLICT,
                "存在可用译文，但找不到可用的原文版本。译文不能替代原文发布。",
                {"active_translations": [d["doc_id"] for d in translations]},
            )
        elif withdrawn_originals:
            add(
                "original.withdrawn",
                SEVERITY_WARNING,
                "原文已撤回，且没有可用的替代原文；资料没有可发布内容。",
                {"withdrawn_originals": [d["doc_id"] for d in withdrawn_originals]},
            )
        elif docs:
            add(
                "original.missing",
                SEVERITY_CONFLICT,
                "没有可用的原文版本。",
                {"documents": [d["doc_id"] for d in docs]},
            )
    if len(originals) > 1:
        add(
            "original.multiple",
            SEVERITY_CONFLICT,
            "同一资料存在多个可用原文版本，无法确定语言对应基准。",
            {
                "originals": [
                    {"doc_id": d["doc_id"], "language": d["parsed"]["language"], "version": d["version"]}
                    for d in originals
                ]
            },
        )

    language_groups: dict[str, list[dict]] = {}
    for doc in active_docs:
        language_groups.setdefault(doc["parsed"]["language"], []).append(doc)
    for language, group in sorted(language_groups.items()):
        if len(group) > 1:
            add(
                "language.duplicate",
                SEVERITY_CONFLICT,
                f"语言 {language} 同时存在 {len(group)} 个可用版本。",
                {
                    "language": language,
                    "documents": [
                        {"doc_id": d["doc_id"], "version": d["version"], "role": d["parsed"]["role"]}
                        for d in group
                    ],
                },
            )

    # ---- translation correspondence --------------------------------------
    for doc in active_docs:
        if doc["parsed"]["role"] != "translation":
            continue
        target_url = doc["translation_of"]
        target = doc_by_url.get(target_url) if target_url else None
        check = check_for(doc, "translation_of", target_url) if target_url else None
        declared = doc["source_version"]
        info: dict[str, Any] = {
            "target_url": target_url,
            "declared_source_version": declared,
            "target_doc_id": target["doc_id"] if target else None,
        }
        if not target_url:
            add(
                "translation.original_unknown",
                SEVERITY_CONFLICT,
                "译文没有声明对应原文地址。",
                info,
                doc_id=doc["doc_id"],
            )
            continue
        if target is None:
            if check and check["state"] == "verified":
                evidence = json.loads(check["evidence_json"])
                info["target_language"] = evidence.get("language")
                info["target_version"] = evidence.get("version")
                info["relation_state"] = "verified_external"
                add(
                    "translation.original_uncollected",
                    SEVERITY_WARNING,
                    "译文对应原文不在本次采集的同一资料组内；只能依据关系核对证据展示。",
                    info,
                    doc_id=doc["doc_id"],
                )
            elif check and check["state"] == "rejected":
                info["relation_state"] = "rejected"
                add(
                    "translation.original_unavailable",
                    SEVERITY_CONFLICT,
                    "译文声明的原文地址无法作为有效原文核对。",
                    {**info, "reasons": json.loads(check["reasons_json"])},
                    doc_id=doc["doc_id"],
                )
            else:
                info["relation_state"] = "pending"
                add(
                    "relation.pending",
                    SEVERITY_WARNING,
                    "译文对应原文的关系尚未核对完成；暂不声明该语言对应。",
                    info,
                    doc_id=doc["doc_id"],
                )
            continue

        target_parsed = target["parsed"]
        info.update(
            {
                "target_language": target_parsed["language"],
                "target_version": target["version"],
            }
        )
        if target_parsed["role"] != "original":
            info["relation_state"] = "rejected"
            add(
                "translation.chain",
                SEVERITY_CONFLICT,
                "译文指向的仍是译文，禁止沿译文形成对应链。",
                info,
                doc_id=doc["doc_id"],
            )
        elif target_parsed["language"] == doc["parsed"]["language"]:
            info["relation_state"] = "rejected"
            add(
                "translation.language_same",
                SEVERITY_CONFLICT,
                "译文与所对应原文声明了相同语言。",
                info,
                doc_id=doc["doc_id"],
            )
        elif target["state"] != "active":
            info["relation_state"] = "rejected"
            add(
                "translation.original_unavailable",
                SEVERITY_CONFLICT,
                "译文对应原文的当前状态不是可用。",
                {**info, "target_state": target["state"]},
                doc_id=doc["doc_id"],
            )
        else:
            current_version = _int_version(target["version"])
            if declared is None:
                add(
                    "translation.version_unknown",
                    SEVERITY_WARNING,
                    "译文没有可比较的原文版本号。",
                    info,
                    doc_id=doc["doc_id"],
                )
            elif current_version is None:
                add(
                    "translation.version_unknown",
                    SEVERITY_WARNING,
                    "原文版本号不是整数，无法判断译文是否滞后。",
                    info,
                    doc_id=doc["doc_id"],
                )
            elif declared < current_version:
                add(
                    "translation.stale",
                    SEVERITY_WARNING,
                    f"译文基于原文第 {declared} 版，原文当前第 {current_version} 版；这是译文自身信息，不代表原文发生了更新。",
                    info,
                    doc_id=doc["doc_id"],
                )
            elif declared > current_version:
                add(
                    "translation.ahead",
                    SEVERITY_CONFLICT,
                    f"译文声明基于原文第 {declared} 版，但原文当前只有第 {current_version} 版。",
                    info,
                    doc_id=doc["doc_id"],
                )
        if check is None:
            info["relation_state"] = info.get("relation_state") or "pending"
            add(
                "relation.pending",
                SEVERITY_WARNING,
                "译文与原文的对应关系尚未由核对器确认。",
                info,
                doc_id=doc["doc_id"],
            )
        elif check["state"] == "rejected":
            info["relation_state"] = "rejected"
            add(
                "translation.original_unavailable",
                SEVERITY_CONFLICT,
                "核对器拒绝了译文声明的原文对应关系。",
                {**info, "reasons": json.loads(check["reasons_json"])},
                doc_id=doc["doc_id"],
            )
        elif check["state"] == "stale":
            info["relation_state"] = "stale"
            add(
                "relation.check_stale",
                SEVERITY_WARNING,
                "译文对应关系的核对结果基于旧快照，等待重新核对。",
                {**info, "check": check["check_id"]},
                doc_id=doc["doc_id"],
            )
        elif check["state"] == "pending":
            info["relation_state"] = "pending"
            exhausted = bool(
                (json.loads(check["evidence_json"]) or {}).get("retry_exhausted")
            )
            add(
                "relation.pending_exhausted" if exhausted else "relation.pending",
                SEVERITY_WARNING,
                "原文暂时无法抓取，重试次数已用尽；对应关系仍未确认。"
                if exhausted
                else "译文与原文的对应关系尚未由核对器确认。",
                {
                    **info,
                    "reasons": json.loads(check["reasons_json"]),
                },
                doc_id=doc["doc_id"],
            )
        elif check["state"] == "verified":
            if check["target_snapshot_id"] == target["snapshot_id"]:
                info["relation_state"] = "verified"
            else:
                info["relation_state"] = "pending"
                add(
                    "relation.target_changed",
                    SEVERITY_WARNING,
                    "核对时原文快照与当前原文快照不一致，对应关系需重新核对。",
                    {**info, "checked_target_snapshot": check["target_snapshot_id"]},
                    doc_id=doc["doc_id"],
                )

    # ---- replacement claims ----------------------------------------------
    applied_edges: list[tuple[str, str]] = []
    edge_labels: dict[tuple[str, str], dict[str, Any]] = {}
    replacement_views: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        parsed = doc["parsed"]
        views: list[dict[str, Any]] = []
        for kind, urls in (
            ("replaces", parsed.get("replaces_urls") or []),
            ("replaced_by", parsed.get("replaced_by_urls") or []),
        ):
            for target_url in urls:
                target = doc_by_url.get(target_url)
                check = check_for(doc, kind, target_url)
                view: dict[str, Any] = {
                    "kind": kind,
                    "target_url": target_url,
                    "target_doc_id": target["doc_id"] if target else None,
                    "target_language": (
                        target["parsed"].get("language") or target["doc_language"]
                        if target
                        else None
                    ),
                    "target_state": target["state"] if target else "uncollected",
                    "relation_state": "pending",
                    "reasons": [],
                    "checked_at": check["checked_at"] if check else None,
                }
                if check:
                    view["reasons"] = json.loads(check["reasons_json"])
                if check is None:
                    add(
                        "relation.pending",
                        SEVERITY_WARNING,
                        "替代关系尚未核对完成；该关系不会在目录中生效。",
                        {"doc_id": doc["doc_id"], "kind": kind, "target_url": target_url},
                        doc_id=doc["doc_id"],
                    )
                elif check["state"] == "pending":
                    view["relation_state"] = "pending"
                    exhausted = bool(
                        (json.loads(check["evidence_json"]) or {}).get("retry_exhausted")
                    )
                    add(
                        "relation.pending_exhausted" if exhausted else "relation.pending",
                        SEVERITY_WARNING,
                        "替代关系的目标暂时无法抓取，重试次数已用尽；关系不会在目录中生效，"
                        "恢复后需重新核对。"
                        if exhausted
                        else "替代关系尚未核对完成；该关系不会在目录中生效。",
                        {
                            "doc_id": doc["doc_id"],
                            "kind": kind,
                            "target_url": target_url,
                            "reasons": view["reasons"],
                        },
                        doc_id=doc["doc_id"],
                    )
                elif check["state"] == "rejected":
                    view["relation_state"] = "rejected"
                    add(
                        "relation.rejected",
                        SEVERITY_CONFLICT,
                        "核对器拒绝了来源声明的替代关系。",
                        {
                            "doc_id": doc["doc_id"],
                            "kind": kind,
                            "target_url": target_url,
                            "reasons": view["reasons"],
                        },
                        doc_id=doc["doc_id"],
                    )
                elif check["state"] == "stale":
                    view["relation_state"] = "stale"
                    add(
                        "relation.check_stale",
                        SEVERITY_WARNING,
                        "替代关系的核对结果基于旧快照，暂不生效。",
                        {"doc_id": doc["doc_id"], "kind": kind, "target_url": target_url},
                        doc_id=doc["doc_id"],
                    )
                else:  # verified
                    if target is None:
                        view["relation_state"] = "verified_external"
                        add(
                            "relation.target_uncollected",
                            SEVERITY_WARNING,
                            "替代目标不在本次采集的资料组内，只能展示核对外部证据。",
                            {"doc_id": doc["doc_id"], "kind": kind, "target_url": target_url},
                            doc_id=doc["doc_id"],
                        )
                    elif (
                        target["state"] == "withdrawn"
                        and doc["state"] == "active"
                        and kind == "replaced_by"
                    ):
                        # An active document claiming to be replaced by a
                        # withdrawn one leaves no current version.  The
                        # reverse direction (a new version replacing a
                        # withdrawn old one) is the normal withdrawal path.
                        view["relation_state"] = "rejected"
                        add(
                            "relation.target_withdrawn",
                            SEVERITY_CONFLICT,
                            "可用版本声称被已撤回文档替代，当前版本无从确定。",
                            {"doc_id": doc["doc_id"], "kind": kind, "target_url": target_url},
                            doc_id=doc["doc_id"],
                        )
                    elif target["state"] == "invalid":
                        view["relation_state"] = "rejected"
                        add(
                            "relation.target_invalid",
                            SEVERITY_CONFLICT,
                            "替代目标的最新快照无法解析，关系暂不成立。",
                            {"doc_id": doc["doc_id"], "kind": kind, "target_url": target_url},
                            doc_id=doc["doc_id"],
                        )
                    elif target["state"] == "gone":
                        view["relation_state"] = "stale"
                        add(
                            "relation.target_gone",
                            SEVERITY_WARNING,
                            "替代目标的来源页面已不存在，关系暂不生效。",
                            {"doc_id": doc["doc_id"], "kind": kind, "target_url": target_url},
                            doc_id=doc["doc_id"],
                        )
                    elif target["state"] == "unlisted":
                        view["relation_state"] = "unlisted"
                        add(
                            "relation.target_unlisted",
                            SEVERITY_WARNING,
                            "替代目标已不再从资料页链接，关系暂不生效。",
                            {"doc_id": doc["doc_id"], "kind": kind, "target_url": target_url},
                            doc_id=doc["doc_id"],
                        )
                    elif target["parsed"]["language"] != doc["parsed"]["language"]:
                        view["relation_state"] = "rejected"
                        add(
                            "relation.language_mismatch",
                            SEVERITY_CONFLICT,
                            "替代关系两端语言不一致。",
                            {
                                "doc_id": doc["doc_id"],
                                "kind": kind,
                                "source_language": doc["parsed"]["language"],
                                "target_language": target["parsed"]["language"],
                            },
                            doc_id=doc["doc_id"],
                        )
                    elif check["target_snapshot_id"] != target["snapshot_id"]:
                        view["relation_state"] = "stale"
                        add(
                            "relation.target_changed",
                            SEVERITY_WARNING,
                            "核对时替代目标快照与当前快照不一致，关系暂不生效。",
                            {"doc_id": doc["doc_id"], "kind": kind, "target_url": target_url},
                            doc_id=doc["doc_id"],
                        )
                    else:
                        view["relation_state"] = "verified"
                        older, newer = (
                            (doc["doc_id"], target["doc_id"])
                            if kind == "replaced_by"
                            else (target["doc_id"], doc["doc_id"])
                        )
                        applied_edges.append((older, newer))
                        edge_labels[(older, newer)] = {
                            "older": older,
                            "newer": newer,
                            "language": doc["parsed"]["language"],
                            "via": f"{doc['doc_id']}.{kind}",
                        }
                        # Reciprocal declaration is desirable but the source
                        # may publish only one side; expose the gap, don't block.
                        target_claims = (
                            target["parsed"].get("replaces_urls") or []
                            if kind == "replaced_by"
                            else target["parsed"].get("replaced_by_urls") or []
                        )
                        if doc["url"] not in target_claims:
                            view["reasons"] = sorted(
                                set(view["reasons"]) | {"reciprocal_missing"}
                            )
                            add(
                                "relation.reciprocal_missing",
                                SEVERITY_WARNING,
                                "替代关系只有单向声明；已按单向证据记录。",
                                {
                                    "doc_id": doc["doc_id"],
                                    "kind": kind,
                                    "target_url": target_url,
                                },
                                doc_id=doc["doc_id"],
                            )
                views.append(view)
        replacement_views[doc["doc_id"]] = views

    # Multiple *active* replacement targets make current availability ambiguous.
    for doc in docs:
        active_replacements = [
            view
            for view in replacement_views.get(doc["doc_id"], [])
            if view["kind"] == "replaced_by" and view["relation_state"] == "verified"
        ]
        active_targets = [
            view
            for view in active_replacements
            if view["target_doc_id"]
            and doc_by_id[view["target_doc_id"]]["state"] == "active"
        ]
        if len(active_targets) > 1:
            add(
                "relation.ambiguous_replacement",
                SEVERITY_CONFLICT,
                "同一版本存在多个可用替代版本，无法确定当前版本。",
                {"doc_id": doc["doc_id"], "targets": [v["target_url"] for v in active_targets]},
                doc_id=doc["doc_id"],
            )

    cycle = _find_cycle(applied_edges)
    if cycle:
        add(
            "relation.cycle",
            SEVERITY_CONFLICT,
            "替代关系形成循环，整组关系不可发布。",
            {
                "cycle": cycle,
                "edges": [edge_labels[e] for e in applied_edges if e[0] in cycle],
            },
        )

    # Withdrawal lineage completeness.
    for doc in docs:
        if doc["state"] != "withdrawn":
            continue
        replacements = [
            view
            for view in replacement_views.get(doc["doc_id"], [])
            if view["kind"] == "replaced_by" and view["relation_state"] == "verified"
        ]
        if not replacements:
            add(
                "withdrawal.without_replacement",
                SEVERITY_WARNING,
                "该版本已撤回，但没有可用替代版本。",
                {"doc_id": doc["doc_id"]},
                doc_id=doc["doc_id"],
            )
        else:
            reachable_active = False
            seen: set[str] = set()
            frontier = [doc["doc_id"]]
            while frontier and not reachable_active:
                current = frontier.pop()
                if current in seen:
                    continue
                seen.add(current)
                for view in replacement_views.get(current, []):
                    target_id = view["target_doc_id"]
                    if not target_id:
                        continue
                    if doc_by_id[target_id]["state"] == "active":
                        reachable_active = True
                        break
                    frontier.append(target_id)
            if not reachable_active:
                add(
                    "withdrawal.no_active_replacement",
                    SEVERITY_WARNING,
                    "撤回链没有到达任何可用版本。",
                    {"doc_id": doc["doc_id"]},
                    doc_id=doc["doc_id"],
                )

    # ---- build view -------------------------------------------------------
    withdrawal_rows = {}
    if docs:
        placeholders = ",".join("?" for _ in docs)
        for row in conn.execute(
            f"SELECT * FROM withdrawal_evidence WHERE snapshot_id IN ({placeholders})",
            [doc["snapshot_id"] for doc in docs],
        ):
            withdrawal_rows[row["snapshot_id"]] = dict(row)

    def doc_view(doc: dict[str, Any]) -> dict[str, Any]:
        evidence = withdrawal_rows.get(doc["snapshot_id"])
        withdrawal = None
        if doc["state"] == "withdrawn":
            parsed_withdrawal = doc["parsed"].get("withdrawal") or {}
            withdrawal = {
                "reason": parsed_withdrawal.get("reason"),
                "withdrawn_at": parsed_withdrawal.get("withdrawn_at"),
                "basis_snapshot_id": doc["snapshot_id"],
                "sha256": doc["body_sha256"],
                "http_status": doc["http_status"],
                "fetched_at": doc["fetched_at"],
                "raw_url": f"/raw/{doc['doc_id']}/{doc['snapshot_id']}",
                "source_url": doc["url"],
                "archived": bool(evidence and evidence.get("raw_path")),
            }
        translation = None
        if doc["parsed"]["role"] == "translation" and doc["state"] != "invalid":
            target_url = doc["translation_of"]
            target = doc_by_url.get(target_url)
            check = check_for(doc, "translation_of", target_url) if target_url else None
            translation = {
                "target_url": target_url,
                "target_doc_id": target["doc_id"] if target else None,
                "target_language": target["parsed"]["language"] if target else None,
                "target_version": target["version"] if target else None,
                "declared_source_version": doc["source_version"],
                "relation_state": (
                    "verified"
                    if check
                    and check["state"] == "verified"
                    and target
                    and check["target_snapshot_id"] == target["snapshot_id"]
                    else ("pending" if not check else check["state"])
                ),
            }
        return {
            "doc_id": doc["doc_id"],
            "language": doc["parsed"]["language"],
            "role": doc["parsed"]["role"],
            "title": doc["title"],
            "version": doc["version"],
            "state": doc["state"],
            "listed": bool(doc["currently_listed"]),
            "url": doc["url"],
            "snapshot_id": doc["snapshot_id"],
            "observed_seq": doc["observed_seq"],
            "fetched_at": doc["fetched_at"],
            "http_status": doc["http_status"],
            "etag": doc["etag"],
            "last_modified": doc["last_modified"],
            "sha256": doc["body_sha256"],
            "raw_url": f"/raw/{doc['doc_id']}/{doc['snapshot_id']}",
            "withdrawal": withdrawal,
            "translation": translation,
            "replacements": replacement_views.get(doc["doc_id"], []),
        }

    language_matrix: dict[str, dict[str, Any]] = {}
    for doc in docs:
        if doc["state"] in ("unlisted", "invalid", "gone"):
            # An unverifiable snapshot cannot support a language correspondence.
            continue
        language = doc["parsed"]["language"]
        entry = language_matrix.setdefault(
            language, {"language": language, "original": None, "translations": []}
        )
        if doc["parsed"]["role"] == "original":
            entry["original"] = doc_view(doc)
        else:
            entry["translations"].append(doc_view(doc))

    conflicts = [f for f in findings if f["severity"] == SEVERITY_CONFLICT]
    warnings = [f for f in findings if f["severity"] == SEVERITY_WARNING]
    state = "conflict" if conflicts else ("degraded" if warnings else "ok")
    relation_counts = {"verified": 0, "pending": 0, "rejected": 0, "stale": 0, "other": 0}
    for view_list in replacement_views.values():
        for view in view_list:
            key = view["relation_state"]
            if key in relation_counts:
                relation_counts[key] += 1
            else:
                relation_counts["other"] += 1

    impact = None
    if conflicts:
        if policy == "quarantine":
            impact = {
                "availability": "整组资料暂停对外发布：目录中不展示该资料，也不提供任何语言版本。",
                "completeness": "不输出可能有冲突的关系，避免把错误关系对外传播；代价是整组资料在修正前不可用。",
                "action": "按页面列出的冲突证据修正来源或元数据，重新采集后只重新发布本资料。",
            }
        else:
            impact = {
                "availability": "保留各语言的独立记录，资料继续可用；不因单条关系冲突隐藏整组内容。",
                "completeness": "只有通过核对的关系才会生效；冲突关系标注为争议，语言对应与替代链完整性下降。",
                "action": "按页面列出的冲突证据修正；冲突关系不会在修正前被当作有效关系。",
            }
    elif warnings:
        impact = {
            "availability": "资料可用；提示项不阻断发布，但需要在页面上明确标注。",
            "completeness": "通过核对的关系已生效；提示项表示信息可能滞后、证据较弱或来源只给出单向声明。",
            "action": "按提示核对来源；修正后重新采集，只会重新发布受影响的资料。",
        }

    return {
        "item_id": item["item_id"],
        "title": item["title"],
        "original_language": item["original_language"],
        "policy": policy,
        "state": state,
        "findings": findings,
        "conflicts": len(conflicts),
        "warnings": len(warnings),
        "documents": [doc_view(doc) for doc in docs],
        "languages": [language_matrix[key] for key in sorted(language_matrix)],
        "relation_counts": relation_counts,
        "impact": impact,
        "availability": {
            "active_documents": len(active_docs),
            "withdrawn_documents": len([d for d in docs if d["state"] == "withdrawn"]),
            "unlisted_documents": len([d for d in docs if d["state"] == "unlisted"]),
            "invalid_documents": len(
                [d for d in docs if d["state"] in ("invalid", "gone")]
            ),
        },
    }
