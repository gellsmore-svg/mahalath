"""Shared pytest fixtures.

`mongo_db` gives each test an isolated database prefixed with
`mahalath_pytest` that is dropped before and after to avoid bleed-over
between runs.
Tests that don't need MongoDB simply don't request the fixture.
"""

from __future__ import annotations

import hashlib
import os
import re

import pytest

from mahalath.config import AppConfig, MongoConfig
from mahalath.db import close_all, ensure_indexes, get_database


TEST_DB_NAME = "mahalath_pytest"


def _test_database_name(nodeid: str) -> str:
    """Return a MongoDB-safe, per-test database name under 64 bytes."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    key = f"{nodeid}:{worker}" if worker else nodeid
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", nodeid).strip("_").lower()
    slug = slug[:28] or "case"
    return f"{TEST_DB_NAME}_{slug}_{digest}"


@pytest.fixture
def mongo_config(request: pytest.FixtureRequest) -> AppConfig:
    return AppConfig(
        mongo=MongoConfig(database=_test_database_name(request.node.nodeid))
    )


@pytest.fixture
def mongo_db(mongo_config: AppConfig):
    db_name = mongo_config.mongo.database
    db = get_database(mongo_config)
    db.client.drop_database(db_name)
    ensure_indexes(db)
    try:
        yield db
    finally:
        db.client.drop_database(db_name)
        close_all()
