"""Scheduler harness tests.

The blocking-mode path is not unit-tested (it would actually block).
We exercise:

  - `--once` mode calls both injected job functions in the right order
  - REM job is skipped when `rem.enabled` is False
  - Default rem_fn counts undecided items without crashing
"""

from __future__ import annotations

from mahalath.config import AppConfig, RemConfig
from mahalath.scheduler import _default_rem_job, run_scheduler


def test_once_calls_both_jobs() -> None:
    config = AppConfig()
    calls: list[str] = []

    def fake_process(_cfg) -> None:
        calls.append("process")

    def fake_rem(_cfg) -> None:
        calls.append("rem")

    rc = run_scheduler(
        config, once=True,
        process_input_fn=fake_process,
        rem_fn=fake_rem,
    )
    assert rc == 0
    assert calls == ["process", "rem"]


def test_once_skips_rem_when_disabled() -> None:
    config = AppConfig(rem=RemConfig(enabled=False))
    calls: list[str] = []
    run_scheduler(
        config, once=True,
        process_input_fn=lambda _c: calls.append("process"),
        rem_fn=lambda _c: calls.append("rem"),
    )
    assert calls == ["process"]


def test_default_rem_job_counts_undecided(
    mongo_db, mongo_config, tmp_path, monkeypatch
) -> None:
    """Default REM job must run cleanly even with an empty undecided queue."""
    # Stand up a config that uses the test database the mongo_db fixture
    # opens, so the default REM job hits a real (empty) collection. The
    # job tail writes the effectiveness snapshot to cwd-relative logs/,
    # so run from a temp dir to keep the repo's logs/ test-free.
    monkeypatch.chdir(tmp_path)
    _default_rem_job(mongo_config)
    snapshot = tmp_path / "logs" / "effectiveness.jsonl"
    assert snapshot.exists() and snapshot.read_text().count("\n") == 1


def test_once_passes_through_overrides() -> None:
    config = AppConfig()
    seen: dict[str, object] = {}

    def fake_process(cfg) -> None:
        seen["got_config"] = cfg

    run_scheduler(
        config, once=True, poll_seconds=999, rem_cron="*/5 * * * *",
        process_input_fn=fake_process,
        rem_fn=lambda _c: None,
    )
    # The overrides matter for the non-once path; in --once we just
    # verify the call was made with the original config object.
    assert seen["got_config"] is config
