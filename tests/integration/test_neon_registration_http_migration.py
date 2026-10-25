"""Reviewed head18 -> 19 HTTP batches match canonical migration and grants."""

import json
from pathlib import Path

from alembic import command

from apps.api.database import migration_config
from deploy.generate_neon_init import connect
from tests.integration.test_clock_runtime_grants import head17 as head17
from tests.integration.test_clock_runtime_grants import head18 as head18
from tests.integration.test_kuls_runtime_grants import installed as installed
from tests.integration.test_neon_clock_http_migration import catalog, renderer, upgrade_alembic
from tests.integration.test_neon_http_migration import http_batch

ROOT = Path(__file__).resolve().parents[2]


def test_registration_http_upgrade_catalog_and_grants_match_alembic(head18):
    url, migrator = head18
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-clock-grants.sql").read_text())
    upgrade_alembic(migrator, "0019_registration_ids")
    canonical = catalog(url)
    with migrator.connect() as db:
        command.downgrade(migration_config(db), "0018_clock_evidence")
    upgrade = json.loads((ROOT / "deploy/neon-http/upgrade-0018-0019.json").read_text())
    grants = json.loads((ROOT / "deploy/neon-http/grants-0019.json").read_text())
    assert upgrade == renderer.registration_statements()
    assert grants == renderer.registration_grant_statements()
    http_batch(url, upgrade)
    assert catalog(url) == canonical
    http_batch(url, grants)
    granted = catalog(url)
    with connect(url) as db:
        db.execute((ROOT / "deploy/neon-registration-grants.sql").read_text())
    assert catalog(url) == granted
