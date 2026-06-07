"""FastAPI app: read-only ontology browser + proposal accept/reject/rollback.

Single-file: routes + templates + CSS. HTML is built from Python
f-strings (html.escape on every interpolation) — Jinja2 would be
overkill for the eight pages this app has, and adding a templating
dep would push past the "minimal optional extra" sweet spot.

The app talks to MongoDB through the same repository layer as the CLI
and `mahalath.proposals` module. No business logic lives here; the
view code only marshals data into HTML and forwards form submissions
to `accept_proposal` / `reject_proposal` / `rollback_proposal`.

Security posture: bind to 127.0.0.1 by default. There is no auth;
operator runs it locally, browser on the same host.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from mahalath.config import AppConfig, load_config
from mahalath.db import ensure_indexes, get_database
from mahalath.db.repositories import (
    OntologyEntryRepository,
    OntologyTreeRepository,
    UndecidedQueueRepository,
)
from mahalath.proposals import (
    ProposalError,
    accept_proposal,
    get_proposal,
    list_proposals,
    reject_proposal,
    rollback_proposal,
)


# --- HTML helpers ---------------------------------------------------------


_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
       max-width: 1100px; margin: 1.5em auto; padding: 0 1em; line-height: 1.5;
       color: #222; background: #fafafa; }
header nav { margin-bottom: 1.5em; padding-bottom: 0.7em; border-bottom: 1px solid #ddd; }
header nav a { margin-right: 1em; color: #0a58ca; text-decoration: none; font-weight: 500; }
header nav a:hover { text-decoration: underline; }
h1, h2 { border-bottom: 1px solid #ddd; padding-bottom: 0.3em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; background: white; }
th, td { text-align: left; padding: 0.55em 0.7em; border-bottom: 1px solid #eee; vertical-align: top; }
th { background: #f0f1f4; font-weight: 600; }
tr:hover td { background: #fafafa; }
code { background: #eef0f3; padding: 0.1em 0.4em; border-radius: 3px;
       font-family: ui-monospace, SF Mono, Menlo, monospace; font-size: 0.92em; }
.badge { display: inline-block; padding: 0.1em 0.55em; border-radius: 10px; font-size: 0.8em;
         font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; }
.badge.applied { background: #d4edda; color: #155724; }
.badge.pending_review { background: #fff3cd; color: #856404; }
.badge.rejected { background: #f8d7da; color: #721c24; }
.badge.invalid { background: #d1ecf1; color: #0c5460; }
.badge.rolled_back { background: #e2e3e5; color: #383d41; }
form.inline { display: inline-block; margin: 0.3em 0.3em 0.3em 0; }
form.inline input[type=text] { padding: 0.3em 0.5em; border: 1px solid #bbb; border-radius: 3px;
                                width: 22em; }
button { padding: 0.4em 1em; cursor: pointer; border: 1px solid #888; background: white;
         border-radius: 3px; }
button.accept { border-color: #28a745; color: #155724; }
button.reject { border-color: #dc3545; color: #721c24; }
button.rollback { border-color: #6c757d; color: #444; }
.definition { font-style: italic; background: #fff; border-left: 4px solid #0a58ca;
              padding: 0.7em 1em; margin: 0.6em 0; border-radius: 0 4px 4px 0; }
.attribution { font-size: 0.85em; color: #555; margin-top: 0.2em; }
.reason { font-size: 0.95em; background: #fff; padding: 0.5em 0.8em; border-left: 3px solid #ccc;
          margin: 0.4em 0; }
.muted { color: #888; }
.kvbox th { width: 12em; }
.summary { display: flex; gap: 1em; flex-wrap: wrap; }
.summary .card { background: white; border: 1px solid #ddd; border-radius: 6px;
                  padding: 1em 1.3em; min-width: 7em; }
.summary .card .n { font-size: 2em; font-weight: 700; line-height: 1; }
.summary .card .label { font-size: 0.85em; color: #555; margin-top: 0.2em; }
"""


def _base(title: str, body: str, database: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — Mahalath</title>
<style>{_CSS}</style>
</head>
<body>
<header>
  <nav>
    <a href="/">Dashboard</a>
    <a href="/ontology">Ontology</a>
    <a href="/proposals?status=pending_review">Pending</a>
    <a href="/proposals">All proposals</a>
    <a href="/undecided">Undecided</a>
    <a href="/documents">Documents</a>
    <span class="muted" style="float:right">{escape(database)}</span>
  </nav>
</header>
{body}
</body>
</html>"""


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        return escape(value.isoformat(timespec="seconds"))
    if value is None:
        return ""
    return escape(str(value))


# --- App factory ----------------------------------------------------------


def create_app(config: AppConfig | None = None) -> FastAPI:
    resolved_config = config or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = get_database(resolved_config)
        ensure_indexes(db)
        yield
        # No close_all() here: the connection pool is shared with any
        # other in-process Mahalath code (tests, embedded usage), and
        # closing it on app shutdown would yank connections out from
        # under them. Long-running serve processes are reaped by the OS.

    app = FastAPI(
        title="Mahalath",
        description="Local ontology browser and operator dashboard",
        lifespan=lifespan,
    )
    app.state.config = resolved_config

    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> str:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        counts = {
            "entries": db.ontology_entries.count_documents({}),
            "pending": db.action_proposals.count_documents({"status": "pending_review"}),
            "applied": db.action_proposals.count_documents({"status": "applied"}),
            "undecided": db.undecided_queue.count_documents({}),
            "documents": db.documents.count_documents({}),
            "reviews": db.ontology_reviews.count_documents({}),
        }
        cards = "".join(
            f'<div class="card"><div class="n">{n}</div><div class="label">{escape(label)}</div></div>'
            for label, n in [
                ("ontology entries", counts["entries"]),
                ("pending proposals", counts["pending"]),
                ("applied proposals", counts["applied"]),
                ("undecided queue", counts["undecided"]),
                ("documents", counts["documents"]),
                ("hierarchy reviews", counts["reviews"]),
            ]
        )
        body = f"""
<h1>Mahalath</h1>
<p class="muted">Local ontology builder · MongoDB <code>{escape(config.mongo.database)}</code></p>
<div class="summary">{cards}</div>
<p style="margin-top:2em" class="muted">
  Operator workflow: drop Markdown into <code>input/</code>, then <code>mahalath run</code> picks
  it up automatically. Review pending proposals here. Glossary refreshes to
  <code>ontology/glossary.{{md,json}}</code> after every change.
</p>
"""
        return _base("Dashboard", body, config.mongo.database)

    @app.get("/ontology", response_class=HTMLResponse)
    def ontology_list(request: Request) -> str:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        entries_repo = OntologyEntryRepository(db)
        tree_repo = OntologyTreeRepository(db)

        rows: list[str] = []
        for label in sorted(entries_repo.all_labels()):
            entry = entries_repo.get(label)
            if entry is None:
                continue
            children = tree_repo.children_of(label)
            parent = (
                f'<a href="/ontology/{escape(entry.parent_label)}"><code>{escape(entry.parent_label)}</code></a>'
                if entry.parent_label else '<span class="muted">top-level</span>'
            )
            rows.append(f"""
<tr>
  <td><a href="/ontology/{escape(entry.mpl_label)}"><code>{escape(entry.mpl_label)}</code></a></td>
  <td>{escape(entry.canonical_term)}</td>
  <td>{entry.confidence:.1f}</td>
  <td>{parent}</td>
  <td>{len(children)}</td>
</tr>""")

        body = f"""
<h1>Ontology <span class="muted">({len(rows)})</span></h1>
<table>
<tr><th>MPL</th><th>Term</th><th>Conf</th><th>Parent</th><th>Children</th></tr>
{''.join(rows) or '<tr><td colspan="5" class="muted">(no entries yet)</td></tr>'}
</table>
"""
        return _base("Ontology", body, config.mongo.database)

    @app.get("/ontology/{mpl_label}", response_class=HTMLResponse)
    def ontology_detail(request: Request, mpl_label: str) -> str:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        entry = OntologyEntryRepository(db).get(mpl_label)
        if entry is None:
            raise HTTPException(404, f"entry not found: {mpl_label}")
        children = OntologyTreeRepository(db).children_of(mpl_label)

        defs_html = "".join(f"""
<div class="definition">{escape(d.text)}</div>
<p class="attribution">— from <code>{escape(d.model_used or "?")}</code> at {_iso(d.created_at)}</p>
""" for d in entry.definitions) or '<p class="muted">(no definitions recorded)</p>'

        aliases = ", ".join(f"<em>{escape(a)}</em>" for a in entry.aliases) or '<span class="muted">—</span>'
        children_html = ", ".join(
            f'<a href="/ontology/{escape(c)}"><code>{escape(c)}</code></a>' for c in children
        ) or '<span class="muted">—</span>'
        parent_html = (
            f'<a href="/ontology/{escape(entry.parent_label)}"><code>{escape(entry.parent_label)}</code></a>'
            if entry.parent_label else '<span class="muted">top-level</span>'
        )

        body = f"""
<h1>{escape(entry.mpl_label)} — {escape(entry.canonical_term)}</h1>
<table class="kvbox">
<tr><th>Confidence</th><td>{entry.confidence:.2f}</td></tr>
<tr><th>Status</th><td><span class="badge {escape(entry.status)}">{escape(entry.status)}</span></td></tr>
<tr><th>Parent</th><td>{parent_html}</td></tr>
<tr><th>Children</th><td>{children_html}</td></tr>
<tr><th>Aliases</th><td>{aliases}</td></tr>
<tr><th>Decision log</th><td><code>{escape(entry.decision_log_id or "")}</code></td></tr>
<tr><th>Source documents</th><td>{', '.join(f'<code>{escape(s)}</code>' for s in entry.source_document_ids) or '<span class="muted">—</span>'}</td></tr>
<tr><th>Updated</th><td>{_iso(entry.updated_at)}</td></tr>
</table>
<h2>Definitions</h2>
{defs_html}
"""
        return _base(f"{entry.mpl_label}", body, config.mongo.database)

    @app.get("/proposals", response_class=HTMLResponse)
    def proposals_list(request: Request, status: str | None = None) -> str:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        proposals = list_proposals(db, status=status)

        rows: list[str] = []
        for p in proposals:
            payload_str = ", ".join(f"{k}={v!r}" for k, v in p.payload.items())
            rows.append(f"""
<tr>
  <td><a href="/proposals/{escape(p.proposal_id)}"><code>{escape(p.proposal_id[:8])}…</code></a></td>
  <td><code>{escape(p.action_type)}</code></td>
  <td><code>{escape(payload_str)}</code></td>
  <td>{p.confidence:.1f}</td>
  <td><span class="badge {escape(p.status)}">{escape(p.status)}</span></td>
  <td>{_iso(p.created_at)}</td>
</tr>""")

        filter_chips = [
            ("all", "/proposals"),
            ("pending", "/proposals?status=pending_review"),
            ("applied", "/proposals?status=applied"),
            ("rejected", "/proposals?status=rejected"),
            ("invalid", "/proposals?status=invalid"),
            ("rolled_back", "/proposals?status=rolled_back"),
        ]
        chips_html = " · ".join(
            f'<a href="{href}">{label}</a>' for label, href in filter_chips
        )

        body = f"""
<h1>Proposals <span class="muted">({len(rows)})</span></h1>
<p>{chips_html}</p>
<table>
<tr><th>ID</th><th>Type</th><th>Payload</th><th>Conf</th><th>Status</th><th>Created</th></tr>
{''.join(rows) or '<tr><td colspan="6" class="muted">(no proposals)</td></tr>'}
</table>
"""
        return _base("Proposals", body, config.mongo.database)

    @app.get("/proposals/{proposal_id}", response_class=HTMLResponse)
    def proposal_detail(request: Request, proposal_id: str) -> str:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        try:
            p = get_proposal(proposal_id, db)
        except ProposalError as exc:
            raise HTTPException(404, str(exc))

        if p.status == "pending_review":
            actions = f"""
<h2>Decide</h2>
<form class="inline" method="post" action="/proposals/{escape(proposal_id)}/accept">
  <input type="text" name="note" placeholder="Optional operator note">
  <button type="submit" class="accept">Accept</button>
</form>
<form class="inline" method="post" action="/proposals/{escape(proposal_id)}/reject">
  <input type="text" name="note" placeholder="Optional operator note">
  <button type="submit" class="reject">Reject</button>
</form>
"""
        elif p.status == "applied":
            actions = f"""
<h2>Roll back</h2>
<form class="inline" method="post" action="/proposals/{escape(proposal_id)}/rollback">
  <input type="text" name="note" placeholder="Optional operator note">
  <button type="submit" class="rollback">Rollback</button>
</form>
"""
        else:
            actions = ""

        payload_rows = "".join(
            f"<tr><th>{escape(k)}</th><td><code>{escape(str(v))}</code></td></tr>"
            for k, v in p.payload.items()
        )
        decision_rows = ""
        if p.operator_decision:
            decision_rows += (
                f"<tr><th>Operator decision</th><td>{escape(p.operator_decision)} "
                f"at {_iso(p.operator_decision_at)}</td></tr>"
            )
        if p.operator_note:
            decision_rows += f"<tr><th>Operator note</th><td>{escape(p.operator_note)}</td></tr>"
        if p.rejection_reason:
            decision_rows += f"<tr><th>Rejection reason</th><td>{escape(p.rejection_reason)}</td></tr>"

        application_html = ""
        if p.application_result:
            application_html = "<h2>Application result</h2><table class='kvbox'>" + "".join(
                f"<tr><th>{escape(str(k))}</th><td><code>{escape(str(v))}</code></td></tr>"
                for k, v in p.application_result.items()
            ) + "</table>"

        body = f"""
<h1>Proposal <code>{escape(p.proposal_id)}</code></h1>
<table class="kvbox">
<tr><th>Type</th><td><code>{escape(p.action_type)}</code></td></tr>
<tr><th>Status</th><td><span class="badge {escape(p.status)}">{escape(p.status)}</span></td></tr>
<tr><th>Confidence</th><td>{p.confidence:.2f}</td></tr>
<tr><th>Proposed by</th><td><code>{escape(p.proposed_by or "")}</code></td></tr>
<tr><th>Created</th><td>{_iso(p.created_at)}</td></tr>
{payload_rows}
{decision_rows}
</table>
<h2>Reason</h2>
<p class="reason">{escape(p.reason)}</p>
{application_html}
{actions}
"""
        return _base(f"Proposal {p.proposal_id[:8]}", body, config.mongo.database)

    @app.post("/proposals/{proposal_id}/accept")
    def proposal_accept(
        request: Request, proposal_id: str, note: str = Form("")
    ) -> RedirectResponse:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        try:
            accept_proposal(proposal_id, db, note=note or None)
        except ProposalError as exc:
            raise HTTPException(400, str(exc))
        # Refresh glossary after applied state changes.
        from mahalath.glossary import refresh_glossary
        refresh_glossary(config, db)
        return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)

    @app.post("/proposals/{proposal_id}/reject")
    def proposal_reject(
        request: Request, proposal_id: str, note: str = Form("")
    ) -> RedirectResponse:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        try:
            reject_proposal(proposal_id, db, note=note or None)
        except ProposalError as exc:
            raise HTTPException(400, str(exc))
        return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)

    @app.post("/proposals/{proposal_id}/rollback")
    def proposal_rollback(
        request: Request, proposal_id: str, note: str = Form("")
    ) -> RedirectResponse:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        try:
            rollback_proposal(proposal_id, db, note=note or None)
        except ProposalError as exc:
            raise HTTPException(400, str(exc))
        from mahalath.glossary import refresh_glossary
        refresh_glossary(config, db)
        return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)

    @app.get("/undecided", response_class=HTMLResponse)
    def undecided_list(request: Request) -> str:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        items = UndecidedQueueRepository(db).list_pending(limit=200)
        rows = [f"""
<tr>
  <td>{escape(item.term)}</td>
  <td><code>{escape(item.reason)}</code></td>
  <td>{f"{item.last_confidence:.1f}" if item.last_confidence is not None else '<span class="muted">—</span>'}</td>
  <td>{item.escalation_level}</td>
  <td><code>{escape(item.decision_log_id[:8])}…</code></td>
  <td>{_iso(item.created_at)}</td>
</tr>""" for item in items]
        body = f"""
<h1>Undecided queue <span class="muted">({len(rows)})</span></h1>
<table>
<tr><th>Term</th><th>Reason</th><th>Last conf</th><th>Escalation</th><th>Decision log</th><th>Created</th></tr>
{''.join(rows) or '<tr><td colspan="6" class="muted">(queue is empty)</td></tr>'}
</table>
"""
        return _base("Undecided", body, config.mongo.database)

    @app.get("/documents", response_class=HTMLResponse)
    def documents_list(request: Request) -> str:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        docs = list(db.documents.find().sort("ingested_at", -1))
        rows = []
        for d in docs:
            doc_id = d.get("document_id", "")
            processed = "✓" if d.get("processed_at") else ""
            overlay = d.get("style_overlay_path") or ""
            rows.append(f"""
<tr>
  <td><code>{escape(doc_id[:8])}…</code></td>
  <td>{escape(d.get("title") or "—")}</td>
  <td>{d.get("byte_size", 0):,}</td>
  <td>{d.get("char_count", 0):,}</td>
  <td>{processed or '<span class="muted">—</span>'}</td>
  <td><code class="muted">{escape(overlay)}</code></td>
</tr>""")
        body = f"""
<h1>Documents <span class="muted">({len(rows)})</span></h1>
<table>
<tr><th>ID</th><th>Title</th><th>Bytes</th><th>Chars</th><th>Processed</th><th>Style overlay</th></tr>
{''.join(rows) or '<tr><td colspan="6" class="muted">(no documents)</td></tr>'}
</table>
"""
        return _base("Documents", body, config.mongo.database)
