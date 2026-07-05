---
type: Module
title: web
description: The local operator browser (mahalath.web.app) — dashboard, ontology browser with frame-grouped definitions, proposal review with accept/reject/rollback, undecided queue, documents, effectiveness self-analysis, grounded chat, and the retrieval API.
resource: https://github.com/gellsmore-svg/mahalath/blob/main/src/mahalath/web/app.py
tags: [mahalath, module, web, operator]
timestamp: 2026-07-05T00:00:00Z
---

# web

A single-file FastAPI app (routes + f-string templates + token CSS with
automatic dark mode via `prefers-color-scheme`): no business logic — it
marshals repository data into HTML and forwards decisions to
`accept_proposal` / `reject_proposal` / `rollback_proposal`. Binds
`127.0.0.1`; no auth (operator-local).

Pages: dashboard (clickable summary cards), ontology list/detail (confidence
meters; definitions grouped by frame so polysemy is visible), proposals with
chip filters and the decide/rollback forms, undecided queue, documents,
effectiveness (§3.4 self-analysis, also at `/api/effectiveness`), and a
read-only grounded chat (`/api/chat`, with operator-confirmable suggested
actions). Machine seams: `/api/retrieve` (prompt-ready codified bundles) and
`/api/propose_term`.
