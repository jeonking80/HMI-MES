"""Initial reference metadata only; business/event/auth tables are not implemented."""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Site(Base):
    __tablename__ = "sites"
    __table_args__ = (
        UniqueConstraint("code", name="uq_sites_code"),
        CheckConstraint("length(id) > 0 AND length(code) > 0", name="ck_sites_identity"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


class Line(Base):
    __tablename__ = "lines"
    __table_args__ = (
        UniqueConstraint("site_id", "code", name="uq_lines_site_code"),
        CheckConstraint("length(id) > 0 AND length(code) > 0", name="ck_lines_identity"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    site_id: Mapped[str] = mapped_column(
        ForeignKey("sites.id", name="fk_lines_site", ondelete="RESTRICT"), index=True
    )
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("line_id", "code", name="uq_assets_line_code"),
        UniqueConstraint("id", "line_id", "code", name="uq_assets_identity_line_code"),
        CheckConstraint("length(id) > 0 AND length(code) > 0", name="ck_assets_identity"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    line_id: Mapped[str] = mapped_column(
        ForeignKey("lines.id", name="fk_assets_line", ondelete="RESTRICT"), index=True
    )
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)


class TagDefinition(Base):
    __tablename__ = "tag_definitions"
    __table_args__ = (
        UniqueConstraint("logical_name", name="uq_tags_logical_name"),
        UniqueConstraint(
            "line_id", "namespace_uri", "node_identifier", name="uq_tags_line_address"
        ),
        ForeignKeyConstraint(
            ["asset_id", "line_id", "tag_group"],
            ["assets.id", "assets.line_id", "assets.code"],
            name="fk_tags_asset_line_group",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(tag_group = 'Line' AND asset_id IS NULL) OR "
            "(tag_group IN ('Infeed','Sealer','Outfeed') AND asset_id IS NOT NULL)",
            name="ck_tags_group_owner",
        ),
        CheckConstraint(
            "data_type IN ('Int32','UInt32','UInt64','Boolean','String','Double')",
            name="ck_tags_data_type",
        ),
        CheckConstraint("identifier_type = 'String'", name="ck_tags_identifier_type"),
        CheckConstraint("access = 'read_only'", name="ck_tags_read_only"),
        CheckConstraint("polling_interval_ms > 0", name="ck_tags_polling_interval"),
        CheckConstraint("length(contract_hash) = 64", name="ck_tags_contract_hash"),
        CheckConstraint(
            "length(id) > 0 AND length(logical_name) > 0 AND "
            "length(namespace_uri) > 0 AND length(node_identifier) > 0",
            name="ck_tags_identity",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    line_id: Mapped[str] = mapped_column(
        ForeignKey("lines.id", name="fk_tags_line", ondelete="RESTRICT"), index=True
    )
    asset_id: Mapped[str | None] = mapped_column(Text, index=True)
    tag_group: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    logical_name: Mapped[str] = mapped_column(Text)
    namespace_uri: Mapped[str] = mapped_column(Text)
    identifier_type: Mapped[str] = mapped_column(Text)
    node_identifier: Mapped[str] = mapped_column(Text)
    data_type: Mapped[str] = mapped_column(Text)
    unit: Mapped[str | None] = mapped_column(Text)
    enum_name: Mapped[str | None] = mapped_column(Text)
    access: Mapped[str] = mapped_column(Text)
    polling_interval_ms: Mapped[int] = mapped_column(Integer)
    analog_history: Mapped[bool] = mapped_column(Boolean)
    enabled: Mapped[bool] = mapped_column(Boolean)
    schema_version: Mapped[str] = mapped_column(Text)
    contract_hash: Mapped[str] = mapped_column(Text)
    contract_payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
