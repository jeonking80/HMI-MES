"""Source-wide mixed alarm ownership; JSON config never supplies processing proof."""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import Connection, text

from apps.api.alarm_capture import AlarmProcessingContext, CaptureIdentity
from apps.api.alarm_consumer import (
    AlarmCaptureConflict,
    AlarmConsumer,
    AlarmConsumerConfig,
    AlarmConsumerUnavailable,
    CaptureDecision,
    ConfiguredClockPolicy,
    _allocate,
    _digest,
    _frontier,
    _lock_channel,
    _ready,
)
from apps.api.alarm_lifecycle import AlarmScope
from apps.api.alarm_rules import PLCFaultRuleConfig, StoredRuleConfig, require_rule_target
from apps.api.alarm_store import _channel, apply_fault_capture, break_continuity
from apps.api.database import SUPPORTED_MIXED_ALARM_REVISIONS
from apps.api.ingestion import IngestionService, SourceBindingError
from contracts.models import ContractModel, Identifier, UInt64, UTCTimestamp
from contracts.transport import BatchBody, batch_digest

_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(UTCTimestamp)


class MixedAlarmChannel(ContractModel):
    asset_id: Literal["Line", "Sealer"]
    config: StoredRuleConfig

    @model_validator(mode="after")
    def target(self) -> Self:
        target = "Line" if isinstance(self.config, PLCFaultRuleConfig) else "Sealer"
        if self.asset_id != target:
            raise ValueError("Rule kind requires its exact logical/physical target")
        return self


class MixedAlarmConsumerConfig(ContractModel):
    kind: Literal["MIXED_ALARM_RUNTIME"]
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier
    gateway_id: Identifier
    capture_generation: UInt64
    channels: Annotated[tuple[MixedAlarmChannel, ...], Field(min_length=1, max_length=3)]
    clock_policy: ConfiguredClockPolicy | None = None

    @property
    def scope(self) -> AlarmScope:
        """Common source binding only; individual processing scopes are channel-specific."""
        return AlarmScope(
            line_id=self.line_id,
            source_id=self.source_id,
            stream_id=self.stream_id,
            asset_id="Line",
        )

    @model_validator(mode="after")
    def bindings(self) -> Self:
        if len({(c.asset_id, c.config.rule.rule_id) for c in self.channels}) != len(self.channels):
            raise ValueError("Each channel identity must be unique")
        if len({c.config.kind for c in self.channels}) != len(self.channels):
            raise ValueError("Enable at most one channel of each rule kind")
        policy = self.clock_policy
        if policy is not None and (
            policy.gateway_id,
            policy.source_id,
            policy.stream_id,
            policy.capture_generation,
        ) != (self.gateway_id, self.source_id, self.stream_id, self.capture_generation):
            raise ValueError("Clock policy must bind the exact source")
        return self


class _AnalogDelegate(AlarmConsumer):
    def _startup_fence(self, connection):
        return self.shared_fence


class _LineConfig(ContractModel):
    scope: AlarmScope
    gateway_id: Identifier
    capture_generation: UInt64
    rules: tuple[PLCFaultRuleConfig]
    clock_policy: ConfiguredClockPolicy | None


class _LineDelegate(_AnalogDelegate):
    preserve_clock_evidence = True

    def __init__(self, config, *, consumer_epoch):
        self.config = config
        self.consumer_epoch = consumer_epoch
        self.channels = ((_channel(config.scope, config.rules[0].rule.rule_id), config.rules[0]),)

    @staticmethod
    def _decode_rule_config(value):
        return PLCFaultRuleConfig.model_validate(value)

    def _decide(self, db, channel, config, batch, position, history, unsafe, raw_gap, now):
        plan = self._classify(db, channel, config, batch, history, unsafe, raw_gap, now)
        # Unsupported originals cannot manufacture an EventSeq or proof. Unsafe
        # source identity cannot enter the trusted fault store at all.
        source = self._source(db)
        if (
            plan.disposition == "UNSUPPORTED"
            or source["conflicted"]
            or source["reconciliation_required"]
        ):
            return self._break_decision(
                db, channel, config, batch, position, plan.historical, plan.disposition, plan.reason
            )
        selected = plan.selected or config
        forced = None
        if plan.reason in {"NO_UNAMBIGUOUS_INSTALLED_CONFIG", "HISTORICAL_CONFIG_UNKNOWN"}:
            forced = "CONFIG_INTERVAL_UNKNOWN"
        elif plan.reason in {"CAPTURE_SEQUENCE_GAP", "SOURCE_RECONCILIATION_REQUIRED"}:
            forced = "CAPTURE_SEQUENCE_GAP"
        elif plan.reason == "AMBIGUOUS_CAPTURE_FRESHNESS":
            forced = "CAPTURE_FRESHNESS_UNKNOWN"
        context = AlarmProcessingContext(
            capture=CaptureIdentity.from_batch(batch),
            batch_sha256=batch_digest(batch),
            scope=self.config.scope,
            rule=selected.rule,
            position=position,
            classification="HISTORICAL" if plan.historical else "LIVE",
            clock_assessment=plan.assessment,
        )
        result = apply_fault_capture(
            db,
            batch,
            context,
            new_alarm_id="alarm:" + _digest([channel, context.capture.model_dump(mode="json")]),
            forced_unknown_reason=forced,
        )
        return CaptureDecision(
            channel_id=channel,
            batch_id=batch.batch_id,
            assigned_position=position,
            consumer_epoch=self.consumer_epoch,
            # CONFIG_INTERVAL uses current installed config as processing owner,
            # never as proof that its version governed the original interval.
            rule_version=None if forced == "CONFIG_INTERVAL_UNKNOWN" else selected.rule.version,
            disposition=plan.disposition,
            reason=plan.reason,
            context=context,
            result=result,
        )


class AlarmCoordinator:
    def __init__(
        self, config: AlarmConsumerConfig | MixedAlarmConsumerConfig, *, consumer_epoch=None
    ):
        model = (
            AlarmConsumerConfig
            if isinstance(config, AlarmConsumerConfig)
            else MixedAlarmConsumerConfig
        )
        self.config = model.model_validate_json(config.model_dump_json())
        self.consumer_epoch = _ID.validate_python(consumer_epoch or str(uuid4()))
        self.config_sha256 = _digest(self.config)
        self._delegates = []
        if isinstance(self.config, AlarmConsumerConfig):
            self._delegates.append(_AnalogDelegate(self.config, consumer_epoch=self.consumer_epoch))
        else:
            for asset in ("Sealer", "Line"):
                rules = tuple(c.config for c in self.config.channels if c.asset_id == asset)
                if not rules:
                    continue
                scope = self.config.scope.model_copy(update={"asset_id": asset})
                for rule in rules:
                    require_rule_target(scope, rule)
                values = dict(
                    scope=scope,
                    gateway_id=self.config.gateway_id,
                    capture_generation=self.config.capture_generation,
                    rules=rules,
                    clock_policy=self.config.clock_policy,
                )
                delegate = (
                    _LineDelegate(_LineConfig(**values), consumer_epoch=self.consumer_epoch)
                    if asset == "Line"
                    else _AnalogDelegate(
                        AlarmConsumerConfig(**values), consumer_epoch=self.consumer_epoch
                    )
                )
                self._delegates.append(delegate)
        for delegate in self._delegates:
            delegate.config_sha256 = self.config_sha256
        self.channels = tuple(
            sorted((pair for d in self._delegates for pair in d.channels), key=lambda pair: pair[0])
        )

    def _write_ready(self, db):
        heads = db.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        if len(heads) != 1 or heads[0] not in SUPPORTED_MIXED_ALARM_REVISIONS:
            raise AlarmConsumerUnavailable("Explicit mixed alarm migration required for mutation")

    def _lock_source_roster(self, db):
        _ready(db)
        IngestionService._advisory(db, "gateway", self.config.gateway_id)
        source = IngestionService._lock_stream(
            db, self.config.scope.source_id, self.config.scope.stream_id
        )
        roster = (
            db.execute(
                text(
                    "SELECT * FROM alarm_capture_frontiers "
                    "WHERE source_id=:s AND stream_id=:t ORDER BY channel_id"
                ),
                {"s": self.config.scope.source_id, "t": self.config.scope.stream_id},
            )
            .mappings()
            .all()
        )
        for channel in sorted({c for c, _ in self.channels} | {r["channel_id"] for r in roster}):
            _lock_channel(db, channel)
        return source, roster

    def _binding(self, source):
        if (source["line_id"], source["gateway_id"], int(source["capture_generation"])) != (
            self.config.scope.line_id,
            self.config.gateway_id,
            self.config.capture_generation,
        ):
            raise SourceBindingError("Explicit coordinator source binding differs")

    def _owned(self, db, source, roster):
        self._write_ready(db)
        self._binding(source)
        if {r["channel_id"] for r in roster} != {c for c, _ in self.channels} or any(
            r["consumer_epoch"] != self.consumer_epoch or r["config_sha256"] != self.config_sha256
            for r in roster
        ):
            raise AlarmConsumerUnavailable("Complete active source roster and epoch required")

    def _installed_heads(self, db):
        try:
            for delegate in self._delegates:
                for channel, config in delegate.channels:
                    delegate._installed(db, channel, config)
        except ValidationError:
            raise AlarmConsumerUnavailable("Installed alarm config unavailable") from None

    def start(self, db: Connection, *, now: datetime):
        now = _TIME.validate_python(now)
        source, roster = self._lock_source_roster(db)
        self._write_ready(db)
        self._binding(source)
        if not {r["channel_id"] for r in roster} <= {c for c, _ in self.channels}:
            raise AlarmConsumerUnavailable(
                "Configured channels cannot omit any durable source frontier"
            )
        if source["conflicted"] or source["reconciliation_required"]:
            raise AlarmConsumerUnavailable("Source requires reconciliation before startup")
        self._installed_heads(db)
        fence = int(
            db.execute(
                text(
                    "SELECT COALESCE(max(capture_seq),0) FROM ingest_batches "
                    "WHERE gateway_id=:g AND source_id=:s AND stream_id=:t "
                    "AND capture_generation=:generation"
                ),
                {
                    "g": self.config.gateway_id,
                    "s": self.config.scope.source_id,
                    "t": self.config.scope.stream_id,
                    "generation": self.config.capture_generation,
                },
            ).scalar_one()
        )
        for delegate in self._delegates:
            delegate.shared_fence = fence
            delegate.start(db, now=now)

    def consume(self, db: Connection, batch: BatchBody, *, source: Mapping, now: datetime):
        batch = BatchBody.model_validate_json(batch.model_dump_json())
        if (batch.gateway_id, batch.source_id, batch.stream_id, batch.capture_generation) != (
            self.config.gateway_id,
            self.config.scope.source_id,
            self.config.scope.stream_id,
            self.config.capture_generation,
        ):
            raise SourceBindingError("Original capture differs from coordinator scope")
        current, roster = self._lock_source_roster(db)
        originals = {}
        for channel, _ in self.channels:
            old = (
                db.execute(
                    text(
                        "SELECT content_sha256,receipt FROM alarm_capture_decisions "
                        "WHERE channel_id=:c AND gateway_id=:g AND batch_id=:b"
                    ),
                    {"c": channel, "g": batch.gateway_id, "b": batch.batch_id},
                )
                .mappings()
                .first()
            )
            if old is not None:
                if old["content_sha256"] != batch_digest(batch):
                    raise AlarmCaptureConflict("Capture identity has different immutable body")
                try:
                    decision = CaptureDecision.model_validate(old["receipt"])
                except ValidationError:
                    raise AlarmConsumerUnavailable("Original capture receipt unavailable") from None
                if (
                    decision.channel_id != channel
                    or decision.batch_id != batch.batch_id
                    or (
                        decision.context is not None
                        and (
                            decision.context.capture != CaptureIdentity.from_batch(batch)
                            or decision.context.batch_sha256 != batch_digest(batch)
                        )
                    )
                ):
                    raise AlarmCaptureConflict("Stored decision does not bind the original capture")
                originals[channel] = decision.model_copy(
                    update={
                        "replayed": True,
                        "result": decision.result.model_copy(
                            update={"replayed": True, "notify_current": False}
                        ),
                    }
                )
        if len(originals) == len(self.channels):
            return tuple(originals[c] for c, _ in self.channels)
        self._owned(db, current, roster)
        self._installed_heads(db)
        now = _TIME.validate_python(now)
        results = {}
        for delegate in self._delegates:
            for decision in delegate.consume(db, batch, source=current, now=now):
                results[decision.channel_id] = decision
        return tuple(results[c] for c, _ in self.channels)

    def invalidate(self, db: Connection, *, source: Mapping, reference_id, reason):
        current, roster = self._lock_source_roster(db)
        self._owned(db, current, roster)
        for delegate in self._delegates:
            delegate.invalidate(db, source=current, reference_id=reference_id, reason=reason)

    def invalidate_authenticated_collision(
        self, db: Connection, *, source: Mapping, reference_id: str, conflict_id: int
    ):
        """Conservatively invalidate current owners after a durable authenticated collision.

        Only ingestion's collision branch invokes this boundary. It cannot process
        captures or take ownership, and does not trust this instance's stale roster.
        """
        reference_id = _ID.validate_python(reference_id)
        if type(conflict_id) is not int or conflict_id <= 0:
            raise AlarmConsumerUnavailable("Persisted collision identity required")
        current, roster = self._lock_source_roster(db)
        self._write_ready(db)
        self._binding(current)
        scope_keys = ("source_id", "stream_id", "gateway_id", "line_id", "capture_generation")
        if any(source[key] != current[key] for key in scope_keys) or not current["conflicted"]:
            raise AlarmConsumerUnavailable("Authoritative conflicted source required")
        evidence = db.execute(
            text(
                "SELECT 1 FROM ingest_conflicts WHERE conflict_id=:id "
                "AND source_id=:s AND stream_id=:t AND gateway_id=:g"
            ),
            {
                "id": conflict_id,
                "s": current["source_id"],
                "t": current["stream_id"],
                "g": current["gateway_id"],
            },
        ).first()
        if evidence is None:
            raise AlarmConsumerUnavailable("Persisted source collision evidence required")
        # The complete sorted source roster is already locked before the first
        # global store input lock, including channels absent from this old config.
        for entry in roster:
            channel = entry["channel_id"]
            frontier = _frontier(db, channel)
            if frontier["requires_restart"]:
                continue
            row = (
                db.execute(
                    text(
                        "SELECT line_id,source_id,stream_id,asset_id,rule_id "
                        "FROM alarm_channels WHERE channel_id=:c"
                    ),
                    {"c": channel},
                )
                .mappings()
                .one()
            )
            scope = AlarmScope(
                **{key: row[key] for key in ("line_id", "source_id", "stream_id", "asset_id")}
            )
            if scope.line_id != current["line_id"]:
                raise AlarmConsumerUnavailable("Durable channel source binding differs")
            position = _allocate(db, frontier)
            break_continuity(
                db,
                scope,
                row["rule_id"],
                input_id="alarm-collision:"
                + _digest([channel, frontier["consumer_epoch"], conflict_id, reference_id]),
                position=position,
                reason="GAP",
                evidence_reference=reference_id,
            )
            db.execute(
                text(
                    "UPDATE alarm_capture_frontiers SET requires_restart=true WHERE channel_id=:c"
                ),
                {"c": channel},
            )
