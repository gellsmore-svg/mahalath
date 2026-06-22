"""Tests for the consolidated `migrate` framework (mahalath.migrations)."""

from __future__ import annotations

from mahalath import migrations
from mahalath.migrations import LEDGER


def test_status_on_fresh_db_lists_all_pending(mongo_db):
    st = migrations.status(mongo_db)
    assert st["current_version"] == 0
    assert st["target_version"] == migrations.MIGRATIONS[-1].number
    assert [m["number"] for m in st["pending"]] == [m.number for m in migrations.MIGRATIONS]


def test_migrate_runs_all_pending_and_records_them(mongo_db):
    report = migrations.migrate(mongo_db)
    assert report["ok"] is True and report["dry_run"] is False
    assert [r["number"] for r in report["ran"]] == [m.number for m in migrations.MIGRATIONS]
    assert all(r["status"] == "applied" for r in report["ran"])
    assert report["current_version"] == migrations.MIGRATIONS[-1].number
    # ledger now holds one row per migration
    assert mongo_db[LEDGER].count_documents({}) == len(migrations.MIGRATIONS)


def test_migrate_is_idempotent(mongo_db):
    migrations.migrate(mongo_db)
    again = migrations.migrate(mongo_db)
    assert again["ran"] == []  # nothing pending the second time
    assert migrations.status(mongo_db)["pending"] == []


def test_dry_run_changes_nothing(mongo_db):
    report = migrations.migrate(mongo_db, dry_run=True)
    assert report["dry_run"] is True
    assert all(r["status"] == "would-run" for r in report["ran"])
    assert mongo_db[LEDGER].count_documents({}) == 0  # nothing recorded
    assert migrations.current_version(mongo_db) == 0


def test_partial_ledger_only_runs_remaining(mongo_db):
    # Pretend migration 1 already ran.
    mongo_db[LEDGER].insert_one({"_id": 1, "name": "backfill_references"})
    pending = migrations.pending(mongo_db)
    assert [m.number for m in pending] == [2, 3]
    report = migrations.migrate(mongo_db)
    assert [r["number"] for r in report["ran"]] == [2, 3]
