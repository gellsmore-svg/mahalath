"""MongoDB client factory.

Module-level cache keyed by URI: long-running processes (REM scheduler,
FastAPI later) reuse a single connection pool per URI. Short-lived CLI
commands open and close on demand via `close_all`.
"""

from __future__ import annotations

from pymongo import MongoClient
from pymongo.database import Database

from mahalath.config import AppConfig

_clients: dict[str, MongoClient] = {}


def get_client(config: AppConfig, *, timeout_ms: int = 2000) -> MongoClient:
    uri = config.mongo.uri
    if uri not in _clients:
        _clients[uri] = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms)
    return _clients[uri]


def get_database(config: AppConfig, *, timeout_ms: int = 2000) -> Database:
    return get_client(config, timeout_ms=timeout_ms)[config.mongo.database]


def close_all() -> None:
    for client in _clients.values():
        client.close()
    _clients.clear()
