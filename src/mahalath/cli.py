"""Mahalath CLI entry point.

Stage 0 scaffold: enough commands to verify the MongoDB connection and
print the loaded config. Real ingestion / debate / write commands land
in Stage 1.

Commands available now:

    mahalath db-ping
    mahalath show-config

Commands intended for Stage 1 (placeholders raise NotImplementedError):

    mahalath ingest-one <path>
    mahalath debate-one <term>
    mahalath process-input
    mahalath list-ontology
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from mahalath.config import AppConfig, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mahalath")
    subcommands = parser.add_subparsers(dest="command")

    subcommands.add_parser("db-ping", help="Ping the configured MongoDB instance.")
    subcommands.add_parser("show-config", help="Print the loaded configuration as JSON.")

    ingest_one = subcommands.add_parser(
        "ingest-one", help="(Stage 1) Ingest a single document by path."
    )
    ingest_one.add_argument("path")

    debate_one = subcommands.add_parser(
        "debate-one", help="(Stage 1) Run one debate cycle on a single candidate term."
    )
    debate_one.add_argument("term")

    subcommands.add_parser(
        "process-input",
        help="(Stage 1) Process anything in the watched input/ folder.",
    )
    subcommands.add_parser(
        "list-ontology", help="(Stage 1) List ontology entries."
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    config = load_config()

    if args.command == "show-config":
        print(json.dumps(_config_as_dict(config), indent=2, default=str))
        return 0

    if args.command == "db-ping":
        return _db_ping(config)

    if args.command in {"ingest-one", "debate-one", "process-input", "list-ontology"}:
        print(
            f"mahalath: command {args.command!r} is a Stage 1 placeholder "
            "and not yet implemented.",
            file=sys.stderr,
        )
        return 2

    parser.print_help()
    return 0


def _config_as_dict(config: AppConfig) -> dict[str, Any]:
    return json.loads(config.model_dump_json())


def _db_ping(config: AppConfig) -> int:
    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError as exc:
        print(
            f"mahalath: pymongo is not installed in this environment: {exc}",
            file=sys.stderr,
        )
        return 3

    client = MongoClient(config.mongo.uri, serverSelectionTimeoutMS=2000)
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        print(
            f"mahalath: MongoDB ping failed at {config.mongo.uri}: {exc}",
            file=sys.stderr,
        )
        return 4
    finally:
        client.close()

    print(
        json.dumps(
            {
                "ok": True,
                "uri": config.mongo.uri,
                "database": config.mongo.database,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
