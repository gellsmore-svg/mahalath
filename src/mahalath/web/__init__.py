"""Mahalath web UI package.

A minimal FastAPI dashboard for browsing the ontology and acting on
the proposal queue. Read-only views for entries / undecided / documents;
forms for accept / reject / rollback of action proposals.

Optional install:

    pip install -e ".[web]"

Run with:

    mahalath serve [--host 127.0.0.1] [--port 8000]
"""

from mahalath.web.app import create_app

__all__ = ["create_app"]
