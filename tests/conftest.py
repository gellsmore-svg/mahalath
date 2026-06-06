"""Shared pytest fixtures.

`mongo_db` gives each test an isolated database (`mahalath_pytest`)
that is dropped before and after to avoid bleed-over between runs.
Tests that don't need MongoDB simply don't request the fixture.
"""

from __future__ import annotations

import pytest

from mahalath.config import AppConfig, MongoConfig
from mahalath.db import close_all, ensure_indexes, get_database


TEST_DB_NAME = "mahalath_pytest"


@pytest.fixture
def mongo_config() -> AppConfig:
    return AppConfig(mongo=MongoConfig(database=TEST_DB_NAME))


@pytest.fixture
def mongo_db(mongo_config: AppConfig):
    db = get_database(mongo_config)
    db.client.drop_database(TEST_DB_NAME)
    ensure_indexes(db)
    try:
        yield db
    finally:
        db.client.drop_database(TEST_DB_NAME)
        close_all()
