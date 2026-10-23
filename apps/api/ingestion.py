"""PostgreSQL event intake; trusted service callers supply authenticated scope.

No HTTP authentication, job scheduling or PLC-counter reconciliation happens here.
An ACK leaves this module only after its owning database transaction has committed.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from pydantic import TypeAdapter
from sqlalchemy.dialects.postgresql import JSONB

from apps.api.database import SCHEMA_LOCK_ID, SUPPORTED_INGESTION_REVISIONS
from apps.api.models import Asset, Line
from contracts.models import (
    EVENT_ADAPTER,
    UINT64_MAX,
    Event,
    Identifier,
    Parameters,
    ProductionEvent,
    ScrapEvent,
    UInt64,
    UTCTimestamp,
)
from contracts.transport import (
    DurableAck,
    EventReceipt,
    GatewayBatch,
    StreamGap,
    StreamProgress,
    canonical_batch_body,
    validate_ack,
)

INGESTION_REVISION = "0002_ingestion"
INGESTION_METADATA = sa.MetaData()
_ID = TypeAdapter(Identifier)
_UINT = TypeAdapter(UInt64)
_TIME = TypeAdapter(UTCTimestamp)


def _identity(*names: str) -> tuple[sa.CheckConstraint, ...]:
    return tuple(
        sa.CheckConstraint(
            f"length({name}) BETWEEN 1 AND 128 AND {name} !~ '[[:space:]]'",
            name=f"ck_{name}_identifier",
        )
        for name in names
    )


def _uint(name: str, *, positive: bool = False, **kwargs) -> sa.Column:
    return sa.Column(
        name,
        sa.Numeric(20, 0),
        sa.CheckConstraint(
            f"{name} BETWEEN {int(positive)} AND {UINT64_MAX}",
            name=f"ck_{name}_uint64",
        ),
        nullable=False,
        **kwargs,
    )


def _stream_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["source_id", "stream_id"],
        ["source_streams.source_id", "source_streams.stream_id"],
        ondelete="RESTRICT",
    )


source_streams = sa.Table(
    "source_streams",
    INGESTION_METADATA,
    sa.Column("source_id", sa.Text, primary_key=True),
    sa.Column("stream_id", sa.Text, primary_key=True),
    sa.Column("gateway_id", sa.Text, nullable=False, index=True),
    sa.Column(
        "line_id",
        sa.Text,
        sa.ForeignKey(Line.__table__.c.id, ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    _uint("capture_generation"),
    sa.Column("conflicted", sa.Boolean, nullable=False),
    sa.Column("reconciliation_required", sa.Boolean, nullable=False),
    sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
    *_identity("source_id", "stream_id", "gateway_id"),
)
production_executions = sa.Table(
    "production_executions",
    INGESTION_METADATA,
    sa.Column("execution_id", sa.Text, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("stream_id", sa.Text, nullable=False),
    sa.Column("work_order_id", sa.Text, nullable=False),
    sa.Column("recipe_version", sa.Text, nullable=False),
    sa.Column("parameters", JSONB, nullable=False),
    sa.Column("counter_epoch", sa.Text, nullable=False),
    sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
    _stream_fk(),
    *_identity("execution_id", "work_order_id", "recipe_version", "counter_epoch"),
    sa.Index("ix_executions_stream", "source_id", "stream_id"),
)
execution_totals = sa.Table(
    "execution_totals",
    INGESTION_METADATA,
    sa.Column(
        "execution_id",
        sa.Text,
        sa.ForeignKey("production_executions.execution_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    _uint("good_quantity"),
    _uint("reject_quantity"),
    _uint("scrap_quantity"),
)
event_inbox = sa.Table(
    "event_inbox",
    INGESTION_METADATA,
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("stream_id", sa.Text, nullable=False),
    _uint("seq", positive=True),
    sa.Column("execution_id", sa.Text, nullable=True, index=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("body", JSONB, nullable=False),
    sa.Column("content_sha256", sa.Text, nullable=False),
    sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("reason_code", sa.Text, nullable=True),
    sa.Column("reason", sa.Text, nullable=True),
    _stream_fk(),
    *_identity("event_id"),
    sa.UniqueConstraint("source_id", "stream_id", "seq", name="uq_inbox_stream_sequence"),
    sa.CheckConstraint("length(content_sha256)=64", name="ck_inbox_digest"),
    sa.CheckConstraint(
        "(applied_at IS NOT NULL AND reason_code IS NULL AND reason IS NULL) OR "
        "(applied_at IS NULL AND reason_code IS NOT NULL AND reason IS NOT NULL)",
        name="ck_inbox_application",
    ),
    sa.Index(
        "ix_inbox_quarantine",
        "source_id",
        "stream_id",
        "seq",
        postgresql_where=sa.text("applied_at IS NULL"),
    ),
)
production_events = sa.Table(
    "production_events",
    INGESTION_METADATA,
    sa.Column(
        "event_id",
        sa.Text,
        sa.ForeignKey("event_inbox.event_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column(
        "execution_id",
        sa.Text,
        sa.ForeignKey("production_executions.execution_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("kind", sa.Text, nullable=False),
    _uint("quantity", positive=True),
    sa.Column("counter_epoch", sa.Text, nullable=False),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("reason", sa.Text, nullable=True),
    sa.CheckConstraint(
        "kind IN ('PRODUCTION_GOOD','PRODUCTION_REJECT','WIP_SCRAP')", name="ck_production_kind"
    ),
    sa.CheckConstraint("kind!='WIP_SCRAP' OR reason IS NOT NULL", name="ck_scrap_reason"),
)
equipment_events = sa.Table(
    "equipment_state_events",
    INGESTION_METADATA,
    sa.Column(
        "event_id",
        sa.Text,
        sa.ForeignKey("event_inbox.event_id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    sa.Column(
        "execution_id",
        sa.Text,
        sa.ForeignKey("production_executions.execution_id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    ),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("body", JSONB, nullable=False),
    sa.CheckConstraint("kind IN ('STATE_CHANGED','FAULT_CHANGED')", name="ck_equipment_event_kind"),
)
event_apply_errors = sa.Table(
    "event_apply_errors",
    INGESTION_METADATA,
    sa.Column("error_id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column(
        "event_id",
        sa.Text,
        sa.ForeignKey("event_inbox.event_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    ),
    sa.Column("reason_code", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
)
stream_progress = sa.Table(
    "stream_progress",
    INGESTION_METADATA,
    sa.Column("source_id", sa.Text, primary_key=True),
    sa.Column("stream_id", sa.Text, primary_key=True),
    _uint("verified_through_seq"),
    _uint("latest_ingested_seq"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    _stream_fk(),
    sa.CheckConstraint("verified_through_seq<=latest_ingested_seq", name="ck_stream_frontiers"),
)
stream_gaps = sa.Table(
    "stream_gaps",
    INGESTION_METADATA,
    sa.Column("source_id", sa.Text, primary_key=True),
    sa.Column("stream_id", sa.Text, primary_key=True),
    _uint("from_seq", positive=True, primary_key=True),
    _uint("through_seq", positive=True),
    sa.Column("body", JSONB, nullable=False),
    _stream_fk(),
    sa.CheckConstraint("from_seq<=through_seq", name="ck_gap_range"),
)
ingest_batches = sa.Table(
    "ingest_batches",
    INGESTION_METADATA,
    sa.Column("gateway_id", sa.Text, primary_key=True),
    sa.Column("batch_id", sa.Text, primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("stream_id", sa.Text, nullable=False),
    _uint("capture_generation"),
    _uint("capture_seq", positive=True),
    sa.Column("content_sha256", sa.Text, nullable=False),
    sa.Column("body", JSONB, nullable=False),
    sa.Column("ack", JSONB, nullable=False),
    sa.Column("stored_at", sa.DateTime(timezone=True), nullable=False),
    _stream_fk(),
    *_identity("gateway_id", "batch_id"),
    sa.UniqueConstraint(
        "gateway_id", "capture_generation", "capture_seq", name="uq_batch_capture_identity"
    ),
    sa.CheckConstraint("length(content_sha256)=64", name="ck_batch_digest"),
    sa.Index("ix_batches_stream", "source_id", "stream_id"),
)
ingest_conflicts = sa.Table(
    "ingest_conflicts",
    INGESTION_METADATA,
    sa.Column("conflict_id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("source_id", sa.Text, nullable=False),
    sa.Column("stream_id", sa.Text, nullable=False),
    sa.Column("gateway_id", sa.Text, nullable=False),
    sa.Column("batch_id", sa.Text, nullable=False),
    sa.Column("reason_code", sa.Text, nullable=False),
    sa.Column("incoming", JSONB, nullable=False),
    sa.Column("original", JSONB, nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    _stream_fk(),
    sa.Index("ix_conflicts_stream", "source_id", "stream_id"),
)


class IngestConflict(ValueError):
    """Immutable batch/capture/event identity already belongs to other content."""


class SourceBindingError(PermissionError):
    """Authenticated identity or configured source/capture ownership does not match."""


class SourceConflicted(IngestConflict):
    """A durable collision blocks this source pending explicit future reconciliation."""


class IngestionUnavailable(RuntimeError):
    """The configured ingestion schema is not ready."""


def _scope(source_id: str, stream_id: str):
    return sa.and_(source_streams.c.source_id == source_id, source_streams.c.stream_id == stream_id)


def _where(table: sa.Table, source_id: str, stream_id: str):
    return sa.and_(table.c.source_id == source_id, table.c.stream_id == stream_id)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _exact(value: Any) -> int:
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        value = int(value)
    return _UINT.validate_python(value)


class IngestionService:
    def __init__(self, engine: sa.Engine, *, alarm_consumer=None) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("Ingestion requires PostgreSQL")
        self.engine = engine
        # Explicitly configured and started by the owning runtime. Its writes use
        # this same transaction, so no ACK can precede durable alarm processing.
        self.alarm_consumer = alarm_consumer

    @contextmanager
    def _transaction(self):
        with self.engine.begin() as connection:
            # Migrations hold the corresponding exclusive transaction lock.
            connection.execute(
                sa.text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID}
            )
            try:
                revisions = (
                    connection.execute(
                        sa.text(
                            "SELECT version_num FROM alembic_version",
                        )
                    )
                    .scalars()
                    .all()
                )
            except sa.exc.SQLAlchemyError as error:
                raise IngestionUnavailable("Ingestion migration must be applied") from error
            if len(revisions) != 1 or revisions[0] not in SUPPORTED_INGESTION_REVISIONS:
                raise IngestionUnavailable("Unsupported ingestion schema revision")
            yield connection

    @staticmethod
    def _advisory(connection: sa.Connection, namespace: str, identity: str) -> None:
        key = int.from_bytes(
            hashlib.sha256(f"{namespace}:{identity}".encode()).digest()[:8],
            byteorder="big",
            signed=True,
        )
        connection.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    @staticmethod
    def _lock_stream(connection: sa.Connection, source_id: str, stream_id: str):
        row = (
            connection.execute(
                sa.select(source_streams).where(
                    _scope(source_id, stream_id),
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            raise SourceBindingError("Source/stream must be explicitly configured")
        connection.execute(sa.select(Line.id).where(Line.id == row["line_id"]).with_for_update())
        return (
            connection.execute(
                sa.select(source_streams)
                .where(
                    _scope(source_id, stream_id),
                )
                .with_for_update()
            )
            .mappings()
            .one()
        )

    def configure_source(
        self,
        *,
        source_id: str,
        stream_id: str,
        gateway_id: str,
        line_id: str,
        capture_generation: int,
        now: datetime,
    ) -> None:
        values = {
            name: _ID.validate_python(value)
            for name, value in (
                ("source_id", source_id),
                ("stream_id", stream_id),
                ("gateway_id", gateway_id),
                ("line_id", line_id),
            )
        }
        values["capture_generation"] = _UINT.validate_python(capture_generation)
        now = _TIME.validate_python(now)
        with self._transaction() as connection:
            self.configure_source_on_connection(connection, **values, now=now)

    def configure_source_on_connection(
        self,
        connection: sa.Connection,
        *,
        source_id: str,
        stream_id: str,
        gateway_id: str,
        line_id: str,
        capture_generation: int,
        now: datetime,
    ) -> None:
        values = {
            name: _ID.validate_python(value)
            for name, value in (
                ("source_id", source_id),
                ("stream_id", stream_id),
                ("gateway_id", gateway_id),
                ("line_id", line_id),
            )
        }
        values["capture_generation"] = _UINT.validate_python(capture_generation)
        now = _TIME.validate_python(now)
        self._advisory(connection, "gateway", gateway_id)
        line = connection.execute(
            sa.select(Line.id)
            .where(
                Line.id == line_id,
            )
            .with_for_update()
        ).first()
        if line is None:
            raise SourceBindingError("Source must bind to a registered line")
        existing = (
            connection.execute(
                sa.select(source_streams)
                .where(
                    _scope(source_id, stream_id),
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if any(existing[key] != value for key, value in values.items()):
                raise IngestConflict("Source/capture registration is immutable")
            return
        connection.execute(
            source_streams.insert().values(
                **values,
                conflicted=False,
                reconciliation_required=False,
                registered_at=now,
            )
        )
        connection.execute(
            stream_progress.insert().values(
                source_id=source_id,
                stream_id=stream_id,
                verified_through_seq=0,
                latest_ingested_seq=0,
                updated_at=now,
            )
        )

    def register_execution(
        self,
        *,
        execution_id: str,
        source_id: str,
        stream_id: str,
        work_order_id: str,
        recipe_version: str,
        parameters: Parameters,
        counter_epoch: str,
        now: datetime,
        target_quantity: int | None = None,
    ) -> None:
        with self._transaction() as connection:
            self.register_execution_on_connection(
                connection,
                execution_id=execution_id,
                source_id=source_id,
                stream_id=stream_id,
                work_order_id=work_order_id,
                recipe_version=recipe_version,
                parameters=parameters,
                counter_epoch=counter_epoch,
                now=now,
                target_quantity=target_quantity,
            )

    def register_execution_on_connection(
        self,
        connection: sa.Connection,
        *,
        execution_id: str,
        source_id: str,
        stream_id: str,
        work_order_id: str,
        recipe_version: str,
        parameters: Parameters,
        counter_epoch: str,
        now: datetime,
        target_quantity: int | None = None,
    ) -> None:
        from apps.api.execution_plan import (
            ExecutionPlanConflict,
            register_execution_plan,
            validate_target_quantity,
        )

        if target_quantity is not None:
            target_quantity = validate_target_quantity(target_quantity)
        values = {
            name: _ID.validate_python(value)
            for name, value in (
                ("execution_id", execution_id),
                ("source_id", source_id),
                ("stream_id", stream_id),
                ("work_order_id", work_order_id),
                ("recipe_version", recipe_version),
                ("counter_epoch", counter_epoch),
            )
        }
        values["parameters"] = Parameters.model_validate_json(
            parameters.model_dump_json()
        ).model_dump(
            mode="json",
        )
        now = _TIME.validate_python(now)
        if connection.dialect.name != "postgresql" or not connection.in_transaction():
            raise IngestionUnavailable("Caller-owned PostgreSQL transaction required")
        connection.execute(
            sa.text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID}
        )
        revisions = (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars().all()
        )
        if len(revisions) != 1 or revisions[0] not in SUPPORTED_INGESTION_REVISIONS:
            raise IngestionUnavailable("Unsupported ingestion schema revision")
        self._lock_stream(connection, source_id, stream_id)
        self._advisory(connection, "execution", execution_id)
        existing = (
            connection.execute(
                sa.select(production_executions)
                .where(
                    production_executions.c.execution_id == execution_id,
                )
                .with_for_update()
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if any(existing[key] != value for key, value in values.items()):
                raise IngestConflict("Execution registration is immutable")
            if target_quantity is not None:
                try:
                    register_execution_plan(
                        connection, execution_id, target_quantity, new_execution=False
                    )
                except ExecutionPlanConflict:
                    raise IngestConflict("Execution plan is absent or immutable") from None
            return
        connection.execute(production_executions.insert().values(**values, registered_at=now))
        connection.execute(
            execution_totals.insert().values(
                execution_id=execution_id,
                good_quantity=0,
                reject_quantity=0,
                scrap_quantity=0,
            )
        )
        if target_quantity is not None:
            register_execution_plan(connection, execution_id, target_quantity, new_execution=True)

    def _collision(self, connection: sa.Connection, batch: GatewayBatch):
        body = batch.body
        existing = (
            connection.execute(
                sa.select(ingest_batches).where(
                    ingest_batches.c.gateway_id == body.gateway_id,
                    sa.or_(
                        ingest_batches.c.batch_id == body.batch_id,
                        sa.and_(
                            ingest_batches.c.capture_generation == Decimal(body.capture_generation),
                            ingest_batches.c.capture_seq == Decimal(body.capture_seq),
                        ),
                    ),
                )
            )
            .mappings()
            .first()
        )
        if existing is not None:
            if existing["batch_id"] == body.batch_id and existing["content_sha256"] == (
                batch.content_sha256
            ):
                ack = DurableAck.model_validate(existing["ack"])
                validate_ack(batch, ack)
                return ack
            return "BATCH_IDENTITY_CONFLICT", body.model_dump(mode="json"), existing["body"]
        for event in body.events:
            existing = (
                connection.execute(
                    sa.select(event_inbox).where(
                        sa.or_(
                            event_inbox.c.event_id == event.event_id,
                            sa.and_(
                                _where(event_inbox, body.source_id, body.stream_id),
                                event_inbox.c.seq == Decimal(event.seq),
                            ),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if existing is not None and (
                existing["event_id"] != event.event_id
                or existing["content_sha256"] != _digest(event.model_dump(mode="json"))
            ):
                return "EVENT_IDENTITY_CONFLICT", event.model_dump(mode="json"), existing["body"]
        return None

    @staticmethod
    def _receipt(row) -> EventReceipt:
        return EventReceipt(
            event_id=row["event_id"],
            seq=_exact(row["seq"]),
            disposition="APPLIED" if row["applied_at"] is not None else "QUARANTINED",
            reason_code=row["reason_code"],
            reason=row["reason"],
        )

    def _apply_event(self, connection: sa.Connection, event: Event, now: datetime):
        line_id = connection.execute(
            sa.select(source_streams.c.line_id).where(_scope(event.source_id, event.stream_id))
        ).scalar_one()
        # The simulator emits logical groups. Resolve them only inside its configured line;
        # canonical database IDs must prove the same ownership. Keep the original wire body.
        equipment_matches = event.equipment_id in ("Line", line_id)
        if not equipment_matches:
            equipment_matches = (
                connection.execute(
                    sa.select(Asset.id).where(
                        Asset.line_id == line_id,
                        sa.or_(
                            Asset.id == event.equipment_id,
                            sa.and_(
                                sa.literal(event.equipment_id).in_(("Infeed", "Sealer", "Outfeed")),
                                Asset.code == event.equipment_id,
                            ),
                        ),
                    )
                ).first()
                is not None
            )
        if not equipment_matches:
            return (
                "EQUIPMENT_SOURCE_MISMATCH",
                "Equipment does not belong to configured source line",
            )
        execution = None
        if event.execution_id is not None:
            execution = (
                connection.execute(
                    sa.select(production_executions)
                    .where(
                        production_executions.c.execution_id == event.execution_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if execution is None:
                return "UNKNOWN_EXECUTION", "Original execution is not registered"
            if (execution["source_id"], execution["stream_id"]) != (
                event.source_id,
                event.stream_id,
            ):
                return "EXECUTION_STREAM_MISMATCH", "Original execution belongs to another stream"
        if isinstance(event, (ProductionEvent, ScrapEvent)):
            if event.payload.counter_epoch != execution["counter_epoch"]:
                return (
                    "COUNTER_EPOCH_MISMATCH",
                    "Production epoch differs from registered execution",
                )
            field = {
                "PRODUCTION_GOOD": "good_quantity",
                "PRODUCTION_REJECT": "reject_quantity",
                "WIP_SCRAP": "scrap_quantity",
            }[event.kind]
            totals = (
                connection.execute(
                    sa.select(execution_totals)
                    .where(
                        execution_totals.c.execution_id == event.execution_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one()
            )
            quantity = _exact(totals[field]) + event.payload.quantity
            if quantity > UINT64_MAX:
                return "QUANTITY_OVERFLOW", "Event would exceed the UInt64 production total"
            connection.execute(
                production_events.insert().values(
                    event_id=event.event_id,
                    execution_id=event.execution_id,
                    kind=event.kind,
                    quantity=event.payload.quantity,
                    counter_epoch=event.payload.counter_epoch,
                    occurred_at=event.occurred_at,
                    reason=event.payload.reason if isinstance(event, ScrapEvent) else None,
                )
            )
            connection.execute(
                execution_totals.update()
                .where(
                    execution_totals.c.execution_id == event.execution_id,
                )
                .values({field: quantity})
            )
        else:
            connection.execute(
                equipment_events.insert().values(
                    event_id=event.event_id,
                    execution_id=event.execution_id,
                    kind=event.kind,
                    body=event.model_dump(mode="json"),
                )
            )
        return None

    def _receive_event(
        self, connection: sa.Connection, event: Event, now: datetime
    ) -> EventReceipt:
        existing = (
            connection.execute(
                sa.select(event_inbox).where(
                    event_inbox.c.event_id == event.event_id,
                )
            )
            .mappings()
            .first()
        )
        if existing is not None:
            return self._receipt(existing)
        payload = event.model_dump(mode="json")
        # Insert the raw inbox first, then apply projections against this durable identity.
        connection.execute(
            event_inbox.insert().values(
                event_id=event.event_id,
                source_id=event.source_id,
                stream_id=event.stream_id,
                seq=event.seq,
                execution_id=event.execution_id,
                kind=event.kind,
                body=payload,
                content_sha256=_digest(payload),
                stored_at=now,
                applied_at=None,
                reason_code="APPLICATION_PENDING",
                reason="Application is provisional in this transaction",
            )
        )
        reason = self._apply_event(connection, event, now)
        self._set_application(connection, event.event_id, reason, now)
        return EventReceipt(
            event_id=event.event_id,
            seq=event.seq,
            disposition="QUARANTINED" if reason else "APPLIED",
            reason_code=reason[0] if reason else None,
            reason=reason[1] if reason else None,
        )

    @staticmethod
    def _set_application(connection, event_id, reason, now):
        connection.execute(
            event_inbox.update()
            .where(event_inbox.c.event_id == event_id)
            .values(
                applied_at=None if reason else now,
                reason_code=reason[0] if reason else None,
                reason=reason[1] if reason else None,
            )
        )
        if reason:
            connection.execute(
                event_apply_errors.insert().values(
                    event_id=event_id,
                    reason_code=reason[0],
                    reason=reason[1],
                    recorded_at=now,
                )
            )

    def _progress(self, connection, source_id, stream_id) -> StreamProgress:
        row = (
            connection.execute(
                sa.select(stream_progress).where(
                    _where(stream_progress, source_id, stream_id),
                )
            )
            .mappings()
            .one()
        )
        gaps = (
            connection.execute(
                sa.select(stream_gaps.c.body)
                .where(
                    _where(stream_gaps, source_id, stream_id),
                )
                .order_by(stream_gaps.c.from_seq)
            )
            .scalars()
            .all()
        )
        return StreamProgress(
            source_id=source_id,
            stream_id=stream_id,
            verified_through_seq=_exact(row["verified_through_seq"]),
            latest_ingested_seq=_exact(row["latest_ingested_seq"]),
            unresolved_gaps=tuple(StreamGap.model_validate(value) for value in gaps),
        )

    def _advance(self, connection, source_id, stream_id, *, latest, reports, now) -> StreamProgress:
        old = self._progress(connection, source_id, stream_id)
        latest = max(latest, old.latest_ingested_seq)
        rows = connection.execute(
            sa.select(event_inbox.c.seq, event_inbox.c.applied_at)
            .where(
                _where(event_inbox, source_id, stream_id),
            )
            .order_by(event_inbox.c.seq)
        ).all()
        verified = old.verified_through_seq
        cursor = 1
        missing = []
        for seq, applied_at in rows:
            seq = _exact(seq)
            if seq > cursor:
                missing.append((cursor, seq - 1))
            cursor = seq + 1
            if seq == verified + 1 and applied_at is not None:
                verified = seq
        if cursor <= latest:
            missing.append((cursor, latest))
        evidence = (*old.unresolved_gaps, *reports)
        # Split holes at evidence boundaries, preserving irrecoverable metadata where present.
        gaps = []
        for start, end in missing:
            boundaries = {start, end + 1}
            for report in evidence:
                if report.from_seq <= end and report.through_seq >= start:
                    boundaries.update(
                        (max(start, report.from_seq), min(end, report.through_seq) + 1)
                    )
            ordered = sorted(boundaries)
            for left, right in zip(ordered, ordered[1:], strict=False):
                covering = [
                    value for value in evidence if value.from_seq <= left <= value.through_seq
                ]
                report = next(
                    (value for value in covering if value.irrecoverable),
                    covering[0] if covering else None,
                )
                values = (
                    report.model_dump()
                    if report
                    else {
                        "irrecoverable": False,
                        "detected_at": now,
                        "reason_code": "MISSING_SOURCE_EVENTS",
                        "reason": "Source events not yet received",
                        "affected_execution_ids": (),
                    }
                )
                gaps.append(StreamGap(**{**values, "from_seq": left, "through_seq": right - 1}))
        if len(gaps) > 100:
            raise IngestConflict("Current missing intervals exceed the v1 progress bound of 100")
        progress = StreamProgress(
            source_id=source_id,
            stream_id=stream_id,
            verified_through_seq=verified,
            latest_ingested_seq=latest,
            unresolved_gaps=tuple(gaps),
        )
        connection.execute(
            stream_progress.update()
            .where(
                _where(stream_progress, source_id, stream_id),
            )
            .values(verified_through_seq=verified, latest_ingested_seq=latest, updated_at=now)
        )
        connection.execute(stream_gaps.delete().where(_where(stream_gaps, source_id, stream_id)))
        for gap in gaps:
            connection.execute(
                stream_gaps.insert().values(
                    source_id=source_id,
                    stream_id=stream_id,
                    from_seq=gap.from_seq,
                    through_seq=gap.through_seq,
                    body=gap.model_dump(mode="json"),
                )
            )
        return progress

    def ingest(
        self,
        batch: GatewayBatch,
        *,
        authenticated_gateway_id: str,
        authenticated_source_id: str,
        authenticated_stream_id: str,
        now: datetime,
    ) -> DurableAck:
        # Revalidate models too: model_construct/model_copy cannot bypass the wire boundary.
        batch = GatewayBatch.model_validate_json(batch.model_dump_json())
        now = _TIME.validate_python(now)
        identity = tuple(
            _ID.validate_python(value)
            for value in (
                authenticated_gateway_id,
                authenticated_source_id,
                authenticated_stream_id,
            )
        )
        body = batch.body
        if identity != (body.gateway_id, body.source_id, body.stream_id):
            raise SourceBindingError("Authenticated gateway/source scope does not match batch")
        conflict = None
        ack = None
        with self._transaction() as connection:
            self._advisory(connection, "gateway", body.gateway_id)
            source = self._lock_stream(connection, body.source_id, body.stream_id)
            if (source["gateway_id"], _exact(source["capture_generation"])) != (
                body.gateway_id,
                body.capture_generation,
            ):
                raise SourceBindingError("Gateway/capture generation is not explicitly registered")
            for event_id in sorted(event.event_id for event in body.events):
                self._advisory(connection, "event", event_id)
            collision = self._collision(connection, batch)
            if isinstance(collision, DurableAck):
                ack = collision
            elif collision is not None:
                code, incoming, original = collision
                conflict_id = connection.execute(
                    ingest_conflicts.insert()
                    .values(
                        source_id=body.source_id,
                        stream_id=body.stream_id,
                        gateway_id=body.gateway_id,
                        batch_id=body.batch_id,
                        reason_code=code,
                        incoming=incoming,
                        original=original,
                        recorded_at=now,
                    )
                    .returning(ingest_conflicts.c.conflict_id)
                ).scalar_one()
                connection.execute(
                    source_streams.update()
                    .where(
                        _scope(body.source_id, body.stream_id),
                    )
                    .values(conflicted=True)
                )
                if self.alarm_consumer is not None:
                    invalidate_collision = getattr(
                        self.alarm_consumer, "invalidate_authenticated_collision", None
                    )
                    if callable(invalidate_collision):
                        # A superseded runtime must still preserve authenticated
                        # collision evidence and invalidate the current durable roster.
                        invalidate_collision(
                            connection,
                            source=source,
                            reference_id=batch.content_sha256,
                            conflict_id=conflict_id,
                        )
                    else:
                        self.alarm_consumer.invalidate(
                            connection,
                            source=source,
                            reference_id=batch.content_sha256,
                            reason="SOURCE_CONFLICT",
                        )
                conflict = code
            elif source["conflicted"]:
                raise SourceConflicted("Source has a durable unresolved identity conflict")
            else:
                if any(gap.irrecoverable for gap in body.gaps):
                    connection.execute(
                        source_streams.update()
                        .where(
                            _scope(body.source_id, body.stream_id),
                        )
                        .values(reconciliation_required=True)
                    )
                receipts = tuple(
                    self._receive_event(connection, event, now) for event in body.events
                )
                latest = max(
                    (
                        0,
                        *(event.seq for event in body.events),
                        *(gap.through_seq for gap in body.gaps),
                    )
                )
                progress = self._advance(
                    connection,
                    body.source_id,
                    body.stream_id,
                    latest=latest,
                    reports=body.gaps,
                    now=now,
                )
                ack = DurableAck(
                    batch_id=body.batch_id,
                    gateway_id=body.gateway_id,
                    content_sha256=batch.content_sha256,
                    durability="COMMITTED",
                    committed_at=now,
                    events=receipts,
                    recorded_gaps=body.gaps,
                    progress=progress,
                )
                validate_ack(batch, ack)
                connection.execute(
                    ingest_batches.insert().values(
                        gateway_id=body.gateway_id,
                        batch_id=body.batch_id,
                        source_id=body.source_id,
                        stream_id=body.stream_id,
                        capture_generation=body.capture_generation,
                        capture_seq=body.capture_seq,
                        content_sha256=batch.content_sha256,
                        body=json.loads(canonical_batch_body(body)),
                        ack=ack.model_dump(mode="json"),
                        stored_at=now,
                    )
                )
                if self.alarm_consumer is not None:
                    self.alarm_consumer.consume(connection, body, source=source, now=now)
                self._before_commit(connection)
        if conflict is not None:
            raise IngestConflict(f"{conflict}; conflict evidence committed, no batch ACK issued")
        return ack

    def _before_commit(self, connection: sa.Connection) -> None:
        """Internal fault-injection seam; no transport exposes it."""

    def replay_quarantined(
        self,
        *,
        source_id: str,
        stream_id: str,
        now: datetime,
        limit: int = 1000,
    ) -> StreamProgress:
        source_id, stream_id = _ID.validate_python(source_id), _ID.validate_python(stream_id)
        now = _TIME.validate_python(now)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Quarantine replay limit must be an integer from 1 through 1000")
        with self._transaction() as connection:
            source = self._lock_stream(connection, source_id, stream_id)
            if source["conflicted"]:
                raise SourceConflicted("Source conflict requires explicit reconciliation")
            rows = (
                connection.execute(
                    sa.select(event_inbox)
                    .where(
                        _where(event_inbox, source_id, stream_id),
                        event_inbox.c.applied_at.is_(None),
                    )
                    .order_by(event_inbox.c.seq)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
            for row in rows:
                event = EVENT_ADAPTER.validate_python(row["body"])
                reason = self._apply_event(connection, event, now)
                self._set_application(connection, event.event_id, reason, now)
            progress = self._advance(
                connection,
                source_id,
                stream_id,
                latest=0,
                reports=(),
                now=now,
            )
            self._before_commit(connection)
        return progress

    def get_progress(self, source_id: str, stream_id: str) -> StreamProgress:
        with self._transaction() as connection:
            self._lock_stream(
                connection, _ID.validate_python(source_id), _ID.validate_python(stream_id)
            )
            return self._progress(connection, source_id, stream_id)

    def get_source_status(self, source_id: str, stream_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            row = self._lock_stream(
                connection, _ID.validate_python(source_id), _ID.validate_python(stream_id)
            )
            progress = self._progress(connection, source_id, stream_id)
            return {
                "source_id": source_id,
                "stream_id": stream_id,
                "conflicted": row["conflicted"],
                "reconciliation_required": row["reconciliation_required"],
                "control_restricted": row["conflicted"]
                or row["reconciliation_required"]
                or progress.verified_through_seq < progress.latest_ingested_seq,
            }

    def get_totals(self, execution_id: str) -> dict[str, int] | None:
        execution_id = _ID.validate_python(execution_id)
        with self._transaction() as connection:
            row = (
                connection.execute(
                    sa.select(execution_totals).where(
                        execution_totals.c.execution_id == execution_id,
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            return {
                field: _exact(row[field])
                for field in (
                    "good_quantity",
                    "reject_quantity",
                    "scrap_quantity",
                )
            }
