"""Explicit alarm rule provisioning and opt-in consumer startup for App A only."""

import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator
from sqlalchemy import text

from apps.api.alarm_consumer import AlarmConsumerConfig
from apps.api.alarm_coordinator import AlarmCoordinator, MixedAlarmConsumerConfig
from apps.api.alarm_lifecycle import AlarmConflict, AlarmScope, ChangeAlarmRule
from apps.api.alarm_qualification import RuleConfig
from apps.api.alarm_rules import StoredRuleConfig, require_rule_target
from apps.api.alarm_store import _channel, _ready, install_rule, replace_rule
from apps.api.auth import UserAuth
from apps.api.database import SUPPORTED_MIXED_ALARM_REVISIONS
from apps.api.ingestion import IngestionService, SourceBindingError
from contracts.models import ContractModel, Identifier, UInt64, UTCTimestamp


class RuleOperation(ContractModel):
    operation_id: Identifier
    expected_version: Identifier | None  # Explicit null installs; a version replaces.
    config: RuleConfig


class RuleProvisioningConfig(ContractModel):
    scope: AlarmScope
    gateway_id: Identifier
    capture_generation: UInt64
    operations: Annotated[tuple[RuleOperation, ...], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def distinct(self) -> Self:
        if self.scope.asset_id != "Sealer":
            raise ValueError("Only Sealer analog provisioning is supported")
        for values in (
            [op.operation_id for op in self.operations],
            [op.config.rule.rule_id for op in self.operations],
            [op.config.kind for op in self.operations],
        ):
            if len(set(values)) != len(values):
                raise ValueError("Distinct operation, rule and analog kind identities required")
        return self


class MixedRuleOperation(ContractModel):
    asset_id: Literal["Line", "Sealer"]
    operation_id: Identifier
    expected_version: Identifier | None
    config: StoredRuleConfig


class MixedRuleProvisioningConfig(ContractModel):
    kind: Literal["MIXED_ALARM_RULES"]
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    gateway_id: Identifier
    capture_generation: UInt64
    operations: Annotated[tuple[MixedRuleOperation, ...], Field(min_length=1, max_length=3)]

    @property
    def scope(self) -> AlarmScope:
        return AlarmScope(
            line_id=self.line_id,
            source_id=self.source_id,
            stream_id=self.stream_id,
            asset_id="Line",
        )

    @model_validator(mode="after")
    def distinct(self) -> Self:
        for operation in self.operations:
            require_rule_target(_operation_scope(self, operation), operation.config)
        identities = (
            [op.operation_id for op in self.operations],
            [(op.asset_id, op.config.rule.rule_id) for op in self.operations],
            [op.config.kind for op in self.operations],
        )
        if any(len(set(values)) != len(values) for values in identities):
            raise ValueError("Distinct operation, channel and kind identities required")
        return self


def _operation_scope(config, operation):
    return (
        config.scope.model_copy(update={"asset_id": operation.asset_id})
        if isinstance(config, MixedRuleProvisioningConfig)
        else config.scope
    )


ALARM_RULES_FILE = TypeAdapter(RuleProvisioningConfig | MixedRuleProvisioningConfig)
ALARM_RUNTIME_FILE = TypeAdapter(AlarmConsumerConfig | MixedAlarmConsumerConfig)


def read_config_file(path: Path, model):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 65_536:
            raise ValueError("Bounded regular configuration file required")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(65_537)
        if len(raw) > 65_536:
            raise ValueError("Configuration exceeds bounds")
        return (
            model.validate_json(raw)
            if isinstance(model, TypeAdapter)
            else model.model_validate_json(raw)
        )
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _source_lock(db, config):
    _ready(db)
    IngestionService._advisory(db, "gateway", config.gateway_id)
    IngestionService._lock_stream(db, config.scope.source_id, config.scope.stream_id)
    row = (
        db.execute(
            text("SELECT * FROM source_streams WHERE source_id=:s AND stream_id=:t"),
            {"s": config.scope.source_id, "t": config.scope.stream_id},
        )
        .mappings()
        .first()
    )
    if row is None or (row["line_id"], row["gateway_id"], int(row["capture_generation"])) != (
        config.scope.line_id,
        config.gateway_id,
        config.capture_generation,
    ):
        raise SourceBindingError("Alarm configuration source binding differs")
    if row["conflicted"] or row["reconciliation_required"]:
        raise SourceBindingError("Alarm source requires explicit reconciliation")


def _rule_intent(config, operation, actor, at):
    if operation.expected_version is None:
        return {
            "kind": "INSTALL",
            "scope": _operation_scope(config, operation).model_dump(mode="json"),
            "config": operation.config.model_dump(mode="json"),
            "actor": actor,
            "at": at.isoformat(),
        }
    event = ChangeAlarmRule(
        event_id=operation.operation_id,
        scope=_operation_scope(config, operation),
        expected_version=operation.expected_version,
        new_rule=operation.config.rule,
        actor=actor,
        at=at,
    )
    return {
        "event": event.model_dump(mode="json"),
        "config": operation.config.model_dump(mode="json"),
    }


def configure_alarm_rules(
    auth: UserAuth,
    *,
    login: str,
    password: str,
    origin: str,
    config: RuleProvisioningConfig | MixedRuleProvisioningConfig,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Authenticate a real temporary session; one atomic file operation, no runtime activation."""
    config = ALARM_RULES_FILE.validate_json(config.model_dump_json())
    session = None
    try:
        challenge = auth.begin_login()
        session = auth.login(
            login,
            password,
            challenge_token=challenge.token,
            csrf_token=challenge.csrf_token,
            origin=origin,
        )
        with auth.authorized_transaction(
            session.token,
            permission="equipment_manage",
            csrf_token=session.csrf_token,
            origin=origin,
        ) as tx:
            db, actor = tx.connection, tx.principal.login
            _source_lock(db, config)
            if isinstance(config, MixedRuleProvisioningConfig):
                heads = db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
                if len(heads) != 1 or heads[0] not in SUPPORTED_MIXED_ALARM_REVISIONS:
                    raise AlarmConflict("Explicit mixed alarm migration required")
            # Source-wide durable roster plus every requested channel first; only
            # then acquire globally sorted input IDs, before looking up receipts.
            channels = set(
                db.execute(
                    text(
                        "SELECT channel_id FROM alarm_capture_frontiers "
                        "WHERE source_id=:s AND stream_id=:t"
                    ),
                    {"s": config.scope.source_id, "t": config.scope.stream_id},
                ).scalars()
            )
            channels.update(
                _channel(_operation_scope(config, op), op.config.rule.rule_id)
                for op in config.operations
            )
            keys = ["alarm-channel:" + channel for channel in sorted(channels)]
            keys.extend(
                "alarm-input:" + value
                for value in sorted(op.operation_id for op in config.operations)
            )
            for key in keys:
                db.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key}
                )
            for operation in config.operations:
                old = (
                    db.execute(
                        text("SELECT channel_id,intent FROM alarm_receipts WHERE input_id=:id"),
                        {"id": operation.operation_id},
                    )
                    .mappings()
                    .first()
                )
                if old is not None:
                    try:
                        raw = old["intent"]
                        at = TypeAdapter(UTCTimestamp).validate_python(
                            raw["at"] if operation.expected_version is None else raw["event"]["at"]
                        )
                    except (KeyError, TypeError, ValueError):
                        raise AlarmConflict("Operation identity belongs to another input") from None
                    if old["channel_id"] != _channel(
                        _operation_scope(config, operation), operation.config.rule.rule_id
                    ) or (raw != _rule_intent(config, operation, actor, at)):
                        raise AlarmConflict("Operation identity belongs to another intent")
                else:
                    at = TypeAdapter(UTCTimestamp).validate_python(clock())
                extra = (
                    {}
                    if operation.expected_version is None
                    else {"expected_version": operation.expected_version}
                )
                action = install_rule if operation.expected_version is None else replace_rule
                action(
                    db,
                    _operation_scope(config, operation),
                    operation.config,
                    input_id=operation.operation_id,
                    principal=tx.principal,
                    at=at,
                    **extra,
                )
    finally:
        if session is not None:
            auth.revoke(session.token, csrf_token=session.csrf_token, origin=origin)


def start_alarm_runtime(
    engine, config: AlarmConsumerConfig | MixedAlarmConsumerConfig, *, args
) -> AlarmCoordinator:
    if (
        config.gateway_id,
        config.scope.source_id,
        config.scope.stream_id,
        config.capture_generation,
    ) != (args.gateway_id, args.source_id, args.stream_id, args.capture_generation):
        raise SourceBindingError("Alarm runtime differs from service binding")
    consumer = AlarmCoordinator(config)
    with engine.begin() as db:
        consumer.start(db, now=datetime.now(UTC))
    return consumer
