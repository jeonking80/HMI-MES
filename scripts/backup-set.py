#!/usr/bin/env python3
"""Explicit local stopped-writer backup/isolated restore operator."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine

from deploy.backup_set import create_backup_set, local_url, restore_backup_set, resume_restored_set


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--simulator", type=Path, required=True)
    backup.add_argument("--gateway", type=Path, required=True)
    backup.add_argument("--settings", type=Path, required=True)
    backup.add_argument("--backup-set-id", required=True)
    backup.add_argument("--destination", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--directory", type=Path, required=True)
    restore.add_argument("--database-name", required=True)
    restore.add_argument("--destination", type=Path, required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("--directory", type=Path, required=True)
    resume.add_argument("--restored-directory", type=Path, required=True)
    resume.add_argument("--operator-id", required=True)
    resume.add_argument("--acknowledge-verified-recovery", action="store_true", required=True)
    args = parser.parse_args()
    dsn = os.environ.get("HMI_BACKUP_DATABASE_DSN")
    if not dsn:
        parser.error("HMI_BACKUP_DATABASE_DSN must explicitly select a local source/admin database")
    engine = create_engine(local_url(dsn), hide_parameters=True)
    try:
        if args.command == "backup":
            result = create_backup_set(
                engine=engine,
                simulator=args.simulator,
                gateway=args.gateway,
                settings=json.loads(args.settings.read_text()),
                destination=args.destination,
                backup_set_id=args.backup_set_id,
            )
        elif args.command == "resume":
            result = resume_restored_set(
                engine=engine,
                directory=args.directory,
                restored_directory=args.restored_directory,
                operator_id=args.operator_id,
                acknowledged=args.acknowledge_verified_recovery,
            )
        else:
            result = restore_backup_set(
                directory=args.directory,
                admin_engine=engine,
                database_name=args.database_name,
                destination=args.destination,
            )
        print(json.dumps({key: value for key, value in result.items() if key != "inventory"}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
