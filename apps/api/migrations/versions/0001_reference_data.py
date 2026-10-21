"""Initial site, line, asset and immutable tag-contract metadata.

Revision ID: 0001_reference_data
Revises: none
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_reference_data"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_sites_code"),
        sa.CheckConstraint("length(id) > 0 AND length(code) > 0", name="ck_sites_identity"),
    )
    op.create_table(
        "lines",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("site_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["site_id"], ["sites.id"], name="fk_lines_site", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("site_id", "code", name="uq_lines_site_code"),
        sa.CheckConstraint("length(id) > 0 AND length(code) > 0", name="ck_lines_identity"),
    )
    op.create_index("ix_lines_site_id", "lines", ["site_id"])
    op.create_table(
        "assets",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("line_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["line_id"], ["lines.id"], name="fk_assets_line", ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("line_id", "code", name="uq_assets_line_code"),
        sa.UniqueConstraint("id", "line_id", "code", name="uq_assets_identity_line_code"),
        sa.CheckConstraint("length(id) > 0 AND length(code) > 0", name="ck_assets_identity"),
    )
    op.create_index("ix_assets_line_id", "assets", ["line_id"])
    op.create_table(
        "tag_definitions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("line_id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.Text(), nullable=True),
        sa.Column("tag_group", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("logical_name", sa.Text(), nullable=False),
        sa.Column("namespace_uri", sa.Text(), nullable=False),
        sa.Column("identifier_type", sa.Text(), nullable=False),
        sa.Column("node_identifier", sa.Text(), nullable=False),
        sa.Column("data_type", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("enum_name", sa.Text(), nullable=True),
        sa.Column("access", sa.Text(), nullable=False),
        sa.Column("polling_interval_ms", sa.Integer(), nullable=False),
        sa.Column("analog_history", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("contract_hash", sa.Text(), nullable=False),
        sa.Column("contract_payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["line_id"], ["lines.id"], name="fk_tags_line", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["asset_id", "line_id", "tag_group"],
            ["assets.id", "assets.line_id", "assets.code"],
            name="fk_tags_asset_line_group",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("logical_name", name="uq_tags_logical_name"),
        sa.UniqueConstraint(
            "line_id", "namespace_uri", "node_identifier", name="uq_tags_line_address"
        ),
        sa.CheckConstraint(
            "(tag_group = 'Line' AND asset_id IS NULL) OR "
            "(tag_group IN ('Infeed','Sealer','Outfeed') AND asset_id IS NOT NULL)",
            name="ck_tags_group_owner",
        ),
        sa.CheckConstraint(
            "data_type IN ('Int32','UInt32','UInt64','Boolean','String','Double')",
            name="ck_tags_data_type",
        ),
        sa.CheckConstraint("identifier_type = 'String'", name="ck_tags_identifier_type"),
        sa.CheckConstraint("access = 'read_only'", name="ck_tags_read_only"),
        sa.CheckConstraint("polling_interval_ms > 0", name="ck_tags_polling_interval"),
        sa.CheckConstraint("length(contract_hash) = 64", name="ck_tags_contract_hash"),
        sa.CheckConstraint(
            "length(id) > 0 AND length(logical_name) > 0 AND "
            "length(namespace_uri) > 0 AND length(node_identifier) > 0",
            name="ck_tags_identity",
        ),
    )
    op.create_index("ix_tag_definitions_line_id", "tag_definitions", ["line_id"])
    op.create_index("ix_tag_definitions_asset_id", "tag_definitions", ["asset_id"])


def downgrade() -> None:
    # Explicitly destructive; callers must select a disposable DB or reviewed backup first.
    op.drop_table("tag_definitions")
    op.drop_table("assets")
    op.drop_table("lines")
    op.drop_table("sites")
