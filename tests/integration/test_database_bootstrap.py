"""Real PostgreSQL 17 migration/metadata tests; Docker absence is an explicit failure."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from apps.api.bootstrap import (
    ASSET_IDS,
    LINE_ID,
    SITE_ID,
    BootstrapConflict,
    bootstrap_reference_data,
)
from apps.api.database import (
    INITIAL_REVISION,
    REPOSITORY_ROOT,
    database_url_from_env,
    migration_config,
)
from apps.api.models import Base, TagDefinition
from contracts.tags.validate import GROUP_COUNTS, load_contract


def test_migration_upgrade_downgrade_and_model_agreement(engine):
    with engine.connect() as connection:
        command.upgrade(migration_config(connection), INITIAL_REVISION)
        assert MigrationContext.configure(connection).get_current_heads() == (INITIAL_REVISION,)
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "sites",
            "lines",
            "assets",
            "tag_definitions",
        }
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        connection.rollback()
        command.downgrade(migration_config(connection), "base")
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
        assert MigrationContext.configure(connection).get_current_heads() == ()
        connection.rollback()
        command.upgrade(migration_config(connection), INITIAL_REVISION)


def test_bootstrap_requires_migration(engine):
    with pytest.raises(ValueError, match="migration"):
        bootstrap_reference_data(engine)
    assert inspect(engine).get_table_names() == []


def test_bootstrap_twice_matches_committed_tag_dictionary(migrated):
    assert bootstrap_reference_data(migrated) == {
        "sites": 1,
        "lines": 1,
        "assets": 3,
        "tag_definitions": 48,
    }
    assert bootstrap_reference_data(migrated) == {
        "sites": 0,
        "lines": 0,
        "assets": 0,
        "tag_definitions": 0,
    }
    contract = load_contract()
    with migrated.connect() as connection:
        for table, expected in (("sites", 1), ("lines", 1), ("assets", 3)):
            assert (
                connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == expected
            )
        rows = connection.execute(select(TagDefinition.__table__)).mappings().all()
        assert len(rows) == 48
        assert Counter(row["tag_group"] for row in rows) == GROUP_COUNTS
        assert sum(row["analog_history"] for row in rows) == 9
        expected_by_id = {tag["logical_name"]: tag for tag in contract["tags"]}
        for row in rows:
            tag = expected_by_id[row["id"]]
            assert row["contract_payload"]["tag"] == tag
            assert row["contract_payload"]["enums"] == contract["enums"]
            assert row["contract_payload"]["quality"] == contract["quality"]
            assert row["namespace_uri"] == contract["namespace_uri"]
            assert row["node_identifier"] == tag["node_id"]["identifier"]
            assert row["identifier_type"] == "String"
            assert row["data_type"] == tag["data_type"]
            assert row["unit"] == tag["unit"]
            assert row["enum_name"] == tag["enum"]
            assert row["access"] == "read_only"
            assert row["polling_interval_ms"] == 1000
            assert row["line_id"] == LINE_ID
            assert row["asset_id"] == ASSET_IDS.get(tag["group"])


def test_changed_contract_refuses_and_rolls_back_earlier_insertion(migrated):
    bootstrap_reference_data(migrated)
    contract = copy.deepcopy(load_contract())
    first_id = contract["tags"][0]["logical_name"]
    last_id = contract["tags"][-1]["logical_name"]
    contract["tags"][-1]["unit"] = "changed-unit"
    with migrated.begin() as connection:
        connection.execute(TagDefinition.__table__.delete().where(TagDefinition.id == first_id))
    with pytest.raises(BootstrapConflict, match=last_id):
        bootstrap_reference_data(migrated, contract)
    with migrated.connect() as connection:
        assert (
            connection.execute(select(TagDefinition.id).where(TagDefinition.id == first_id)).first()
            is None
        )
        assert (
            connection.execute(
                select(TagDefinition.unit).where(TagDefinition.id == last_id)
            ).scalar_one()
            != "changed-unit"
        )
    assert bootstrap_reference_data(migrated)["tag_definitions"] == 1


def test_existing_metadata_tamper_is_not_overwritten(migrated):
    bootstrap_reference_data(migrated)
    with migrated.begin() as connection:
        connection.execute(
            TagDefinition.__table__.update()
            .where(TagDefinition.id == "Line.State")
            .values(data_type="String")
        )
    with pytest.raises(BootstrapConflict, match="Line.State"):
        bootstrap_reference_data(migrated)
    with migrated.connect() as connection:
        assert (
            connection.execute(
                select(TagDefinition.data_type).where(TagDefinition.id == "Line.State")
            ).scalar_one()
            == "String"
        )


def test_parallel_bootstraps_do_not_duplicate_rows(migrated):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: bootstrap_reference_data(migrated), range(2)))
    assert sorted(result["tag_definitions"] for result in results) == [0, 48]


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        (
            "INSERT INTO lines VALUES ('orphan','absent','ORPHAN','Orphan')",
            "fk_lines_site",
        ),
        (
            "INSERT INTO assets VALUES ('orphan','absent','Infeed','Orphan')",
            "fk_assets_line",
        ),
        (
            "INSERT INTO sites VALUES ('duplicate','LOCAL','Duplicate')",
            "uq_sites_code",
        ),
        (
            "INSERT INTO lines VALUES ('duplicate','site-local','PACKAGING-1','Duplicate')",
            "uq_lines_site_code",
        ),
        (
            "INSERT INTO assets VALUES ('duplicate','packaging-line-1','Infeed','Duplicate')",
            "uq_assets_line_code",
        ),
        ("DELETE FROM sites WHERE id = 'site-local'", "fk_lines_site"),
        ("DELETE FROM lines WHERE id = 'packaging-line-1'", "fk_assets_line"),
    ],
)
def test_parent_and_identity_constraints_are_enforced(migrated, sql, constraint):
    bootstrap_reference_data(migrated)
    with pytest.raises(IntegrityError) as error, migrated.begin() as connection:
        connection.execute(text(sql))
    assert error.value.orig.diag.constraint_name == constraint


@pytest.mark.parametrize(
    ("changes", "constraint"),
    [
        ({"logical_name": "Line.State"}, "uq_tags_logical_name"),
        ({"node_identifier": "Line.State"}, "uq_tags_line_address"),
        ({"line_id": "absent"}, "fk_tags_line"),
        ({"asset_id": ASSET_IDS["Sealer"], "tag_group": "Infeed"}, "fk_tags_asset_line_group"),
        ({"asset_id": None, "tag_group": "Infeed"}, "ck_tags_group_owner"),
        ({"access": "read_write"}, "ck_tags_read_only"),
        ({"identifier_type": "Numeric"}, "ck_tags_identifier_type"),
        ({"data_type": "Float32"}, "ck_tags_data_type"),
        ({"polling_interval_ms": 0}, "ck_tags_polling_interval"),
    ],
)
def test_tag_constraints_are_enforced_in_postgresql(migrated, changes, constraint):
    bootstrap_reference_data(migrated)
    with migrated.connect() as connection:
        row = dict(connection.execute(select(TagDefinition.__table__)).mappings().first())
    row.update(id="new-tag", logical_name="new-tag", node_identifier="new-tag")
    row.update(changes)
    with pytest.raises(IntegrityError) as error, migrated.begin() as connection:
        connection.execute(TagDefinition.__table__.insert().values(**row))
    assert error.value.orig.diag.constraint_name == constraint


def test_asset_from_different_line_is_rejected(migrated):
    bootstrap_reference_data(migrated)
    with migrated.begin() as connection:
        connection.execute(
            text("INSERT INTO lines VALUES ('other-line',:site,'OTHER','Other')"), {"site": SITE_ID}
        )
        connection.execute(
            text("INSERT INTO assets VALUES ('other-asset','other-line','Infeed','Other')")
        )
    with pytest.raises(IntegrityError) as error, migrated.begin() as connection:
        connection.execute(
            TagDefinition.__table__.update()
            .where(TagDefinition.id == "Infeed.State")
            .values(asset_id="other-asset")
        )
    assert error.value.orig.diag.constraint_name == "fk_tags_asset_line_group"


def test_bootstrap_cli_requires_dsn_and_reports_no_secret(monkeypatch, capsys):
    from apps.api.bootstrap import main

    monkeypatch.delenv("HMI_DATABASE_DSN", raising=False)
    assert main() == 1
    monkeypatch.setenv("HMI_DATABASE_DSN", "bad-dsn-with-private-value")
    assert main() == 1
    captured = capsys.readouterr()
    assert "bad-dsn-with-private-value" not in captured.err
    assert "HMI_DATABASE_DSN" in captured.err
    monkeypatch.setenv("HMI_DATABASE_DSN", "sqlite:///unsupported.sqlite3")
    with pytest.raises(ValueError, match="PostgreSQL"):
        database_url_from_env()
    monkeypatch.setenv("HMI_DATABASE_DSN", "postgresql://")
    with pytest.raises(ValueError, match="explicitly"):
        database_url_from_env()


def test_bootstrap_module_cli_with_migrated_database(migrated):
    cli_env = os.environ.copy()
    cli_env["HMI_DATABASE_DSN"] = migrated.url.render_as_string(hide_password=False)
    outputs = []
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "apps.api.bootstrap"],
            cwd=REPOSITORY_ROOT,
            env=cli_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0
        assert migrated.url.password not in result.stdout + result.stderr
        outputs.append(result.stdout)
    assert '"tag_definitions": 48' in outputs[0]
    assert '"tag_definitions": 0' in outputs[1]
