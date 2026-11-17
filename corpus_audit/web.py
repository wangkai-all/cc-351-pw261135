"""Read-only public catalog web UI.

Serves only committed publication payloads plus archived raw snapshots.  It
never writes to the database and exposes no mutation endpoints, so the public
surface can run next to the collector without widening its authority.
"""

from __future__ import annotations

import html
import json
import sqlite3
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from corpus_audit import db
from corpus_audit.static_paths import normalize_path

STATE_LABELS = {
    "ok": ("可用", "ok"),
    "degraded": ("可用（有提示）", "degraded"),
    "conflict_independent": ("独立记录（关系有争议）", "conflict"),
    "blocked": ("整组隔离", "blocked"),
}

SEVERITY_LABELS = {"conflict": "冲突", "warning": "提示", "info": "信息"}

POLICY_LABELS = {"quarantine": "隔离整组", "independent": "保留独立记录"}

STATE_TEXT = {
    "active": "可用",
    "withdrawn": "已撤回",
    "unlisted": "已下架",
    "invalid": "无法确认（元数据损坏）",
    "gone": "无法确认（来源页面不存在）",
}

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

CSS = """
:root {
  color-scheme: light dark;
  --bg: #f7f7f5; --panel: #ffffff; --ink: #1d2329; --muted: #5b6670;
  --line: #d9dee3; --accent: #1f5f8b; --ok: #1f6b45; --warn: #8a5a00;
  --bad: #9c2c2c; --chip: #eef2f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15191c; --panel: #1d2328; --ink: #e8ecef; --muted: #a1adb7;
    --line: #333c44; --accent: #7db7dc; --ok: #7fc7a0; --warn: #e0b166;
    --bad: #f09090; --chip: #262e35;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.6 "Noto Sans SC", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif; }
header { border-bottom: 1px solid var(--line); background: var(--panel); }
.wrap { max-width: 1080px; margin: 0 auto; padding: 0 20px; }
header .wrap { padding-top: 18px; padding-bottom: 18px; display: flex;
  justify-content: space-between; gap: 16px; align-items: baseline; flex-wrap: wrap; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
h1 { font-size: 1.45rem; margin: 0; }
h2 { font-size: 1.15rem; margin: 28px 0 10px; }
h3 { font-size: 1rem; margin: 20px 0 8px; }
main { padding: 26px 0 70px; }
.sub { color: var(--muted); font-size: .92rem; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 18px 20px; margin: 14px 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 14px; }
.item-card h3 { margin: 0 0 4px; font-size: 1.05rem; }
.chip { display: inline-block; border: 1px solid var(--line); background: var(--chip);
  border-radius: 999px; padding: 1px 10px; font-size: .82rem; margin: 2px 4px 2px 0; }
.chip.ok { color: var(--ok); border-color: currentColor; }
.chip.degraded, .chip.warning { color: var(--warn); border-color: currentColor; }
.chip.conflict, .chip.bad { color: var(--bad); border-color: currentColor; }
.chip.blocked { color: var(--bad); background: transparent; border-color: currentColor; font-weight: 600; }
.chip.muted { color: var(--muted); }
.notice { border-left: 4px solid var(--accent); padding: 10px 14px; background: var(--chip);
  border-radius: 4px; margin: 14px 0; }
.notice.bad { border-color: var(--bad); }
.notice.warn { border-color: var(--warn); }
table { width: 100%; border-collapse: collapse; font-size: .92rem; }
th, td { text-align: left; vertical-align: top; padding: 8px 10px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: .82rem; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: .86rem; overflow-wrap: anywhere; }
pre { background: var(--chip); border: 1px solid var(--line); border-radius: 6px;
  padding: 12px; overflow: auto; font-size: .84rem; white-space: pre-wrap; word-break: break-word; }
details { margin: 8px 0; }
summary { cursor: pointer; color: var(--accent); font-size: .9rem; }
ul.plain { list-style: none; padding-left: 0; margin: 6px 0; }
ul.plain li { padding: 3px 0; }
.badge-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 6px 0; }
.kv { display: grid; grid-template-columns: max-content 1fr; gap: 3px 14px; font-size: .9rem; }
.kv dt { color: var(--muted); }
.kv dd { margin: 0; }
footer { border-top: 1px solid var(--line); padding: 18px 0 40px; color: var(--muted); font-size: .86rem; }
.muted { color: var(--muted); }
.small { font-size: .84rem; }
.inline-list { display: flex; flex-wrap: wrap; gap: 4px 8px; padding: 0; margin: 0; list-style: none; }
hr { border: 0; border-top: 1px solid var(--line); margin: 22px 0; }
"""


def _e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def fmt_ts(value: str | None) -> str:
    if not value:
        return "—"
    return _e(value.replace("T", " ").replace("+00:00", " UTC"))


def fmt_short(value: str | None, length: int = 16) -> str:
    if not value:
        return "—"
    return _e(value if len(value) <= length else value[:length] + "…")


def page(title: str, body: str, *, policy: str | None = None) -> str:
    policy_note = (
        f'<span class="sub">冲突策略：{_e(POLICY_LABELS.get(policy, policy))}</span>'
        if policy
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · 多语言资料目录</title>
<style>{CSS}</style></head>
<body>
<header><div class="wrap">
  <div><a href="/"><strong>多语言资料目录</strong></a>
  <div class="sub">只读目录 · 内容来自来源站点的已核对快照</div></div>
  <div>{policy_note} <a href="/status" class="sub">运行状态</a></div>
</div></header>
<main class="wrap">{body}</main>
<footer class="wrap">本目录只展示已记录来源证据的公开快照。译文版本单独标注，不能据此推断原文已更新。</footer>
</body></html>"""


class Catalog:
    def __init__(self, db_path: str | Path, data_root: str | Path) -> None:
        self.db_path = Path(db_path)
        self.data_root = Path(data_root).resolve()

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path, readonly=True)

    # ---------------------------------------------------------------- pages
    def index(self) -> str:
        try:
            conn = self.connect()
        except sqlite3.Error as exc:
            return self._unavailable(str(exc))
        try:
            policy = db.get_setting(conn, "conflict_policy") or "quarantine"
            rows = conn.execute(
                "SELECT item_id, state, payload_json FROM publications ORDER BY item_id"
            ).fetchall()
        finally:
            conn.close()
        usable, blocked = [], []
        for row in rows:
            payload = json.loads(row["payload_json"])
            (blocked if payload.get("public_state") == "blocked" else usable).append(payload)
        body = [
            "<h1>资料目录</h1>",
            '<p class="sub">同一份资料的不同语言版本分开采集、核对和发布。'
            "冲突处理策略会明确影响可用性与完整性。</p>",
        ]
        if not rows:
            body.append(
                '<div class="notice">目录还没有可展示的资料。请先运行采集、关系核对与发布步骤。</div>'
            )
        if usable:
            body.append("<h2>可用资料</h2>")
            body.append('<div class="grid">')
            body.extend(self._item_card(p) for p in usable)
            body.append("</div>")
        if blocked:
            body.append("<h2>隔离区</h2>")
            body.append(
                '<div class="notice bad">以下整组资料因关系冲突暂停对外提供。'
                "页面仍保留来源证据、语言对应和冲突说明，供修正使用；"
                "修正后只重新发布受影响资料。</div>"
            )
            body.append('<div class="grid">')
            body.extend(self._item_card(p) for p in blocked)
            body.append("</div>")
        body.append(self._policy_box(policy))
        return page("资料目录", "".join(body), policy=policy)

    def _unavailable(self, error: str) -> str:
        return page(
            "目录不可用",
            '<div class="notice bad"><h1>目录不可用</h1>'
            f'<p>数据库无法读取：<code>{_e(error)}</code></p>'
            "<p>请先初始化数据库并运行采集与发布。</p></div>",
        )

    def _item_card(self, payload: dict[str, Any]) -> str:
        label, css = STATE_LABELS.get(payload.get("public_state", "ok"), ("未知", "muted"))
        chips = []
        for language in payload.get("languages", []):
            original = language.get("original")
            if original:
                chips.append(
                    f'<span class="chip">'
                    f"{_e(language['language'])} · 原文 v{_e(original['version'])}</span>"
                )
            translations = language.get("translations", [])
            active = [t for t in translations if t.get("state") == "active"]
            if not original and not active:
                chips.append(
                    f'<span class="chip muted">{_e(language["language"])} · '
                    "无可用版本</span>"
                )
            for translation in translations:
                if translation.get("state") == "active":
                    chips.append(
                        f'<span class="chip">{_e(translation["language"])} · 译文 v'
                        f'{_e(translation["version"])}</span>'
                    )
                elif translation.get("state") == "withdrawn":
                    chips.append(
                        f'<span class="chip muted">{_e(translation["language"])} · '
                        f'译文 v{_e(translation["version"])} 已撤回</span>'
                    )
                elif translation.get("state") == "invalid":
                    chips.append(
                        f'<span class="chip bad">{_e(translation["language"])} · 状态无法确认</span>'
                    )
        finding_note = ""
        if payload.get("conflicts"):
            finding_note = (
                f'<p class="small" style="color:var(--bad)">'
                f'{payload["conflicts"]} 项冲突 · {payload.get("warnings", 0)} 项提示</p>'
            )
        elif payload.get("warnings"):
            finding_note = (
                f'<p class="small" style="color:var(--warn)">'
                f'{payload["warnings"]} 项提示（不影响可用性）</p>'
            )
        original_language = payload.get("original_language") or "?"
        return (
            f'<div class="card item-card"><h3><a href="/items/{_e(payload["item_id"])}">'
            f'{_e(payload["title"])}</a></h3>'
            f'<div class="badge-row"><span class="chip {css}">{_e(label)}</span>'
            f'<span class="chip muted">原文语言 {_e(original_language)}</span></div>'
            f'<div class="badge-row">{"".join(chips)}</div>'
            f"{finding_note}"
            f'<p class="small muted">修订 {payload.get("revision", "—")} · 发布 '
            f'{fmt_ts(payload.get("generated_at"))}</p></div>'
        )

    def _policy_box(self, policy: str) -> str:
        if policy == "quarantine":
            text = (
                "<strong>当前策略：隔离整组。</strong>"
                "任一组内关系冲突时，整组资料都不作为对外可用资料展示；"
                "完整性优先，代价是整组在修正前不可用。"
            )
        else:
            text = (
                "<strong>当前策略：保留独立记录。</strong>"
                "每个语言版本仍可单独使用，只有核对通过的关系才生效；"
                "可用性优先，代价是冲突关系存在时语言对应与替代链不完整。"
            )
        return f'<div class="notice">{text}</div>'

    # ------------------------------------------------------------- detail
    def item(self, item_id: str) -> tuple[int, str] | None:
        try:
            conn = self.connect()
        except sqlite3.Error as exc:
            return HTTPStatus.INTERNAL_SERVER_ERROR, self._unavailable(str(exc))
        try:
            row = conn.execute(
                "SELECT payload_json FROM publications WHERE item_id=?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            payload = json.loads(row["payload_json"])
            policy = payload.get("policy") or "quarantine"
            audit_rows = conn.execute(
                "SELECT at, actor, action, detail FROM audit_log WHERE item_id=? "
                "ORDER BY audit_id DESC LIMIT 40",
                (item_id,),
            ).fetchall()
        finally:
            conn.close()

        label, css = STATE_LABELS.get(payload.get("public_state", "ok"), ("未知", "muted"))
        body = [
            f'<p class="sub"><a href="/">← 返回目录</a></p>',
            f"<h1>{_e(payload['title'])}</h1>",
            '<div class="badge-row">'
            f'<span class="chip {css}">{_e(label)}</span>'
            f'<span class="chip">策略 {_e(POLICY_LABELS.get(policy, policy))}</span>'
            f'<span class="chip muted">原文语言 {_e(payload.get("original_language") or "?")}</span>'
            f'<span class="chip muted">修订 {_e(payload.get("revision", "—"))}</span></div>',
            f'<p class="small muted">发布于 {fmt_ts(payload.get("generated_at"))} · '
            f'核对运行 #{_e(payload.get("verification_run_id", "—"))}</p>',
        ]

        if payload.get("public_state") == "blocked":
            body.append(
                '<div class="notice bad"><strong>整组隔离中。</strong>'
                "本组不提供任何语言版本作为对外可用资料；下方保留全部来源证据用于修正。</div>"
            )
        impact = payload.get("impact") or {}
        if impact:
            body.append(
                '<div class="card"><h2 style="margin-top:0">策略影响</h2><dl class="kv">'
                f'<dt>可用性</dt><dd>{_e(impact.get("availability", "—"))}</dd>'
                f'<dt>完整性</dt><dd>{_e(impact.get("completeness", "—"))}</dd>'
                f'<dt>修正</dt><dd>{_e(impact.get("action", "—"))}</dd></dl></div>'
            )

        findings = payload.get("findings") or []
        if findings:
            body.append("<h2>问题与证据</h2>")
            for finding in findings:
                sev = finding.get("severity", "info")
                body.append(
                    f'<div class="card"><div class="badge-row">'
                    f'<span class="chip {_e(sev)}">{_e(SEVERITY_LABELS.get(sev, sev))}</span>'
                    f'<code>{_e(finding.get("code"))}</code>'
                    + (
                        f'<span class="chip muted">{_e(finding["doc_id"])}</span>'
                        if finding.get("doc_id")
                        else ""
                    )
                    + "</div>"
                    f'<p>{_e(finding.get("message"))}</p>'
                    "<details><summary>查看证据</summary>"
                    f"<pre>{_e(json.dumps(finding.get('evidence'), ensure_ascii=False, indent=2, default=str))}</pre>"
                    "</details></div>"
                )
        else:
            body.append(
                '<div class="notice"><strong>未发现关系冲突或提示。</strong>'
                "原文与译文对应均通过核对。</div>"
            )

        body.append("<h2>语言对应</h2>")
        if payload.get("public_state") == "blocked":
            body.append(
                '<p class="sub">隔离期间不声明任何可用语言版本，以下内容仅用于核对。</p>'
            )
        body.append(
            '<p class="sub">原文行决定资料的语言基准与版本号。译文行分别标注'
            "所依据的原文版本；译文更新只改变译文行，不会把原文标记为新版本。</p>"
        )
        languages = payload.get("languages") or []
        if not languages:
            body.append('<p class="muted">没有可用的语言分组。</p>')
        for language in languages:
            body.append(
                f'<h3>{_e(language["language"])}'
                + (
                    ' <span class="chip muted">原文语言</span>'
                    if language.get("original")
                    else ""
                )
                + "</h3>"
            )
            rows = []
            if language.get("original"):
                rows.append(self._doc_row(language["original"], role="原文"))
            for translation in language.get("translations", []):
                rows.append(self._doc_row(translation, role="译文"))
            body.append(
                '<div class="card" style="padding:0;overflow:auto"><table><thead><tr>'
                "<th>角色</th><th>版本</th><th>标题</th><th>状态</th>"
                "<th>对应关系</th><th>来源 / 存档</th></tr></thead><tbody>"
                + "".join(rows)
                + "</tbody></table></div>"
            )

        body.append("<h2>替代与撤回链</h2>")
        lineage = [doc for doc in payload.get("documents", []) if doc.get("replacements")]
        if not lineage:
            body.append('<p class="muted">本组没有声明替代关系。</p>')
        for doc in lineage:
            body.append(
                f'<div class="card"><h3 style="margin-top:0">'
                f'{_e(doc["language"])} · {_e(doc["version"])} · {_e(doc["title"])}</h3>'
                f'<p class="small muted">当前状态：{_e(STATE_TEXT.get(doc["state"], doc["state"]))}'
                f' · 文档 <code>{_e(doc["doc_id"])}</code></p><ul class="plain">'
            )
            for view in doc["replacements"]:
                direction = "替代" if view["kind"] == "replaces" else "被替代为"
                state = view.get("relation_state")
                css2 = "ok" if state in ("verified",) else (
                    "warning" if state in ("pending", "stale") else "conflict"
                )
                target = view.get("target_doc_id") or view.get("target_url")
                link = (
                    f'<a href="/items/{_e(payload["item_id"])}#{_e(view["target_doc_id"])}">'
                    if view.get("target_doc_id")
                    else ""
                )
                end = "</a>" if view.get("target_doc_id") else ""
                reasons = ", ".join(view.get("reasons") or [])
                body.append(
                    f'<li><span class="chip {css2}">{_e(state)}</span> '
                    f"{_e(direction)} {link}<code>{_e(target)}</code>{end}"
                    + (f' <span class="small muted">（{_e(reasons)}）</span>' if reasons else "")
                    + "</li>"
                )
            body.append("</ul></div>")

        withdrawn = [
            doc for doc in payload.get("documents", []) if doc.get("withdrawal")
        ]
        body.append("<h2>撤回依据</h2>")
        if not withdrawn:
            body.append('<p class="muted">本组没有已撤回的版本。</p>')
        for doc in withdrawn:
            basis = doc["withdrawal"]
            body.append(
                f'<div class="card"><div class="badge-row">'
                f'<span class="chip bad">已撤回</span>'
                f'<span class="chip muted">{_e(doc["language"])} · 译文 v{_e(doc["version"])}</span>'
                "</div>"
                f'<p><strong>撤回理由：</strong>{_e(basis.get("reason") or "来源未给出理由")}</p>'
                '<dl class="kv">'
                f'<dt>撤回日期</dt><dd>{_e(basis.get("withdrawn_at") or "—")}</dd>'
                f'<dt>依据快照</dt><dd>#{_e(basis.get("basis_snapshot_id"))}</dd>'
                f'<dt>抓取时间</dt><dd>{fmt_ts(basis.get("fetched_at"))}</dd>'
                f'<dt>HTTP 状态</dt><dd>{_e(basis.get("http_status"))}</dd>'
                f'<dt>内容 SHA-256</dt><dd class="mono">{_e(basis.get("sha256"))}</dd>'
                f'<dt>来源地址</dt><dd><a href="{_e(basis.get("source_url"))}" rel="noopener noreferrer">'
                f'{_e(basis.get("source_url"))}</a></dd>'
                f'<dt>存档</dt><dd><a href="{_e(basis.get("raw_url"))}">查看抓取原文</a>'
                f'{" · 已归档" if basis.get("archived") else " · 未归档"}</dd></dl></div>'
            )

        body.append("<h2>全部来源快照</h2>")
        body.append(
            '<p class="sub">目录保留每次观察到的原始响应。来源链接指向外部站点，'
            "存档链接指向本目录保存的抓取内容；两者都可用于复核。</p>"
        )
        body.append(
            '<div class="card" style="padding:0;overflow:auto"><table><thead><tr>'
            "<th>语言</th><th>角色</th><th>版本</th><th>状态</th><th>快照</th>"
            "<th>抓取时间</th><th>SHA-256</th></tr></thead><tbody>"
        )
        for doc in payload.get("documents", []):
            body.append(
                f'<tr id="{_e(doc["doc_id"])}"><td>{_e(doc["language"])}</td>'
                f'<td>{_e("原文" if doc["role"] == "original" else "译文")}</td>'
                f'<td>v{_e(doc["version"])}</td>'
                f'<td>{_e(STATE_TEXT.get(doc["state"], doc["state"]))}</td>'
                f'<td><a href="/source/{_e(doc["doc_id"])}/{_e(doc["snapshot_id"])}">'
                f'#{_e(doc["snapshot_id"])}</a> · '
                f'<a href="{_e(doc["url"])}" rel="noopener noreferrer">来源</a> · '
                f'<a href="{_e(doc["raw_url"])}">下载</a></td>'
                f"<td>{fmt_ts(doc.get('fetched_at'))}</td>"
                f'<td class="mono">{fmt_short(doc.get("sha256"), 20)}</td></tr>'
            )
        body.append("</tbody></table></div>")

        body.append("<h2>最近审计记录</h2>")
        body.append('<div class="card"><ul class="plain">')
        for entry in audit_rows:
            detail = entry["detail"]
            try:
                detail_obj = json.loads(detail)
            except (TypeError, json.JSONDecodeError):
                detail_obj = detail
            body.append(
                f'<li><span class="small muted">{fmt_ts(entry["at"])}</span> '
                f'<span class="chip muted">{_e(entry["actor"])}</span> '
                f'<code>{_e(entry["action"])}</code> '
                f'<span class="small muted">{_e(json.dumps(detail_obj, ensure_ascii=False, default=str))}</span></li>'
            )
        if not audit_rows:
            body.append('<li class="muted">暂无记录。</li>')
        body.append("</ul></div>")
        return HTTPStatus.OK, page(payload["title"], "".join(body), policy=policy)

    def _doc_row(self, doc: dict[str, Any], *, role: str) -> str:
        state = doc.get("state")
        state_chip = f'<span class="chip {"muted" if state != "active" else ""}">' \
            f'{_e(STATE_TEXT.get(state, state))}</span>'
        relation = ""
        translation = doc.get("translation")
        if translation:
            target = translation.get("target_language") or "?"
            target_version = translation.get("target_version") or "?"
            declared = translation.get("declared_source_version")
            relation_state = translation.get("relation_state")
            relation_css = "ok" if relation_state == "verified" else (
                "warning" if relation_state in ("pending", "stale", None) else "conflict"
            )
            relation = (
                f'→ {_e(target)} 原文 v{_e(target_version)}'
                f'<br><span class="chip {relation_css}">{_e(relation_state or "pending")}</span>'
                f'<span class="small muted">依据原文 v{_e(declared if declared is not None else "?")}</span>'
            )
            if doc["state"] != "active":
                relation += (
                    '<br><span class="small muted">已撤回/下架版本；对应关系仅作历史记录，'
                    "不参与当前语言对应</span>"
                )
        elif role == "原文":
            relation = '<span class="small muted">语言基准</span>'
        if doc["state"] != "active":
            state_chip = f'<span class="chip muted">{_e(STATE_TEXT.get(state, state))}</span>'
        source = (
            f'<a href="{_e(doc.get("url"))}" rel="noopener noreferrer">来源</a> · '
            f'<a href="/source/{_e(doc["doc_id"])}/{_e(doc["snapshot_id"])}">抓取原文</a>'
        )
        return (
            f"<tr><td>{_e(role)}</td><td>v{_e(doc['version'])}</td>"
            f"<td>{_e(doc['title'])}</td><td>{state_chip}</td>"
            f"<td>{relation}</td><td class='small'>{source}<br>"
            f'<span class="mono muted">{fmt_short(doc.get("sha256"), 16)}</span></td></tr>'
        )

    # --------------------------------------------------------------- raw
    def _owned_raw(self, doc_id: str, snapshot_id: str) -> tuple[bytes | None, str | None, sqlite3.Row | None]:
        """Resolve an archived file, refusing anything outside the data root."""
        if not snapshot_id.isdigit():
            return None, "快照编号非法。", None
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT s.*, p.item_id AS published_item "
                "FROM snapshots s JOIN publications p ON p.item_id = s.item_id "
                "WHERE s.snapshot_id=? AND s.doc_id=?",
                (int(snapshot_id), doc_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None, None, None
        try:
            resolved = Path(row["raw_path"]).resolve()
            if not resolved.is_relative_to(self.data_root):
                return None, "存档路径不在数据目录内，已拒绝读取。", row
            if not resolved.is_file():
                return None, "存档文件不存在。", row
            return resolved.read_bytes(), None, row
        except OSError as exc:
            return None, f"读取存档失败：{exc}", row

    def source_view(self, doc_id: str, snapshot_id: str) -> tuple[int, str] | None:
        data, error, row = self._owned_raw(doc_id, snapshot_id)
        if row is None:
            return None
        content = data.decode("utf-8", errors="replace") if data is not None else None
        body = [
            f'<p class="sub"><a href="/items/{_e(row["item_id"])}">← 返回资料</a></p>',
            f"<h1>抓取原文 · {_e(row['title'])}</h1>",
            '<div class="card"><dl class="kv">'
            f'<dt>快照</dt><dd>#{_e(row["snapshot_id"])} · 观察序号 {_e(row["observed_seq"])}</dd>'
            f'<dt>文档</dt><dd class="mono">{_e(row["doc_id"])}</dd>'
            f'<dt>语言 / 角色</dt><dd>{_e(row["language"])} / {_e(row["role"])}</dd>'
            f'<dt>版本 / 状态</dt><dd>v{_e(row["version"])} / {_e(row["status"])}</dd>'
            f'<dt>来源地址</dt><dd><a href="{_e(row["url"])}" rel="noopener noreferrer">'
            f'{_e(row["url"])}</a></dd>'
            f'<dt>抓取时间</dt><dd>{fmt_ts(row["fetched_at"])}</dd>'
            f'<dt>HTTP</dt><dd>{_e(row["http_status"])}</dd>'
            f'<dt>ETag</dt><dd class="mono">{_e(row["etag"] or "—")}</dd>'
            f'<dt>Last-Modified</dt><dd class="mono">{_e(row["last_modified"] or "—")}</dd>'
            f'<dt>SHA-256</dt><dd class="mono">{_e(row["body_sha256"])}</dd>'
            f'<dt>下载</dt><dd><a href="/raw/{_e(row["doc_id"])}/{_e(row["snapshot_id"])}">'
            "原始字节（text/plain）</a></dd></dl></div>",
        ]
        if error:
            body.append(f'<div class="notice bad">{_e(error)}</div>')
        else:
            body.append(
                '<h2>原始响应内容</h2><p class="sub">以下是来源返回的 HTML 原文，'
                "已按文本转义显示，不会执行其中的脚本。</p>"
                f"<pre>{_e(content or '')}</pre>"
            )
        return HTTPStatus.OK, page(f"抓取原文 · {row['title']}", "".join(body))

    def raw_download(self, doc_id: str, snapshot_id: str) -> tuple[int, bytes, dict[str, str]] | None:
        data, _error, row = self._owned_raw(doc_id, snapshot_id)
        if row is None:
            return None
        if data is None:
            return None
        headers = {
            "Content-Disposition": f'attachment; filename="{row["doc_id"]}-{row["snapshot_id"]}.html"',
            "X-Content-Type-Options": "nosniff",
            "X-Snapshot-SHA256": row["body_sha256"],
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        return HTTPStatus.OK, data, headers

    # -------------------------------------------------------------- status
    def status(self) -> str:
        try:
            conn = self.connect()
        except sqlite3.Error as exc:
            return self._unavailable(str(exc))
        try:
            policy = db.get_setting(conn, "conflict_policy") or "quarantine"
            runs = conn.execute(
                "SELECT * FROM verification_runs ORDER BY run_id DESC LIMIT 8"
            ).fetchall()
            collect = conn.execute(
                "SELECT * FROM collect_runs ORDER BY run_id DESC LIMIT 8"
            ).fetchall()
            relation_runs = conn.execute(
                "SELECT * FROM relation_runs ORDER BY run_id DESC LIMIT 8"
            ).fetchall()
            jobs = conn.execute(
                "SELECT status, COUNT(*) AS n FROM relation_jobs GROUP BY status"
            ).fetchall()
            dirty = conn.execute(
                "SELECT i.item_id, i.title, COALESCE(st.dirty_seq,0) AS d, "
                "COALESCE(st.published_seq,0) AS p, st.public_state FROM items i "
                "LEFT JOIN item_state st ON st.item_id=i.item_id "
                "WHERE COALESCE(st.dirty_seq,0) > COALESCE(st.published_seq,0) "
                "ORDER BY i.item_id"
            ).fetchall()
            counts = {
                "items": conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"],
                "documents": conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"],
                "snapshots": conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"],
            }
            problems = conn.execute(
                "SELECT at, item_id, doc_id, url, code, message, "
                "'collect_issue' AS source FROM collect_issues "
                "UNION ALL "
                "SELECT attempted_at, item_id, doc_id, url, code, message, "
                "'fetch_error' AS source FROM fetch_errors "
                "ORDER BY at DESC LIMIT 15"
            ).fetchall()
            stuck = conn.execute(
                "SELECT * FROM relation_jobs WHERE status IN ('queued','exhausted') "
                "ORDER BY job_id DESC LIMIT 15"
            ).fetchall()
        finally:
            conn.close()
        body = [
            "<h1>运行状态</h1>",
            f'<div class="notice"><strong>当前冲突策略：'
            f"{_e(POLICY_LABELS.get(policy, policy))}</strong></div>",
            '<div class="card"><dl class="kv">'
            f'<dt>资料</dt><dd>{counts["items"]}</dd>'
            f'<dt>文档</dt><dd>{counts["documents"]}</dd>'
            f'<dt>快照</dt><dd>{counts["snapshots"]}</dd>'
            + "".join(
                f'<dt>关系任务 {_e(row["status"])}</dt><dd>{row["n"]}</dd>'
                for row in jobs
            )
            + "</dl></div>",
            "<h2>等待推进的资料</h2>",
        ]
        if dirty:
            body.append(
                '<p class="sub">采集或核对产生的新观察只把这些资料标记为待发布，'
                "发布会跳过其他未变化的资料。</p>"
                '<div class="card"><ul class="plain">'
            )
            for row in dirty:
                body.append(
                    f'<li><a href="/items/{_e(row["item_id"])}">{_e(row["title"])}</a> '
                    f'<span class="chip muted">dirty {row["d"]} &gt; published {row["p"]}</span></li>'
                )
            body.append("</ul></div>")
        else:
            body.append('<p class="muted">没有待推进资料。</p>')

        def table(title: str, rows, cols: list[str]) -> str:
            out = [f"<h2>{_e(title)}</h2>"]
            if not rows:
                return out[0] + '<p class="muted">暂无记录。</p>'
            out.append('<div class="card" style="padding:0;overflow:auto"><table><thead><tr>')
            out.append("".join(f"<th>{_e(c)}</th>" for c in cols))
            out.append("</tr></thead><tbody>")
            for row in rows:
                out.append("<tr>")
                for col in cols:
                    value = row[col.lower().replace(" ", "_")] if col.lower().replace(" ", "_") in row.keys() else ""
                    out.append(f"<td class='small'>{_e(value if value is not None else '—')}</td>")
                out.append("</tr>")
            out.append("</tbody></table></div>")
            return "".join(out)

        body.append(table("最近发布核对", runs, ["run_id", "started_at", "status", "scope", "policy", "affected_items"]))
        body.append(table("最近采集", collect, ["run_id", "started_at", "status", "seen_items", "seen_docs", "new_snapshots"]))
        body.append(table("最近关系核对", relation_runs, ["run_id", "started_at", "status", "applied", "blocked"]))

        body.append("<h2>采集问题</h2>")
        if problems:
            body.append(
                '<div class="card" style="padding:0;overflow:auto"><table><thead><tr>'
                "<th>时间</th><th>代码</th><th>资料</th><th>地址</th><th>说明</th>"
                "</tr></thead><tbody>"
            )
            for row in problems:
                body.append(
                    f"<tr><td class='small'>{fmt_ts(row['at'])}</td>"
                    f"<td><code>{_e(row['code'])}</code></td>"
                    f"<td class='small'>{_e(row['item_id'] or '—')}</td>"
                    f"<td class='small mono'>{_e(row['url'] or '—')}</td>"
                    f"<td class='small'>{_e(row['message'])}</td></tr>"
                )
            body.append("</tbody></table></div>")
            body.append(
                '<p class="sub">采集失败的语言版本不会被标记为下架，也不会被当作可用；'
                "它们保持上一次已核对的状态并在此列出。</p>"
            )
        else:
            body.append('<p class="muted">最近没有采集失败。</p>')

        body.append("<h2>未完成的关系任务</h2>")
        if stuck:
            body.append(
                '<div class="card" style="padding:0;overflow:auto"><table><thead><tr>'
                "<th>任务</th><th>状态</th><th>尝试</th><th>类型</th><th>来源文档</th>"
                "<th>目标</th><th>说明</th></tr></thead><tbody>"
            )
            for row in stuck:
                css = "conflict" if row["status"] == "exhausted" else "warning"
                body.append(
                    f"<tr><td>#{_e(row['job_id'])}</td>"
                    f'<td><span class="chip {css}">{_e(row["status"])}</span></td>'
                    f"<td>{_e(row['attempts'])}</td>"
                    f"<td class='small'>{_e(row['kind'])}</td>"
                    f"<td class='small mono'>{_e(row['source_doc'])}</td>"
                    f"<td class='small mono'>{_e(row['target_url'])}</td>"
                    f"<td class='small'>{_e(row['reason'] or '—')}</td></tr>"
                )
            body.append("</tbody></table></div>")
            body.append(
                '<p class="sub">重试耗尽的任务不会自行恢复。修正来源或确认目标可用后，'
                "需要重新采集以排队新的核对任务。</p>"
            )
        else:
            body.append('<p class="muted">没有排队或耗尽的关系任务。</p>')
        return page("运行状态", "".join(body), policy=policy)


class Handler(BaseHTTPRequestHandler):
    catalog: Catalog
    server_version = "CorpusCatalog/1.0"

    def log_message(self, fmt, *args):
        pass

    def _respond(self, status: int, body: str | bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = normalize_path(urlsplit(self.path).path)
        try:
            self._route(path)
        except sqlite3.Error as exc:
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, page("数据库错误", f"<h1>数据库错误</h1><pre>{_e(exc)}</pre>"), "text/html; charset=utf-8")
        except Exception as exc:  # pragma: no cover - defensive
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, page("内部错误", f"<h1>内部错误</h1><pre>{_e(exc)}</pre>"), "text/html; charset=utf-8")

    def _route(self, path: str) -> None:
        if path in ("/", "/index.html"):
            self._respond(200, self.catalog.index(), "text/html; charset=utf-8")
            return
        if path == "/status":
            self._respond(200, self.catalog.status(), "text/html; charset=utf-8")
            return
        if path == "/healthz":
            self._respond(200, b"ok", "text/plain; charset=utf-8")
            return
        parts = [unquote(p) for p in path.split("/") if p]
        if len(parts) == 2 and parts[0] == "items":
            result = self.catalog.item(parts[1])
            if result is None:
                self._respond(404, page("未找到", "<h1>未找到该资料</h1>"), "text/html; charset=utf-8")
            else:
                status, body = result
                self._respond(status, body, "text/html; charset=utf-8")
            return
        if len(parts) == 3 and parts[0] == "source":
            result = self.catalog.source_view(parts[1], parts[2])
            if result is None:
                self._respond(404, page("未找到", "<h1>未找到该快照</h1>"), "text/html; charset=utf-8")
            else:
                status, body = result
                self._respond(status, body, "text/html; charset=utf-8")
            return
        if len(parts) == 3 and parts[0] == "raw":
            result = self.catalog.raw_download(parts[1], parts[2])
            if result is None:
                self._respond(404, "not found", "text/plain; charset=utf-8")
            else:
                status, data, headers = result
                # Raw archived pages are served as inert text so a planted
                # script in a captured response can never execute here.
                self._respond(
                    status,
                    data,
                    "text/plain; charset=utf-8",
                    headers,
                )
            return
        self._respond(404, page("未找到", "<h1>未找到</h1><p><a href='/'>返回目录</a></p>"), "text/html; charset=utf-8")


def make_server(db_path: str | Path, data_root: str | Path, port: int = 0) -> ThreadingHTTPServer:
    catalog = Catalog(db_path, data_root)
    handler = type("BoundCatalogHandler", (Handler,), {"catalog": catalog})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.catalog = catalog  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Serve the read-only catalog")
    parser.add_argument("--db", default="data/catalog.db")
    parser.add_argument("--data", default="data")
    parser.add_argument("--port", type=int, default=8200)
    args = parser.parse_args()
    server = make_server(args.db, args.data, args.port)
    print(f"catalog listening on http://127.0.0.1:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
