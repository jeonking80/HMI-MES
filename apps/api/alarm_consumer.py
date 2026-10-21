"""Opt-in same-transaction consumer of authenticated, original gateway captures.

Clock bounds are explicit deployment assumptions, never measured from delivery age.
No networking, service creation, commit, notification delivery or implicit config.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Annotated, Literal, NamedTuple, Self
from uuid import uuid4

from pydantic import Field, TypeAdapter, model_validator
from sqlalchemy import Connection, text

from apps.api.alarm_capture import (
    AlarmProcessingContext,
    CaptureIdentity,
    ClockAssessment,
    normalize_alarm_capture,
)
from apps.api.alarm_lifecycle import AlarmScope
from apps.api.alarm_qualification import Finite, RuleConfig
from apps.api.alarm_rules import StoredRuleConfig
from apps.api.alarm_store import (
    StoredAlarmResult,
    _channel,
    apply_observation,
    break_continuity,
)
from apps.api.database import SCHEMA_LOCK_ID, SUPPORTED_ALARM_CAPTURE_REVISIONS
from apps.api.ingestion import IngestConflict, IngestionUnavailable, SourceBindingError
from contracts.models import ContractModel, Identifier, Reason, UInt64, UTCTimestamp
from contracts.transport import BatchBody, batch_digest

_TIME = TypeAdapter(UTCTimestamp)
_ID = TypeAdapter(Identifier)
_CONFIG = TypeAdapter(RuleConfig)
_MAX = 2**64 - 1


class AlarmConsumerUnavailable(IngestionUnavailable):
    """Runtime startup/configuration/continuity cannot safely process a new capture."""


class AlarmCaptureConflict(IngestConflict):
    """An immutable original capture identity has different content."""


def _json(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class ConfiguredClockPolicy(ContractModel):
    """Uniform API-UTC minus gateway-UTC bounds, excluding the capture read bracket.

    The named shared-clock deployment guarantee is an explicit operator assumption.
    It is neither a network measurement nor verification of remote/absolute time.
    """

    policy_id: Identifier
    policy_version: Identifier
    reference_id: Identifier
    method: Literal["CONFIGURED_API_REFERENCE"]
    guarantee_kind: Literal["SAME_HOST_SHARED_KERNEL_UTC"]
    guarantee_basis: Reason
    gateway_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    capture_generation: UInt64
    clock_epoch: Identifier
    valid_from_api_utc: UTCTimestamp
    valid_until_api_utc: UTCTimestamp
    offset_lower_seconds: Finite
    offset_upper_seconds: Finite

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.offset_lower_seconds > self.offset_upper_seconds:
            raise ValueError("Ordered explicit clock bounds required")
        if self.valid_until_api_utc <= self.valid_from_api_utc:
            raise ValueError("Clock guarantee requires a nonempty validity interval")
        return self


class AlarmConsumerConfig(ContractModel):
    scope: AlarmScope
    gateway_id: Identifier
    capture_generation: UInt64
    rules: Annotated[tuple[RuleConfig, ...], Field(min_length=1, max_length=2)]
    clock_policy: ConfiguredClockPolicy | None = None

    @model_validator(mode="after")
    def bindings(self) -> Self:
        if self.scope.asset_id != "Sealer":
            raise ValueError("Only the Sealer analog consumer is implemented")
        if len({config.rule.rule_id for config in self.rules}) != len(self.rules):
            raise ValueError("Each enabled rule identity must be unique")
        if len({config.kind for config in self.rules}) != len(self.rules):
            raise ValueError("Enable at most one rule of each analog kind")
        policy = self.clock_policy
        if policy is not None and (
            policy.gateway_id != self.gateway_id
            or policy.source_id != self.scope.source_id
            or policy.stream_id != self.scope.stream_id
            or policy.capture_generation != self.capture_generation
        ):
            raise ValueError("Clock policy must bind the explicitly configured source")
        return self


class CaptureDecision(ContractModel):
    channel_id: Identifier
    batch_id: Identifier
    assigned_position: UInt64
    consumer_epoch: Identifier
    rule_version: Identifier | None = None
    disposition: Literal["LIVE", "HISTORY_ONLY", "UNKNOWN", "UNSUPPORTED", "QUARANTINED"]
    reason: Identifier
    context: AlarmProcessingContext | None = None
    result: StoredAlarmResult
    replayed: bool = False


class CapturePlan(NamedTuple):
    selected: StoredRuleConfig | None
    assessment: ClockAssessment | None
    historical: bool
    disposition: str
    reason: str
    break_required: bool


def _ready(db):
    if db.dialect.name != "postgresql" or not db.in_transaction():
        raise AlarmConsumerUnavailable("Caller-owned PostgreSQL transaction required")
    db.execute(text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": SCHEMA_LOCK_ID})
    revisions = db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(revisions) != 1 or revisions[0] not in SUPPORTED_ALARM_CAPTURE_REVISIONS:
        raise AlarmConsumerUnavailable("Capture-context migration required")


def _lock_channel(db, channel):
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
        {"key": "alarm-channel:" + channel},
    )


def _frontier(db, channel):
    return (
        db.execute(
            text("SELECT * FROM alarm_capture_frontiers WHERE channel_id=:c FOR UPDATE"),
            {"c": channel},
        )
        .mappings()
        .first()
    )


def _allocate(db, frontier):
    value = int(frontier["allocator_position"]) + 1
    if value > _MAX:
        raise AlarmConsumerUnavailable("Durable alarm processing position exhausted")
    db.execute(
        text("UPDATE alarm_capture_frontiers SET allocator_position=:p WHERE channel_id=:c"),
        {"p": value, "c": frontier["channel_id"]},
    )
    return value


def assess_capture_clock(batch: BatchBody, policy: ConfiguredClockPolicy | None, now: datetime):
    """Return assessment, conservative API interval and reason; never infer an offset."""
    if policy is None:
        return None, None, "NO_CLOCK_POLICY"
    capture = batch.capture_time
    if capture is None:
        return None, None, "NO_CAPTURE_CLOCK"
    if (
        policy.gateway_id,
        policy.source_id,
        policy.stream_id,
        policy.capture_generation,
        policy.clock_epoch,
    ) != (
        batch.gateway_id,
        batch.source_id,
        batch.stream_id,
        batch.capture_generation,
        capture.clock_epoch,
    ):
        return None, None, "CLOCK_BINDING_MISMATCH"
    if not policy.valid_from_api_utc <= now < policy.valid_until_api_utc:
        return None, None, "CLOCK_POLICY_EXPIRED"
    span = capture.utc_read_span_seconds
    lower, upper = policy.offset_lower_seconds - span, policy.offset_upper_seconds + span
    if lower < -2.0 or upper > 2.0:
        return None, None, "CLOCK_BOUND_UNKNOWN"
    start = batch.captured_at + timedelta(microseconds=math.floor(lower * 1_000_000))
    end = batch.captured_at + timedelta(microseconds=math.ceil(upper * 1_000_000))
    if start < policy.valid_from_api_utc or end >= policy.valid_until_api_utc:
        return None, None, "CAPTURE_OUTSIDE_CLOCK_VALIDITY"
    return (
        ClockAssessment(
            method=policy.method,
            reference_id=policy.reference_id,
            gateway_id=batch.gateway_id,
            clock_epoch=capture.clock_epoch,
            batch_sha256=batch_digest(batch),
            offset_lower_seconds=lower,
            offset_upper_seconds=upper,
        ),
        (start, end),
        "CONFIGURED_REFERENCE",
    )


class AlarmConsumer:
    def __init__(self, config: AlarmConsumerConfig, *, consumer_epoch: str | None = None):
        self.config = AlarmConsumerConfig.model_validate_json(config.model_dump_json())
        self.consumer_epoch = _ID.validate_python(consumer_epoch or str(uuid4()))
        self.config_sha256 = _digest(self.config)
        self.channels = tuple(
            sorted(
                (
                    (_channel(self.config.scope, config.rule.rule_id), config)
                    for config in self.config.rules
                ),
                key=lambda pair: pair[0],
            )
        )

    def _source(self, db):
        row = (
            db.execute(
                text("SELECT * FROM source_streams WHERE source_id=:s AND stream_id=:t"),
                {"s": self.config.scope.source_id, "t": self.config.scope.stream_id},
            )
            .mappings()
            .first()
        )
        if row is None or (row["line_id"], row["gateway_id"], int(row["capture_generation"])) != (
            self.config.scope.line_id,
            self.config.gateway_id,
            self.config.capture_generation,
        ):
            raise AlarmConsumerUnavailable("Configured source/gateway/generation binding differs")
        return row

    def _installed(self, db, channel, configured):
        row = (
            db.execute(
                text(
                    "SELECT c.current_version,v.config FROM alarm_channels c "
                    "JOIN alarm_rule_versions v ON v.channel_id=c.channel_id "
                    "AND v.version=c.current_version WHERE c.channel_id=:c"
                ),
                {"c": channel},
            )
            .mappings()
            .first()
        )
        if row is None or self._decode_rule_config(row["config"]) != configured:
            raise AlarmConsumerUnavailable(
                "Explicit runtime config differs from the installed rule head"
            )

    @staticmethod
    def _decode_rule_config(value):
        return _CONFIG.validate_python(value)

    def _startup_fence(self, connection):
        return int(
            connection.execute(
                text(
                    "SELECT COALESCE(max(capture_seq),0) FROM ingest_batches WHERE gateway_id=:g "
                    "AND source_id=:s AND stream_id=:t AND capture_generation=:generation"
                ),
                {
                    "g": self.config.gateway_id,
                    "s": self.config.scope.source_id,
                    "t": self.config.scope.stream_id,
                    "generation": self.config.capture_generation,
                },
            ).scalar_one()
        )

    def start(self, connection: Connection, *, now: datetime) -> None:
        """Source-first startup fence and durable restart break; caller commits before serving."""
        now = _TIME.validate_python(now)
        _ready(connection)
        # Exactly match ingestion's gateway -> line/source -> channel lock order.
        from apps.api.ingestion import IngestionService

        IngestionService._advisory(connection, "gateway", self.config.gateway_id)
        IngestionService._lock_stream(
            connection, self.config.scope.source_id, self.config.scope.stream_id
        )
        source = self._source(connection)
        if source["conflicted"] or source["reconciliation_required"]:
            raise AlarmConsumerUnavailable(
                "Unresolved source requires explicit reconciliation before startup"
            )
        fence = self._startup_fence(connection)
        # Acquire the whole channel set before any store operation takes a global
        # input lock. Otherwise a cross-channel user input-ID collision could invert
        # a later channel lock against the first channel's already-held input lock.
        for channel, _config in self.channels:
            _lock_channel(connection, channel)
        for channel, config in self.channels:
            self._installed(connection, channel, config)
            old = _frontier(connection, channel)
            if old and old["consumer_epoch"] == self.consumer_epoch:
                if old["config_sha256"] != self.config_sha256 or old["requires_restart"]:
                    raise AlarmConsumerUnavailable(
                        "Consumer epoch cannot be reused after config or safety change"
                    )
                continue
            startup_id = "alarm-start:" + _digest([channel, self.consumer_epoch])
            if connection.execute(
                text("SELECT 1 FROM alarm_receipts WHERE input_id=:id"),
                {"id": startup_id},
            ).first():
                raise AlarmConsumerUnavailable("A retired consumer epoch cannot take over again")
            position = int(
                connection.execute(
                    text(
                        "SELECT COALESCE(last_position,0) FROM alarm_channels WHERE channel_id=:c"
                    ),
                    {"c": channel},
                ).scalar_one()
            )
            if old:
                position = max(position, int(old["allocator_position"]))
            connection.execute(
                text(
                    "INSERT INTO alarm_capture_frontiers VALUES "
                    "(:c,:s,:t,:g,:generation,:p,:fence,:fence,:epoch,:config,false) "
                    "ON CONFLICT(channel_id) DO UPDATE SET "
                    "allocator_position=EXCLUDED.allocator_position,"
                    "highest_capture_seq=GREATEST("
                    "alarm_capture_frontiers.highest_capture_seq,:fence),"
                    "activation_fence_seq=:fence,consumer_epoch=:epoch,"
                    "config_sha256=:config,requires_restart=false"
                ),
                {
                    "c": channel,
                    "s": self.config.scope.source_id,
                    "t": self.config.scope.stream_id,
                    "g": self.config.gateway_id,
                    "generation": self.config.capture_generation,
                    "p": position,
                    "fence": fence,
                    "epoch": self.consumer_epoch,
                    "config": self.config_sha256,
                },
            )
            frontier = _frontier(connection, channel)
            position = _allocate(connection, frontier)
            break_continuity(
                connection,
                self.config.scope,
                config.rule.rule_id,
                input_id="alarm-start:" + _digest([channel, self.consumer_epoch]),
                position=position,
                reason="RESTART",
                evidence_reference=self.consumer_epoch,
            )

    def invalidate(
        self,
        connection: Connection,
        *,
        source: Mapping,
        reference_id: str,
        reason: Literal["SOURCE_CONFLICT", "SOURCE_RECONCILIATION"],
    ) -> None:
        """Reject continuity across source safety changes, including rejected raw collisions."""
        _ready(connection)
        reference_id = _ID.validate_python(reference_id)
        if reason not in {"SOURCE_CONFLICT", "SOURCE_RECONCILIATION"}:
            raise ValueError("Explicit source safety reason required")
        self._source(connection)
        # Acquire the whole channel set before any store operation takes a global
        # input lock. Otherwise a cross-channel user input-ID collision could invert
        # a later channel lock against the first channel's already-held input lock.
        for channel, _config in self.channels:
            _lock_channel(connection, channel)
        for channel, config in self.channels:
            frontier = _frontier(connection, channel)
            if frontier is None or frontier["consumer_epoch"] != self.consumer_epoch:
                raise AlarmConsumerUnavailable("Active consumer startup epoch required")
            if frontier["requires_restart"]:
                continue
            position = _allocate(connection, frontier)
            break_continuity(
                connection,
                self.config.scope,
                config.rule.rule_id,
                input_id="alarm-invalid:"
                + _digest([channel, self.consumer_epoch, reason, reference_id]),
                position=position,
                reason="GAP",
                evidence_reference=reference_id,
            )
            connection.execute(
                text(
                    "UPDATE alarm_capture_frontiers SET requires_restart=true WHERE channel_id=:c"
                ),
                {"c": channel},
            )

    def consume(
        self, connection: Connection, batch: BatchBody, *, source: Mapping, now: datetime
    ) -> tuple[CaptureDecision, ...]:
        batch = BatchBody.model_validate_json(batch.model_dump_json())
        now = _TIME.validate_python(now)
        _ready(connection)
        current_source = self._source(connection)
        if (batch.gateway_id, batch.source_id, batch.stream_id, batch.capture_generation) != (
            self.config.gateway_id,
            self.config.scope.source_id,
            self.config.scope.stream_id,
            self.config.capture_generation,
        ):
            raise SourceBindingError("Original capture differs from configured consumer scope")
        results = []
        # Acquire the whole channel set before any store operation takes a global
        # input lock. Otherwise a cross-channel user input-ID collision could invert
        # a later channel lock against the first channel's already-held input lock.
        for channel, _config in self.channels:
            _lock_channel(connection, channel)
        for channel, config in self.channels:
            old = (
                connection.execute(
                    text(
                        "SELECT content_sha256,receipt FROM alarm_capture_decisions "
                        "WHERE channel_id=:c AND gateway_id=:g AND batch_id=:b"
                    ),
                    {"c": channel, "g": batch.gateway_id, "b": batch.batch_id},
                )
                .mappings()
                .first()
            )
            if old:
                if old["content_sha256"] != batch_digest(batch):
                    raise AlarmCaptureConflict(
                        "Capture decision identity has different immutable content"
                    )
                decision = CaptureDecision.model_validate(old["receipt"])
                results.append(
                    decision.model_copy(
                        update={
                            "replayed": True,
                            "result": decision.result.model_copy(
                                update={"replayed": True, "notify_current": False}
                            ),
                        }
                    )
                )
                continue
            frontier = _frontier(connection, channel)
            if (
                frontier is None
                or frontier["consumer_epoch"] != self.consumer_epoch
                or frontier["config_sha256"] != self.config_sha256
            ):
                raise AlarmConsumerUnavailable(
                    "Explicit matching active consumer startup epoch required"
                )
            self._installed(connection, channel, config)
            position = _allocate(connection, frontier)
            history = batch.capture_seq <= max(
                int(frontier["highest_capture_seq"]), int(frontier["activation_fence_seq"])
            )
            raw_gap = batch.capture_seq > int(frontier["highest_capture_seq"]) + 1
            unsafe = (
                current_source["conflicted"]
                or current_source["reconciliation_required"]
                or bool(batch.gaps)
                or frontier["requires_restart"]
            )
            decision = self._decide(
                connection, channel, config, batch, position, history, unsafe, raw_gap, now
            )
            connection.execute(
                text(
                    "UPDATE alarm_capture_frontiers SET "
                    "highest_capture_seq=GREATEST(highest_capture_seq,:seq),"
                    "requires_restart=requires_restart OR :unsafe WHERE channel_id=:c"
                ),
                {"c": channel, "seq": batch.capture_seq, "unsafe": unsafe},
            )
            self._save(connection, channel, batch, decision)
            results.append(decision)
        return tuple(results)

    def _break_decision(self, db, channel, config, batch, position, history, disposition, reason):
        result = break_continuity(
            db,
            self.config.scope,
            config.rule.rule_id,
            input_id="alarm-capture-break:"
            + _digest([channel, CaptureIdentity.from_batch(batch).model_dump(mode="json")]),
            position=position,
            reason="UNSUPPORTED_CAPTURE" if disposition == "UNSUPPORTED" else "GAP",
            evidence_reference=batch.batch_id,
            historical=history,
        )
        return CaptureDecision(
            channel_id=channel,
            batch_id=batch.batch_id,
            assigned_position=position,
            consumer_epoch=self.consumer_epoch,
            disposition=disposition,
            reason=reason,
            result=result,
        )

    def _version_for_interval(self, db, channel, interval):
        start, end = interval
        rows = (
            db.execute(
                text(
                    "SELECT version,config,changed_at FROM alarm_rule_versions "
                    "WHERE channel_id=:c AND changed_at<=:at "
                    "ORDER BY changed_at DESC LIMIT 1"
                ),
                {"c": channel, "at": start},
            )
            .mappings()
            .first()
        )
        if rows is None:
            return None
        ties = db.execute(
            text("SELECT count(*) FROM alarm_rule_versions WHERE channel_id=:c AND changed_at=:at"),
            {"c": channel, "at": rows["changed_at"]},
        ).scalar_one()
        if ties != 1:
            return None
        boundary = db.execute(
            text(
                "SELECT min(changed_at) FROM alarm_rule_versions "
                "WHERE channel_id=:c AND changed_at>:at"
            ),
            {"c": channel, "at": rows["changed_at"]},
        ).scalar_one()
        if boundary is not None and end >= boundary:
            return None
        return self._decode_rule_config(rows["config"])

    def _classify(self, db, channel, config, batch, history, unsafe, raw_gap, now):
        """Shared conservative admission; no checkpoint/store mutation or allocation."""

        def stop(disposition, reason, *, selected=None, assessment=None):
            return CapturePlan(selected, assessment, history, disposition, reason, True)

        if unsafe:
            return stop("QUARANTINED", "SOURCE_RECONCILIATION_REQUIRED")
        if batch.schema_version != "1.2" or batch.capture_time is None or not batch.samples:
            return stop("UNSUPPORTED", "UNSUPPORTED_CAPTURE")
        assessment, interval, clock_reason = assess_capture_clock(
            batch, self.config.clock_policy, now
        )
        selected = config
        disposition, reason = "LIVE", "FRESH_CAPTURE"
        if interval is not None:
            age_low = (now - interval[1]).total_seconds()
            age_high = (now - interval[0]).total_seconds()
            if age_low >= 3.0:
                history = True
                reason = "DELAYED_CAPTURE"
            elif age_low < 0.0 or age_high >= 3.0:
                if not getattr(self, "preserve_clock_evidence", False):
                    assessment = None
                disposition, reason = "UNKNOWN", "AMBIGUOUS_CAPTURE_FRESHNESS"
            selected = self._version_for_interval(db, channel, interval)
            if selected is None:
                return stop(
                    "QUARANTINED",
                    "NO_UNAMBIGUOUS_INSTALLED_CONFIG",
                    assessment=assessment
                    if getattr(self, "preserve_clock_evidence", False)
                    else None,
                )
            if selected.rule.version != config.rule.version:
                history = True
                reason = "INSTALLED_OLD_CONFIG"
        else:
            disposition, reason = "UNKNOWN", clock_reason
            if history:
                return stop("QUARANTINED", "HISTORICAL_CONFIG_UNKNOWN")
        if history:
            disposition = "HISTORY_ONLY"
            reason = reason if reason != "FRESH_CAPTURE" else "OLDER_CAPTURE_POSITION"
        if not history and raw_gap:
            return stop("UNKNOWN", "CAPTURE_SEQUENCE_GAP", selected=selected, assessment=assessment)
        if not history and (batch.communication is None or batch.communication.state != "GOOD"):
            return stop(
                "UNKNOWN", "COMMUNICATION_NOT_GOOD", selected=selected, assessment=assessment
            )
        return CapturePlan(selected, assessment, history, disposition, reason, False)

    def _decide(self, db, channel, config, batch, position, history, unsafe, raw_gap, now):
        plan = self._classify(db, channel, config, batch, history, unsafe, raw_gap, now)
        if plan.break_required:
            return self._break_decision(
                db, channel, config, batch, position, plan.historical, plan.disposition, plan.reason
            )
        context = AlarmProcessingContext(
            capture=CaptureIdentity.from_batch(batch),
            batch_sha256=batch_digest(batch),
            scope=self.config.scope,
            rule=plan.selected.rule,
            position=position,
            classification="HISTORICAL" if plan.historical else "LIVE",
            clock_assessment=plan.assessment,
        )
        normalized = normalize_alarm_capture(batch, plan.selected, context)
        result = apply_observation(
            db,
            config.rule.rule_id,
            normalized.observation,
            new_alarm_id="alarm:" + _digest([channel, normalized.observation.observation_id]),
        )
        return CaptureDecision(
            channel_id=channel,
            batch_id=batch.batch_id,
            assigned_position=position,
            consumer_epoch=self.consumer_epoch,
            rule_version=plan.selected.rule.version,
            disposition=plan.disposition,
            reason=plan.reason,
            context=context,
            result=result,
        )

    def _save(self, db, channel, batch, decision):
        db.execute(
            text(
                "INSERT INTO alarm_capture_decisions VALUES "
                "(:c,:g,:b,:s,:t,:generation,:seq,:hash,:position,:epoch,:version,"
                ":disposition,:reason,CAST(:context AS jsonb),CAST(:policy AS jsonb),"
                "CAST(:receipt AS jsonb))"
            ),
            {
                "c": channel,
                "g": batch.gateway_id,
                "b": batch.batch_id,
                "s": batch.source_id,
                "t": batch.stream_id,
                "generation": batch.capture_generation,
                "seq": batch.capture_seq,
                "hash": batch_digest(batch),
                "position": decision.assigned_position,
                "epoch": decision.consumer_epoch,
                "version": decision.rule_version,
                "disposition": decision.disposition,
                "reason": decision.reason,
                "context": decision.context.model_dump_json() if decision.context else "null",
                "policy": self.config.clock_policy.model_dump_json()
                if self.config.clock_policy
                else "null",
                "receipt": decision.model_dump_json(),
            },
        )
