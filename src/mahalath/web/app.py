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

from fastapi import Body, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from mahalath.config import AppConfig, load_config
from mahalath.db import ensure_indexes, get_database
from mahalath.db.repositories import (
    DefinitionContextRepository,
    OntologyEntryRepository,
    OntologyTreeRepository,
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
:root {
  --bg: #f6f7f9; --surface: #ffffff; --ink: #1c2128; --muted: #667085;
  --line: #e3e6ea; --line-soft: #edf0f3; --accent: #2f5fd0; --accent-soft: #e8eefc;
  --ok-bg: #d9f0e1; --ok-ink: #14572c; --warn-bg: #fdf1cf; --warn-ink: #755600;
  --bad-bg: #fadbde; --bad-ink: #82202b; --info-bg: #d8ecf3; --info-ink: #0c5460;
  --neutral-bg: #e6e8eb; --neutral-ink: #3d434b;
  --frame-bg: #e9e2f7; --frame-ink: #4b2e83;
  --shadow: 0 1px 2px rgba(16, 24, 40, 0.06), 0 1px 3px rgba(16, 24, 40, 0.08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12151b; --surface: #1a1e26; --ink: #e5e8ee; --muted: #97a0af;
    --line: #2a3039; --line-soft: #232833; --accent: #7c9cff; --accent-soft: #232c45;
    --ok-bg: #14361f; --ok-ink: #7edc9f; --warn-bg: #3a3212; --warn-ink: #ecd07a;
    --bad-bg: #43191e; --bad-ink: #ff9aa4; --info-bg: #123340; --info-ink: #8ed3ea;
    --neutral-bg: #262c36; --neutral-ink: #b6bec9;
    --frame-bg: #2c2444; --frame-ink: #c3b1ee;
    --shadow: none;
  }
}
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
       margin: 0; line-height: 1.55; color: var(--ink); background: var(--bg);
       font-size: 15px; }
main { max-width: 1100px; margin: 0 auto; padding: 1.2em 1.2em 3em; }
a { color: var(--accent); }
h1 { font-size: 1.5em; margin: 0.6em 0 0.5em; letter-spacing: -0.01em; }
h2 { font-size: 1.08em; margin: 1.6em 0 0.5em; text-transform: uppercase;
     letter-spacing: 0.05em; color: var(--muted); font-weight: 600; }
header { position: sticky; top: 0; z-index: 10; background: var(--surface);
         border-bottom: 1px solid var(--line); }
header nav { max-width: 1100px; margin: 0 auto; padding: 0.55em 1.2em;
             display: flex; align-items: center; gap: 0.25em; flex-wrap: wrap; }
.brand { font-weight: 700; margin-right: 0.8em; color: var(--ink); text-decoration: none;
         letter-spacing: -0.01em; }
header nav a.navlink { color: var(--muted); text-decoration: none; font-weight: 500;
             padding: 0.3em 0.7em; border-radius: 999px; font-size: 0.94em; }
header nav a.navlink:hover { color: var(--ink); background: var(--line-soft); }
header nav a.navlink.active { color: var(--accent); background: var(--accent-soft); }
.dbname { margin-left: auto; color: var(--muted); font-size: 0.85em;
          font-family: ui-monospace, "SF Mono", Menlo, monospace; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; background: var(--surface);
        border: 1px solid var(--line); border-radius: 10px; overflow: hidden;
        box-shadow: var(--shadow); }
th, td { text-align: left; padding: 0.6em 0.8em; border-bottom: 1px solid var(--line-soft);
         vertical-align: top; }
tr:last-child td { border-bottom: none; }
th { background: var(--line-soft); font-weight: 600; font-size: 0.85em;
     text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
tr:hover td { background: var(--line-soft); }
@media (max-width: 720px) { table { display: block; overflow-x: auto; } }
code { background: var(--line-soft); padding: 0.1em 0.4em; border-radius: 4px;
       font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 0.92em; }
.badge { display: inline-block; padding: 0.12em 0.6em; border-radius: 999px; font-size: 0.78em;
         font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em; }
.badge.applied { background: var(--ok-bg); color: var(--ok-ink); }
.badge.pending_review { background: var(--warn-bg); color: var(--warn-ink); }
.badge.rejected { background: var(--bad-bg); color: var(--bad-ink); }
.badge.invalid { background: var(--info-bg); color: var(--info-ink); }
.badge.rolled_back { background: var(--neutral-bg); color: var(--neutral-ink); }
.badge.frame { background: var(--frame-bg); color: var(--frame-ink); }
.badge.untagged { background: transparent; color: var(--muted); border: 1px dashed var(--muted); }
.badge.intent { background: var(--ok-bg); color: var(--ok-ink); margin-right: 0.3em; }
.badge.intentionality { background: var(--warn-bg); color: var(--warn-ink); }
.meter { display: inline-flex; align-items: center; gap: 0.5em; white-space: nowrap; }
.meter .track { width: 3.2em; height: 6px; border-radius: 999px; background: var(--line);
                overflow: hidden; display: inline-block; }
.meter .fill { display: block; height: 100%; border-radius: 999px; background: var(--accent); }
.meter .val { font-size: 0.88em; font-variant-numeric: tabular-nums; color: var(--muted); }
form.inline { display: inline-block; margin: 0.3em 0.3em 0.3em 0; }
form.inline input[type=text] { padding: 0.45em 0.65em; border: 1px solid var(--line);
                                border-radius: 8px; width: 22em; background: var(--surface);
                                color: var(--ink); }
form.inline input[type=text]:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
button { padding: 0.45em 1.1em; cursor: pointer; border: 1px solid var(--line);
         background: var(--surface); color: var(--ink); border-radius: 8px; font: inherit;
         font-size: 0.94em; font-weight: 500; }
button:hover { border-color: var(--muted); }
button.accept { border-color: var(--ok-ink); color: var(--ok-ink); background: var(--ok-bg); }
button.reject { border-color: var(--bad-ink); color: var(--bad-ink); background: var(--bad-bg); }
button.rollback { border-color: var(--muted); color: var(--muted); }
.chatform textarea { width: 100%; padding: 0.7em; font-size: 1em; border: 1px solid var(--line);
                     border-radius: 10px; box-sizing: border-box; background: var(--surface);
                     color: var(--ink); resize: vertical; }
.chatform textarea:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
.chatform button { margin-top: 0.6em; background: var(--accent); color: #fff;
                   border-color: var(--accent); }
.chatresult { margin-top: 1.5em; }
.chatanswer { background: var(--surface); padding: 1em 1.3em; border: 1px solid var(--line);
              border-radius: 10px; line-height: 1.55; box-shadow: var(--shadow); }
.chatmeta { font-size: 0.85em; color: var(--muted); margin-top: 0.5em; }
.definition { font-style: italic; background: var(--surface); border-left: 4px solid var(--accent);
              padding: 0.7em 1em; margin: 0.6em 0; border-radius: 0 8px 8px 0; }
.definition-detailed { font-style: normal; background: var(--surface); border-left: 4px solid var(--line);
              padding: 0.7em 1em; margin: 0.4em 0 0.6em; border-radius: 0 8px 8px 0;
              line-height: 1.55; white-space: pre-wrap; }
.attribution { font-size: 0.85em; color: var(--muted); margin-top: 0.2em; }
.reason { font-size: 0.95em; background: var(--surface); padding: 0.6em 0.9em;
          border-left: 3px solid var(--line); margin: 0.4em 0; border-radius: 0 8px 8px 0; }
.muted { color: var(--muted); }
.ctxgroup { margin: 0.6em 0 1.4em; }
.ctxhead { display: flex; align-items: baseline; gap: 0.6em; flex-wrap: wrap; margin: 0.2em 0; }
.ctxdesc { font-size: 0.85em; color: var(--muted); }
.intents { font-size: 0.85em; color: var(--muted); margin: 0.2em 0 0.6em; }
.polysemy { font-size: 0.9em; color: var(--frame-ink); background: var(--frame-bg);
            border-left: 3px solid var(--frame-ink); padding: 0.5em 0.8em; margin: 0.4em 0;
            border-radius: 0 8px 8px 0; }
.kvbox th { width: 12em; }
.summary { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
           gap: 0.8em; }
.summary .card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px;
                 padding: 1em 1.2em; box-shadow: var(--shadow); text-decoration: none;
                 color: inherit; display: block; }
a.card:hover { border-color: var(--accent); }
.summary .card .n { font-size: 1.9em; font-weight: 700; line-height: 1.15;
                    font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
.summary .card .label { font-size: 0.83em; color: var(--muted); margin-top: 0.15em; }
.chips { display: inline-flex; gap: 0.4em; flex-wrap: wrap; }
.chips a { text-decoration: none; padding: 0.2em 0.75em; border-radius: 999px;
           border: 1px solid var(--line); color: var(--muted); font-size: 0.9em;
           background: var(--surface); }
.chips a:hover { color: var(--ink); border-color: var(--muted); }
"""

_NAV_ITEMS = [
    ("Dashboard", "/"),
    ("Ontology", "/ontology"),
    ("Pending", "/proposals?status=pending_review"),
    ("All proposals", "/proposals"),
    ("Undecided", "/undecided"),
    ("Documents", "/documents"),
    ("Effectiveness", "/effectiveness"),
    ("Chat", "/chat"),
]


def _base(title: str, body: str, database: str, active: str = "") -> str:
    links = "".join(
        f'<a class="navlink{" active" if href == active else ""}" href="{escape(href)}">{escape(label)}</a>'
        for label, href in _NAV_ITEMS
    )
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
    <a class="brand" href="/">Mahalath</a>
    {links}
    <span class="dbname">{escape(database)}</span>
  </nav>
</header>
<main>
{body}
</main>
</body>
</html>"""


def _conf(value: float) -> str:
    """Confidence as a small inline meter with the numeric value."""
    width = max(0, min(100, round(value * 100)))
    return (
        f'<span class="meter"><span class="track">'
        f'<span class="fill" style="width:{width}%"></span></span>'
        f'<span class="val">{value:.1f}</span></span>'
    )


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
            f'<a class="card" href="{escape(href)}"><div class="n">{n}</div><div class="label">{escape(label)}</div></a>'
            for label, n, href in [
                ("ontology entries", counts["entries"], "/ontology"),
                ("pending proposals", counts["pending"], "/proposals?status=pending_review"),
                ("applied proposals", counts["applied"], "/proposals?status=applied"),
                ("undecided queue", counts["undecided"], "/undecided"),
                ("documents", counts["documents"], "/documents"),
                ("hierarchy reviews", counts["reviews"], "/effectiveness"),
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
        return _base("Dashboard", body, config.mongo.database, active="/")

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
            untagged = sum(1 for d in entry.definitions if not d.context_id)
            contexts_cell = (
                f'<span class="badge untagged">untagged {untagged}</span>'
                if untagged else '<span class="muted">—</span>'
            )
            rows.append(f"""
<tr>
  <td><a href="/ontology/{escape(entry.mpl_label)}"><code>{escape(entry.mpl_label)}</code></a></td>
  <td>{escape(entry.canonical_term)}</td>
  <td>{_conf(entry.confidence)}</td>
  <td>{parent}</td>
  <td>{len(children)}</td>
  <td>{contexts_cell}</td>
</tr>""")

        untagged_entries = sum(1 for r in rows if "badge untagged" in r)
        summary = (
            f' <span class="muted">· {untagged_entries} with untagged definitions</span>'
            if untagged_entries else ""
        )
        body = f"""
<h1>Ontology <span class="muted">({len(rows)})</span>{summary}</h1>
<table>
<tr><th>MPL</th><th>Term</th><th>Conf</th><th>Parent</th><th>Children</th><th>Contexts</th></tr>
{''.join(rows) or '<tr><td colspan="6" class="muted">(no entries yet)</td></tr>'}
</table>
"""
        return _base("Ontology", body, config.mongo.database, active="/ontology")

    @app.get("/ontology/{mpl_label}", response_class=HTMLResponse)
    def ontology_detail(request: Request, mpl_label: str) -> str:
        config: AppConfig = request.app.state.config
        db = get_database(config)
        entry = OntologyEntryRepository(db).get(mpl_label)
        if entry is None:
            raise HTTPException(404, f"entry not found: {mpl_label}")
        children = OntologyTreeRepository(db).children_of(mpl_label)

        # Group definitions by their context (frame) so polysemy is visible:
        # one section per frame, untagged definitions collected last.
        contexts_by_id = {
            c.context_id: c for c in DefinitionContextRepository(db).all()
        }
        groups: dict[str | None, list] = {}
        for d in entry.definitions:
            groups.setdefault(d.context_id, []).append(d)
        ordered_cids: list[str | None] = [c for c in groups if c is not None]
        if None in groups:
            ordered_cids.append(None)

        def _def_block(d: Any) -> str:
            # Intent annotation (I-C): deployment metadata badges.
            intent_html = ""
            badges = "".join(
                f'<span class="badge intent">{escape(contexts_by_id[t].name if t in contexts_by_id else t)}</span>'
                for t in d.intent_tags
            )
            if d.intentionality:
                badges += (
                    f'<span class="badge intentionality">'
                    f'intentionality: {escape(d.intentionality)}</span>'
                )
            if badges:
                intent_html = f'<p class="intents">deployed to: {badges}</p>' \
                    if d.intent_tags else f'<p class="intents">{badges}</p>'
            detailed_html = ""
            if getattr(d, "detailed_text", None):
                detailed_html = (
                    f'<div class="definition-detailed">'
                    f'{escape(d.detailed_text)}</div>'
                )
            return f"""
<div class="definition">{escape(d.text)}</div>
{detailed_html}
<p class="attribution">— from <code>{escape(d.model_used or "?")}</code> at {_iso(d.created_at)}</p>
{intent_html}"""

        sections = []
        for cid in ordered_cids:
            if cid is None:
                head = '<span class="badge untagged">untagged</span>'
                desc = '<span class="ctxdesc muted">no context frame assigned</span>'
            elif cid in contexts_by_id:
                ctx = contexts_by_id[cid]
                head = f'<span class="badge frame">{escape(ctx.name)}</span>'
                desc = f'<span class="ctxdesc">{escape(ctx.description)}</span>'
            else:
                head = f'<span class="badge frame">{escape(cid)}</span>'
                desc = '<span class="ctxdesc muted">(context record missing)</span>'
            bodies = "".join(_def_block(d) for d in groups[cid])
            sections.append(
                f'<div class="ctxgroup"><div class="ctxhead">{head}{desc}</div>{bodies}</div>'
            )
        defs_html = "".join(sections) or '<p class="muted">(no definitions recorded)</p>'

        distinct_frames = [c for c in groups if c is not None]
        polysemy_html = (
            '<p class="polysemy">This term holds definitions in '
            f'{len(distinct_frames)} co-equal frames — neither overrides the '
            'other; each speaks within its own context.</p>'
            if len(distinct_frames) > 1 else ""
        )

        aliases = ", ".join(f"<em>{escape(a)}</em>" for a in entry.aliases) or '<span class="muted">—</span>'
        children_html = ", ".join(
            f'<a href="/ontology/{escape(c)}"><code>{escape(c)}</code></a>' for c in children
        ) or '<span class="muted">—</span>'
        parent_html = (
            f'<a href="/ontology/{escape(entry.parent_label)}"><code>{escape(entry.parent_label)}</code></a>'
            if entry.parent_label else '<span class="muted">top-level</span>'
        )

        # ADR-034: the conversation behind every prose layer must be readable,
        # not just identified by an opaque id.
        from mahalath.transcripts import conversations_for_entry

        conversations = conversations_for_entry(db, entry.mpl_label)
        decision_log_html = (
            f'<a href="/decisions/{escape(entry.decision_log_id)}">'
            f'<code>{escape(entry.decision_log_id)}</code></a>'
            if entry.decision_log_id else '<span class="muted">—</span>'
        )
        if conversations:
            rows = "".join(
                f"""
<tr>
  <td><span class="badge">{escape(c.layer)}</span></td>
  <td>{'<span class="muted">—</span>' if c.definition_index is None
       else f"#{c.definition_index}"}</td>
  <td>{escape(c.outcome)}</td>
  <td>{_iso(c.created_at) if c.created_at else '<span class="muted">—</span>'}</td>
  <td>{'<span class="muted">no record (predates capture)</span>'
       if c.missing else
       f'<a href="/decisions/{escape(c.decision_log_id)}">show conversation</a>'}</td>
</tr>""" for c in conversations
            )
            conversations_html = f"""
<h2>How this term was arrived at</h2>
<table>
<tr><th>Layer</th><th>Definition</th><th>Outcome</th><th>When</th><th></th></tr>
{rows}
</table>"""
        else:
            conversations_html = (
                "<h2>How this term was arrived at</h2>"
                '<p class="muted">No conversation records — this entry predates '
                "conversation capture (ADR-034).</p>"
            )

        body = f"""
<h1>{escape(entry.mpl_label)} — {escape(entry.canonical_term)}</h1>
<table class="kvbox">
<tr><th>Confidence</th><td>{entry.confidence:.2f}</td></tr>
<tr><th>Status</th><td><span class="badge {escape(entry.status)}">{escape(entry.status)}</span></td></tr>
<tr><th>Parent</th><td>{parent_html}</td></tr>
<tr><th>Children</th><td>{children_html}</td></tr>
<tr><th>Aliases</th><td>{aliases}</td></tr>
<tr><th>Decision log</th><td>{decision_log_html}</td></tr>
<tr><th>Source documents</th><td>{', '.join(f'<code>{escape(s)}</code>' for s in entry.source_document_ids) or '<span class="muted">—</span>'}</td></tr>
<tr><th>Updated</th><td>{_iso(entry.updated_at)}</td></tr>
</table>
<h2>Definitions</h2>
{polysemy_html}
{defs_html}
{conversations_html}
"""
        return _base(f"{entry.mpl_label}", body, config.mongo.database, active="/ontology")

    @app.get("/decisions/{decision_log_id}", response_class=HTMLResponse)
    def decision_detail(request: Request, decision_log_id: str) -> str:
        """The full conversation behind one piece of prose (ADR-034)."""
        from mahalath.transcripts import load_conversation

        config: AppConfig = request.app.state.config
        db = get_database(config)
        conversation = load_conversation(db, decision_log_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="decision log not found")

        turns = "".join(
            f"""
<h3>iteration {t.iteration} · {escape(t.role)} · <code>{escape(t.model)}</code>
{f'· confidence {t.confidence}' if t.confidence is not None else ''}</h3>
<details><summary>prompt sent to the model</summary><pre>{escape(t.prompt)}</pre></details>
<pre>{escape(t.response)}</pre>""" for t in conversation.exchanges
        ) or "".join(
            f"<h3>{escape(str(m.get('role')))}</h3><pre>{escape(str(m.get('content') or ''))}</pre>"
            for m in conversation.messages
        ) or '<p class="muted">(record exists but carries no messages)</p>'

        body = f"""
<h1>Conversation <code>{escape(conversation.decision_log_id)}</code></h1>
<table class="kvbox">
<tr><th>Produced</th><td><span class="badge">{escape(conversation.layer)}</span></td></tr>
<tr><th>Term</th><td>{escape(conversation.term)}</td></tr>
<tr><th>Outcome</th><td>{escape(conversation.outcome)}</td></tr>
<tr><th>Confidence</th><td>{conversation.final_confidence
    if conversation.final_confidence is not None else '—'}</td></tr>
<tr><th>Source document</th><td><code>{escape(conversation.source_document_id or '—')}</code></td></tr>
<tr><th>Recorded</th><td>{_iso(conversation.created_at) if conversation.created_at else '—'}</td></tr>
</table>
{turns}
"""
        return _base(
            f"Conversation {conversation.decision_log_id[:8]}",
            body, config.mongo.database, active="/ontology",
        )

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
  <td>{_conf(p.confidence)}</td>
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
        chips_html = "".join(
            f'<a href="{href}">{label}</a>' for label, href in filter_chips
        )

        body = f"""
<h1>Proposals <span class="muted">({len(rows)})</span></h1>
<p class="chips">{chips_html}</p>
<table>
<tr><th>ID</th><th>Type</th><th>Payload</th><th>Conf</th><th>Status</th><th>Created</th></tr>
{''.join(rows) or '<tr><td colspan="6" class="muted">(no proposals)</td></tr>'}
</table>
"""
        active_nav = "/proposals?status=pending_review" if status == "pending_review" else "/proposals"
        return _base("Proposals", body, config.mongo.database, active=active_nav)

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
        return _base(f"Proposal {p.proposal_id[:8]}", body, config.mongo.database, active="/proposals")

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
        """Only terms the system has finished trying on (ADR-037)."""
        from mahalath.review_gate import load_review_queue

        config: AppConfig = request.app.state.config
        db = get_database(config)
        queue = load_review_queue(
            db, confidence_threshold=config.runtime.confidence_threshold
        )
        rows = [f"""
<tr>
  <td><a href="/decisions/{escape(item.decision_log_id)}">{escape(item.term)}</a></td>
  <td><code>{escape(item.reason)}</code></td>
  <td>{f"{item.last_confidence:.1f}" if item.last_confidence is not None else '<span class="muted">—</span>'}</td>
  <td>{item.escalation_level}</td>
  <td>{_iso(item.created_at)}</td>
  <td>
    <form method="post" action="/undecided/{escape(item.decision_log_id)}/accept" class="inline">
      <input type="text" name="definition" placeholder="override definition (optional)" size="30">
      <button type="submit">Accept</button>
    </form>
    <form method="post" action="/undecided/{escape(item.decision_log_id)}/reject" class="inline">
      <button type="submit">Reject</button>
    </form>
  </td>
</tr>""" for item in queue.awaiting]
        still = (
            f'<p class="muted">{queue.still_retrying} further item(s) are still '
            f'being re-debated overnight and are not shown: the system has not '
            f'finished with them yet. Terms surface here once they have been '
            f'attempted {queue.escalation_threshold}+ times and are still below '
            f'{queue.confidence_threshold:g}, or immediately when agents disagree '
            f'on whether a term holds one meaning or two.</p>'
            if queue.still_retrying else ""
        )
        body = f"""
<h1>Needs your review <span class="muted">({len(rows)} of {queue.total_pending} queued)</span></h1>
{still}
<table>
<tr><th>Term</th><th>Reason</th><th>Last conf</th><th>Attempts</th><th>Created</th><th>Decision</th></tr>
{''.join(rows) or '<tr><td colspan="6" class="muted">(nothing needs you right now)</td></tr>'}
</table>
"""
        return _base("Undecided", body, config.mongo.database, active="/undecided")

    @app.post("/undecided/{decision_log_id}/accept")
    def undecided_accept(
        request: Request, decision_log_id: str,
        definition: str = Form(""), note: str = Form(""),
    ) -> RedirectResponse:
        """Accept a stuck term on the operator's authority (ADR-037)."""
        from mahalath.review_gate import ReviewError, accept_undecided

        config: AppConfig = request.app.state.config
        db = get_database(config)
        try:
            accept_undecided(
                db, decision_log_id, config.runtime,
                definition=definition.strip() or None,
                note=note,
                decided_by="operator (web)",
            )
        except ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        from mahalath.glossary import refresh_glossary
        refresh_glossary(config, db)
        return RedirectResponse("/undecided", status_code=303)

    @app.post("/undecided/{decision_log_id}/reject")
    def undecided_reject(
        request: Request, decision_log_id: str, note: str = Form("")
    ) -> RedirectResponse:
        """Drop a stuck term; the audit trail keeps the decision (ADR-037)."""
        from mahalath.review_gate import ReviewError, reject_undecided

        config: AppConfig = request.app.state.config
        db = get_database(config)
        try:
            reject_undecided(
                db, decision_log_id, note=note, decided_by="operator (web)"
            )
        except ReviewError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/undecided", status_code=303)

    @app.get("/effectiveness", response_class=HTMLResponse)
    def effectiveness_page(request: Request) -> str:
        """Decision-effectiveness self-analysis (requirements §3.4)."""
        from mahalath.analysis import build_effectiveness_report

        config: AppConfig = request.app.state.config
        db = get_database(config)
        report = build_effectiveness_report(
            db, database_name=config.mongo.database
        )
        d, p, q, v, c = (
            report.debates, report.proposals, report.queue,
            report.reviews, report.coverage,
        )

        def pct(value: float | None) -> str:
            return f"{value * 100:.0f}%" if value is not None else "—"

        findings = "".join(
            f'<div class="reason">{escape(f)}</div>' for f in report.findings
        )
        cards = "".join(
            f'<div class="card"><div class="n">{escape(str(n))}</div>'
            f'<div class="label">{escape(label)}</div></div>'
            for label, n in [
                ("debate acceptance", pct(d.acceptance_rate)),
                ("operator-decided proposals", p.operator_decided),
                ("undecided queue", q.size),
                ("re-debate resolution", pct(report.redebates.resolution_rate)),
                ("stale entries", c.stale_entries),
            ]
        )

        outcome_rows = "".join(
            f"<tr><td><code>{escape(outcome)}</code></td><td>{n}</td>"
            f"<td>{d.avg_iterations_by_outcome.get(outcome, '—')}</td>"
            f"<td>{d.avg_confidence_by_outcome.get(outcome, '—')}</td></tr>"
            for outcome, n in sorted(d.by_outcome.items())
        ) or '<tr><td colspan="4" class="muted">(no debates yet)</td></tr>'

        band_rows = "".join(
            f"<tr><td>{band.lo:g}–{band.hi:g}</td><td>{band.decided}</td>"
            f"<td>{band.accepted}</td><td>{band.rejected}</td>"
            f"<td>{band.rolled_back}</td><td>{pct(band.acceptance_rate)}</td></tr>"
            for band in p.bands if band.decided
        ) or (
            '<tr><td colspan="6" class="muted">(no operator-decided '
            "proposals yet)</td></tr>"
        )

        review_summary = (
            f"{v.total} passes · {v.with_actions} with actions · "
            f"no-action rate {pct(v.no_action_rate)} · avg actions "
            f"{v.avg_actions if v.avg_actions is not None else '—'}"
            if v.total else "(no hierarchy reviews yet)"
        )

        body = f"""
<h1>Decision effectiveness</h1>
<p class="muted">Self-analysis over the audit trails (requirements §3.4).
Generated {_iso(report.generated_at)} · also appended nightly to
<code>logs/effectiveness.jsonl</code> by the REM job ·
<a href="/api/effectiveness">JSON</a></p>
<div class="summary">{cards}</div>
<h2>Findings</h2>
{findings}
<h2>Debate outcomes</h2>
<table>
<tr><th>Outcome</th><th>Count</th><th>Avg iterations</th><th>Avg confidence</th></tr>
{outcome_rows}
</table>
<h2>Calibration — operator verdicts by agent confidence</h2>
<p class="muted">Each band: how often the operator accepted proposals the
agents made at that confidence. Rising acceptance with confidence =
calibrated.</p>
<table>
<tr><th>Confidence band</th><th>Decided</th><th>Accepted</th><th>Rejected</th><th>Rolled back</th><th>Acceptance</th></tr>
{band_rows}
</table>
<h2>Hierarchy reviews</h2>
<p>{escape(review_summary)}</p>
<h2>Coverage</h2>
<p>{c.entries} entries (avg confidence
{c.avg_entry_confidence if c.avg_entry_confidence is not None else "—"}) ·
{c.definitions} definitions ({c.frame_tagged} frame-tagged,
{c.intent_annotated} intent-annotated) · {c.stale_entries} stale</p>
"""
        return _base("Effectiveness", body, config.mongo.database, active="/effectiveness")

    @app.get("/api/effectiveness")
    def effectiveness_api(request: Request) -> JSONResponse:
        """The same §3.4 report as machine-readable JSON."""
        from mahalath.analysis import (
            build_effectiveness_report,
            report_to_dict,
        )

        config: AppConfig = request.app.state.config
        db = get_database(config)
        report = build_effectiveness_report(
            db, database_name=config.mongo.database
        )
        return JSONResponse({"ok": True, "report": report_to_dict(report)})

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request) -> str:
        config: AppConfig = request.app.state.config
        body = """
<h1>Chat</h1>
<p class="muted">Ask about the ontology. Answers are grounded in the current entries, references, and audit state. The chat is read-only; to act on what surfaces here, use the proposals page.</p>
<form id="chat-form" class="chatform">
  <textarea name="question" rows="3" placeholder="What does the ontology say about substrate? How are MPL-001 and MPL-004 related?"></textarea>
  <button type="submit">Ask</button>
</form>
<div id="answer" class="chatresult"></div>
<script>
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, function(c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
  });
}
document.getElementById('chat-form').addEventListener('submit', async function(e) {
  e.preventDefault();
  const q = document.querySelector('textarea[name=question]').value;
  if (!q.trim()) return;
  document.getElementById('answer').innerHTML = '<p class="muted">thinking…</p>';
  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: q})
    });
    const data = await r.json();
    if (!r.ok) {
      document.getElementById('answer').innerHTML = '<p style="color:#a00">' + esc(data.detail || 'error') + '</p>';
      return;
    }
    const cited = (data.cited_labels || []).map(l => '<a href="/ontology/' + encodeURIComponent(l) + '"><code>' + esc(l) + '</code></a>').join(', ');
    const ctx = (data.context_labels || []).map(l => '<a href="/ontology/' + encodeURIComponent(l) + '"><code>' + esc(l) + '</code></a>').join(', ');
    let actionsHtml = '';
    if ((data.suggested_actions || []).length > 0) {
      actionsHtml = '<h2>Suggested actions</h2>';
      data.suggested_actions.forEach((a, i) => {
        const payloadDesc = Object.entries(a.payload || {}).map(([k, v]) => k + '=' + JSON.stringify(v)).join(', ');
        actionsHtml +=
          '<div class="chatanswer" style="border-left: 4px solid #f0ad4e">' +
          '<strong>' + esc(a.type) + '</strong> (' + a.confidence.toFixed(1) + ' conf): <code>' + esc(payloadDesc) + '</code>' +
          '<p class="chatmeta">' + esc(a.reasoning) + '</p>' +
          '<button class="accept" data-idx="' + i + '">Apply</button>' +
          ' <button class="reject" data-idx="' + i + '">Discard</button>' +
          '<div class="chatmeta apply-result" id="apply-result-' + i + '"></div>' +
          '</div>';
      });
    }
    document.getElementById('answer').innerHTML =
      '<h2>Answer</h2>' +
      '<div class="chatanswer">' + esc(data.answer).replace(/\\n\\n/g, '</p><p>').replace(/\\n/g, '<br>') + '</div>' +
      '<p class="chatmeta">Cited: ' + (cited || '<em>none</em>') + '</p>' +
      '<p class="chatmeta">Context entries: ' + (ctx || '<em>none</em>') + '</p>' +
      '<p class="chatmeta"><em>' + esc(data.duration_ms) + 'ms · ' + esc(data.model_used) + '</em></p>' +
      actionsHtml;
    document.querySelectorAll('button.accept').forEach(btn => {
      btn.addEventListener('click', async function() {
        const idx = parseInt(btn.dataset.idx, 10);
        const action = data.suggested_actions[idx];
        const ar = await fetch('/api/chat/apply_action', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(action),
        });
        const aData = await ar.json();
        document.getElementById('apply-result-' + idx).innerHTML =
          ar.ok ? ('<strong>' + esc(aData.status) + '</strong>: ' + esc(aData.detail) + ' (proposal ' + esc(aData.proposal_id.slice(0, 8)) + '…)')
                : ('<span style="color:#a00">error: ' + esc(aData.detail || ar.status) + '</span>');
        btn.disabled = true;
      });
    });
    document.querySelectorAll('button.reject').forEach(btn => {
      btn.addEventListener('click', function() {
        const idx = parseInt(btn.dataset.idx, 10);
        document.getElementById('apply-result-' + idx).innerHTML = '<em>discarded</em>';
        btn.disabled = true;
        document.querySelector('button.accept[data-idx="' + idx + '"]').disabled = true;
      });
    });
  } catch (err) {
    document.getElementById('answer').innerHTML = '<p style="color:#a00">network error: ' + esc(err) + '</p>';
  }
});
</script>
"""
        return _base("Chat", body, config.mongo.database, active="/chat")

    @app.post("/api/chat")
    def chat_api(
        request: Request, payload: dict = Body(default_factory=dict)
    ) -> JSONResponse:
        from mahalath.adapters import make_adapter
        from mahalath.adapters.base import AdapterError as _AdapterError
        from mahalath.chat import answer_question
        from mahalath.style import load_style_overlay

        config: AppConfig = request.app.state.config
        question = str(payload.get("question", "")).strip()
        if not question:
            raise HTTPException(400, "question is required")
        focus_label = payload.get("focus_label")
        max_ctx = int(payload.get("max_context_entries", 10))

        db = get_database(config)

        try:
            adapter = make_adapter(config.runtime.chat_adapter, config)
        except _AdapterError as exc:
            raise HTTPException(
                503,
                f"chat adapter unavailable: {exc} — set chat_adapter in config "
                "or ANTHROPIC_API_KEY for the default claude_api.",
            )

        overlay = load_style_overlay(config)

        try:
            response = answer_question(
                question, db, adapter,
                max_context_entries=max_ctx,
                focus_label=focus_label,
                style_overlay=overlay,
            )
        except _AdapterError as exc:
            raise HTTPException(502, f"adapter error: {exc}")

        return JSONResponse({
            "question": response.question,
            "answer": response.answer,
            "context_labels": response.context_labels,
            "cited_labels": response.cited_labels,
            "suggested_actions": [
                {
                    "type": a.type,
                    "payload": a.payload,
                    "reasoning": a.reasoning,
                    "confidence": a.confidence,
                }
                for a in response.suggested_actions
            ],
            "model_used": response.model_used,
            "duration_ms": response.duration_ms,
        })

    @app.post("/api/retrieve")
    def retrieve_api(
        request: Request, payload: dict = Body(default_factory=dict)
    ) -> JSONResponse:
        """Prompt-ready codified bundle for an orchestrating LLM (S-C).

        Body: {"terms": [...], "labels": [...], "filters": {branch,
        context, status, min_confidence, intent}, "token_budget": N,
        "format": "json"|"text"}. Labels accept frame-scoped handles
        ("MPL-004#structural"). Returns the bundle (all frames per
        entry, ADR-022; reference-closed, ADR-023).
        """
        from mahalath.retrieval import Filters, build_bundle

        config: AppConfig = request.app.state.config
        terms = [str(t) for t in (payload.get("terms") or [])]
        labels = [str(t) for t in (payload.get("labels") or [])]
        refs_or_terms = labels + terms
        if not refs_or_terms:
            raise HTTPException(400, "terms or labels are required")

        raw_filters = payload.get("filters") or {}
        min_conf = raw_filters.get("min_confidence")
        filters = Filters(
            language=str(raw_filters.get("language") or "en"),
            branch=raw_filters.get("branch"),
            context_name=raw_filters.get("context"),
            status=raw_filters.get("status"),
            min_confidence=float(min_conf) if min_conf is not None else None,
            intent_tag=raw_filters.get("intent"),
        )
        token_budget = int(payload.get("token_budget", 1500))
        out_format = str(payload.get("format", "json"))

        db = get_database(config)
        ensure_indexes(db)  # the $text index backs fuzzy matching
        bundle = build_bundle(
            db, refs_or_terms, token_budget=token_budget, filters=filters,
        )
        if out_format == "text":
            return JSONResponse({"ok": True, "text": bundle.as_text})
        return JSONResponse({"ok": True, "bundle": bundle.to_dict()})

    @app.post("/api/propose_term")
    def propose_term_api(
        request: Request, payload: dict = Body(default_factory=dict)
    ) -> JSONResponse:
        """Propose a term the ontology doesn't confidently cover (S-D).

        Body: {"term": str, "context": str?, "near": "MPL-x"?,
        "dry_run": bool?}. Returns the ProposalTemplate: existing
        matches when the term is covered, otherwise the enqueued
        undecided-queue handle. The caller decides when to call this —
        /api/retrieve never enqueues on its own (retrieval-spec Q3).
        """
        from mahalath.retrieval import propose_term

        config: AppConfig = request.app.state.config
        term = str(payload.get("term", "")).strip()
        if not term:
            raise HTTPException(400, "term is required")

        db = get_database(config)
        ensure_indexes(db)
        template = propose_term(
            db, term,
            context=payload.get("context"),
            near=payload.get("near"),
            enqueue=not bool(payload.get("dry_run", False)),
            language=str(payload.get("language") or "en"),
        )
        return JSONResponse({"ok": True, **template.to_dict()})

    @app.post("/api/chat/apply_action")
    def chat_apply_action(
        request: Request, payload: dict = Body(default_factory=dict)
    ) -> JSONResponse:
        from mahalath.actions import (
            ProposeAlias as _ProposeAlias,
            ProposeParent as _ProposeParent,
            dispatch as _dispatch,
        )

        config: AppConfig = request.app.state.config
        db = get_database(config)
        atype = str(payload.get("type", "")).strip()
        action_payload = payload.get("payload") or {}
        try:
            confidence = float(payload.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        # Client-supplied — clamp to the valid scale so a forged request
        # cannot inflate its way past validation elsewhere.
        confidence = max(0.0, min(confidence, config.runtime.confidence_scale_max))
        reasoning = str(payload.get("reasoning", "")).strip() or (
            "operator confirmed via chat"
        )

        if atype == "propose_parent":
            action = _ProposeParent(
                child_label=str(action_payload.get("child_label", "")),
                parent_label=str(action_payload.get("parent_label", "")),
                reason=reasoning,
                confidence=confidence,
                proposed_by="operator_via_chat",
            )
        elif atype == "propose_alias":
            action = _ProposeAlias(
                label=str(action_payload.get("label", "")),
                alias=str(action_payload.get("alias", "")),
                reason=reasoning,
                confidence=confidence,
                proposed_by="operator_via_chat",
            )
        else:
            raise HTTPException(400, f"unsupported action type: {atype!r}")

        result = _dispatch(action, db)
        from mahalath.glossary import refresh_glossary
        if result.status == "applied":
            refresh_glossary(config, db)
        return JSONResponse({
            "ok": True,
            "proposal_id": result.proposal_id,
            "status": result.status,
            "detail": result.detail,
            "payload": result.payload,
        })

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
        return _base("Documents", body, config.mongo.database, active="/documents")
