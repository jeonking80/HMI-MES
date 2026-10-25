"""Gateway-owned durable transport queue; no simulator/API database or network access."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from pydantic import TypeAdapter

from contracts.models import UINT64_MAX, Identifier, PositiveUInt64, UTCTimestamp
from contracts.transport import (
    CaptureTime,
    DurableAck,
    GatewayBatch,
    StreamGap,
    StreamProgress,
    gateway_batch_digest,
    validate_ack,
)

_IDENTIFIER = TypeAdapter(Identifier)
_TIMESTAMP = TypeAdapter(UTCTimestamp)
_POSITIVE = TypeAdapter(PositiveUInt64)
_APPLICATION_ID = 0x484D4742
_SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1
_ROW_ALLOWANCE = 512
_ACK_ENCODING = b"HMIACK1\0"
_MAX_ACK_BYTES = 8 * 1024 * 1024
_LOSS_ENCODING = b"HMILOSS1\0"
_MAX_LOSS_BYTES = 128 * 1024 * 1024


def _uint(value: int) -> str:
    if type(value) is not int or not 0 <= value <= UINT64_MAX:
        raise ValueError("Expected an exact UInt64")
    return f"{value:020d}"


def _decode(value: str) -> int:
    if (
        not isinstance(value, str)
        or len(value) != 20
        or not value.isascii()
        or not value.isdecimal()
    ):
        raise BufferCorruption("Invalid persisted UInt64")
    integer = int(value)
    if integer > UINT64_MAX:
        raise BufferCorruption("Persisted UInt64 overflow")
    return integer


def _json(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _time(value: datetime) -> datetime:
    return _TIMESTAMP.validate_python(value)


def _iso(value: datetime) -> str:
    return _time(value).isoformat()


@dataclass(frozen=True)
class BufferLimits:
    max_age: timedelta = timedelta(minutes=30)
    max_bytes: int = 100_000_000
    max_metadata_bytes: int = 50_000_000
    ledger_retention: timedelta = timedelta(days=7)

    def __post_init__(self):
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise ValueError("Buffer age must be positive")
        if not isinstance(self.ledger_retention, timedelta) or self.ledger_retention <= timedelta(
            0
        ):
            raise ValueError("Ledger retention must be positive")
        for value in (self.max_bytes, self.max_metadata_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("Buffer byte budgets must be exact positive integers")


class BufferFull(ValueError):
    """Caller must apply backpressure/reconcile capacity; no data was silently removed."""


class BatchConflict(ValueError):
    pass


class BufferCorruption(ValueError):
    pass


@dataclass(frozen=True)
class BufferedBatch:
    batch: GatewayBatch
    queued_at: datetime
    status: str
    attempts: int
    accounted_bytes: int


@dataclass(frozen=True)
class BufferLoss:
    loss_id: str
    batch_id: str
    source_id: str
    stream_id: str
    reason_code: str
    detected_at: datetime
    # Exact inclusive intervals, not min/max across holes in an original batch.
    event_ranges: tuple[tuple[int, int], ...]
    affected_execution_ids: tuple[str, ...]
    # Original sample IDs, tags, quality and all observation clocks remain in the loss evidence.
    samples: tuple[dict, ...]
    reported_gaps: tuple[dict, ...]
    capture_time: CaptureTime | None = None


@dataclass(frozen=True)
class EnqueueResult:
    batch_id: str
    status: str
    duplicate: bool
    losses: tuple[BufferLoss, ...] = ()

    @property
    def source_requery_required(self) -> bool:
        return any(loss.event_ranges for loss in self.losses)


@dataclass(frozen=True)
class SourceProgress:
    source_id: str
    stream_id: str
    latest_read_seq: int
    api_progress: StreamProgress


@dataclass(frozen=True)
class BufferStatus:
    pending_batches: int
    inflight_batches: int
    queue_bytes: int
    metadata_bytes: int
    loss_records: int
    oldest_queued_at: datetime | None
    age_limit_reached: bool
    database_bytes: int
    wal_bytes: int
    shared_memory_bytes: int


class GatewayBuffer:
    """Single process/connection owner of a dedicated local POSIX SQLite file.

    API receipt DTOs cannot prove a remote commit. The future authenticated HTTP adapter
    must only pass received COMMITTED receipts here, after the API's transaction commits.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        gateway_id: str | None = None,
        limits: BufferLimits | None = None,
        recover_inflight: bool = True,
    ):
        if type(recover_inflight) is not bool:
            raise ValueError("recover_inflight must be a boolean")
        limits = BufferLimits() if limits is None else limits
        if str(path) == ":memory:" or not isinstance(limits, BufferLimits):
            raise ValueError("GatewayBuffer requires durable storage and typed limits")
        self.path = Path(path).resolve()
        self.limits = limits
        self._connection = None
        self._lock = None
        self._corrupt = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_nlink != 1:
            raise ValueError("Hardlinked database files are unsupported")
        try:
            self._lock = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Inspect a pre-existing file read-only before changing any journal/header settings.
            if self.path.exists() and self.path.stat().st_size:
                with sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True) as probe:
                    version = probe.execute("PRAGMA user_version").fetchone()[0]
                    application = probe.execute("PRAGMA application_id").fetchone()[0]
                    if (
                        version not in (_LEGACY_SCHEMA_VERSION, _SCHEMA_VERSION)
                        or application != _APPLICATION_ID
                    ):
                        raise ValueError(
                            "Not a supported gateway buffer; never reuse another app's DB"
                        )
            if not self.path.exists():
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
            self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            self._connection.row_factory = sqlite3.Row
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.execute("PRAGMA busy_timeout=5000")
            self._initialize()
            with self._transaction():
                self._upgrade_envelope_ledger()
                row = self._db.execute(
                    "SELECT * FROM gateway_identity WHERE singleton=1"
                ).fetchone()
                if row is None:
                    identity = _IDENTIFIER.validate_python(
                        gateway_id if gateway_id is not None else str(uuid4())
                    )
                    self._db.execute(
                        "INSERT INTO gateway_identity VALUES (1,?,?,?)",
                        (identity, _uint(1), _uint(0)),
                    )
                else:
                    identity = _IDENTIFIER.validate_python(row["gateway_id"])
                    if gateway_id is not None and gateway_id != identity:
                        raise ValueError("Gateway identity cannot change on reopen")
                self.gateway_id = identity
                self._upgrade_ledger_checkpoint()
                self._verify()
                # A previous owner's in-flight request may have committed remotely. Replay its
                # exact identity/payload and let the API return its existing durable receipt.
                if recover_inflight:
                    self._db.execute(
                        "UPDATE outbound_batches SET status='PENDING' WHERE status='INFLIGHT'"
                    )
        except BaseException:
            self.close()
            raise

    @property
    def _db(self):
        if self._connection is None:
            raise ValueError("Gateway buffer is closed")
        return self._connection

    def _initialize(self):
        if self._db.execute("PRAGMA user_version").fetchone()[0] in (
            _LEGACY_SCHEMA_VERSION,
            _SCHEMA_VERSION,
        ):
            return
        if self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
            raise ValueError("Unknown existing SQLite schema")
        self._db.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE gateway_identity (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), gateway_id TEXT NOT NULL UNIQUE,
                capture_generation TEXT NOT NULL, last_capture_seq TEXT NOT NULL
            );
            CREATE TABLE batch_ledger (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL UNIQUE,
                content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
                envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256)=64),
                capture_generation TEXT NOT NULL, capture_seq TEXT NOT NULL,
                source_id TEXT NOT NULL, stream_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('QUEUED','ACKNOWLEDGED','EVICTED')),
                ack TEXT, metadata_bytes INTEGER NOT NULL CHECK(metadata_bytes>=0),
                UNIQUE(capture_generation,capture_seq)
            );
            CREATE TABLE outbound_batches (
                batch_id TEXT PRIMARY KEY REFERENCES batch_ledger(batch_id) ON DELETE RESTRICT,
                body TEXT NOT NULL, queued_at TEXT NOT NULL,
                accounted_bytes INTEGER NOT NULL CHECK(accounted_bytes>0),
                status TEXT NOT NULL CHECK(status IN ('PENDING','INFLIGHT')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0)
            );
            CREATE INDEX ix_outbound_status ON outbound_batches(status,queued_at);
            CREATE TABLE source_progress (
                source_id TEXT NOT NULL, stream_id TEXT NOT NULL, latest_read_seq TEXT NOT NULL,
                api_progress TEXT NOT NULL, committed_at TEXT,
                metadata_bytes INTEGER NOT NULL CHECK(metadata_bytes>=0),
                PRIMARY KEY(source_id,stream_id)
            );
            CREATE TABLE missing_ranges (
                loss_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE
                    REFERENCES batch_ledger(batch_id) ON DELETE RESTRICT,
                body TEXT NOT NULL, metadata_bytes INTEGER NOT NULL CHECK(metadata_bytes>0)
            );
            CREATE TABLE loss_intervals (
                loss_id TEXT NOT NULL REFERENCES missing_ranges(loss_id) ON DELETE RESTRICT,
                from_seq TEXT NOT NULL, through_seq TEXT NOT NULL, gap TEXT NOT NULL,
                report_batch_id TEXT REFERENCES batch_ledger(batch_id) ON DELETE RESTRICT,
                metadata_bytes INTEGER NOT NULL CHECK(metadata_bytes>0),
                PRIMARY KEY(loss_id,from_seq,through_seq)
            );
            PRAGMA application_id={_APPLICATION_ID};
            PRAGMA user_version={_LEGACY_SCHEMA_VERSION};
            COMMIT;
            """
        )

    def _upgrade_envelope_ledger(self):
        """Only a known legacy schema proves absent optional evidence; never guess nulls."""
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(batch_ledger)")}
        if "envelope_sha256" in columns:
            return
        if self._db.execute("PRAGMA user_version").fetchone()[0] != _LEGACY_SCHEMA_VERSION:
            raise BufferCorruption("Current ledger is missing immutable envelope evidence")
        for row in self._db.execute("SELECT body FROM outbound_batches"):
            legacy = GatewayBatch.model_validate_json(row["body"])
            if legacy.clock_evidence is not None:
                raise BufferCorruption("Legacy ledger cannot certify measured evidence")
        self._db.execute("ALTER TABLE batch_ledger ADD COLUMN envelope_sha256 TEXT")
        for row in self._db.execute("SELECT batch_id,content_sha256 FROM batch_ledger").fetchall():
            digest = hashlib.sha256(
                _json({"content_sha256": row["content_sha256"]}).encode("utf-8")
            ).hexdigest()
            self._db.execute(
                "UPDATE batch_ledger SET envelope_sha256=? WHERE batch_id=?",
                (digest, row["batch_id"]),
            )

    def _checkpoint_digest(self, values: dict) -> str:
        return hashlib.sha256(
            _json(
                {"domain": "gateway-ledger-checkpoint-v1", "gateway_id": self.gateway_id, **values}
            ).encode("utf-8")
        ).hexdigest()

    def _checkpoint_values(self, *, generation, sequence, cumulative, deleted, updated):
        return {
            "capture_generation": _uint(generation),
            "last_capture_seq": _uint(sequence),
            "cumulative_sha256": cumulative,
            "deleted_count": deleted,
            "updated_at": updated,
        }

    def _write_checkpoint(self, values):
        digest = self._checkpoint_digest(values)
        size = len(_json({**values, "checkpoint_sha256": digest}).encode("utf-8")) + _ROW_ALLOWANCE
        self._db.execute(
            "INSERT OR REPLACE INTO ledger_checkpoint VALUES (1,?,?,?,?,?,?,?)",
            (*values.values(), digest, size),
        )

    def _upgrade_ledger_checkpoint(self):
        if self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_checkpoint'"
        ).fetchone():
            if self._db.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA_VERSION:
                raise BufferCorruption("Unexpected checkpoint in legacy schema")
            return
        if self._db.execute("PRAGMA user_version").fetchone()[0] != _LEGACY_SCHEMA_VERSION:
            raise BufferCorruption("Checkpoint missing from current schema")
        # Validate the complete legacy chain and identities before certifying an empty prefix.
        self._verify()
        self._db.execute(
            "CREATE TABLE ledger_checkpoint (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
            "capture_generation TEXT NOT NULL,last_capture_seq TEXT NOT NULL,"
            "cumulative_sha256 TEXT NOT NULL,"
            "deleted_count INTEGER NOT NULL CHECK(deleted_count>=0),"
            "updated_at TEXT NOT NULL,checkpoint_sha256 TEXT NOT NULL,"
            "metadata_bytes INTEGER NOT NULL CHECK(metadata_bytes>=0))"
        )
        generation, _ = self.capture_position
        self._write_checkpoint(
            self._checkpoint_values(
                generation=generation,
                sequence=0,
                cumulative=self._checkpoint_digest({"capture_generation": _uint(generation)}),
                deleted=0,
                updated=_iso(datetime(1970, 1, 1, tzinfo=UTC)),
            )
        )
        self._db.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    def _read_checkpoint(self):
        try:
            return self._validated_checkpoint()
        except BufferCorruption:
            raise
        except (ValueError, TypeError, KeyError, IndexError) as error:
            raise BufferCorruption("Invalid persisted ledger checkpoint") from error

    def _validated_checkpoint(self):
        row = self._db.execute("SELECT * FROM ledger_checkpoint").fetchall()
        if len(row) != 1 or row[0]["singleton"] != 1:
            raise BufferCorruption("Missing or duplicate ledger checkpoint")
        row = row[0]
        generation = _decode(row["capture_generation"])
        sequence = _decode(row["last_capture_seq"])
        values = self._checkpoint_values(
            generation=generation,
            sequence=sequence,
            cumulative=row["cumulative_sha256"],
            deleted=row["deleted_count"],
            updated=row["updated_at"],
        )
        digest = self._checkpoint_digest(values)
        cumulative = row["cumulative_sha256"]
        if (
            generation != self.capture_position[0]
            or sequence > self.capture_position[1]
            or type(row["deleted_count"]) is not int
            or row["deleted_count"] != sequence
            or not isinstance(cumulative, str)
            or len(cumulative) != 64
            or any(char not in "0123456789abcdef" for char in cumulative)
            or row["checkpoint_sha256"] != digest
            or row["metadata_bytes"]
            != len(_json({**values, "checkpoint_sha256": digest}).encode("utf-8")) + _ROW_ALLOWANCE
        ):
            raise BufferCorruption("Invalid ledger checkpoint identity/accounting")
        try:
            _time(row["updated_at"])
        except (ValueError, TypeError) as error:
            raise BufferCorruption("Invalid checkpoint update timestamp") from error
        if sequence == 0 and cumulative != self._checkpoint_digest(
            {"capture_generation": _uint(generation)}
        ):
            raise BufferCorruption("Invalid initial ledger checkpoint")
        return row

    @contextmanager
    def _transaction(self):
        if self._corrupt:
            raise BufferCorruption("Buffer is gated after persisted corruption")
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.commit()
        except BaseException as error:
            self._db.rollback()
            if isinstance(error, BufferCorruption):
                self._corrupt = True
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    @property
    def capture_position(self) -> tuple[int, int]:
        row = self._db.execute(
            "SELECT capture_generation,last_capture_seq FROM gateway_identity"
        ).fetchone()
        return _decode(row[0]), _decode(row[1])

    @staticmethod
    def _validated_batch(batch: GatewayBatch) -> GatewayBatch:
        if not isinstance(batch, GatewayBatch):
            raise ValueError("Expected a typed GatewayBatch")
        return GatewayBatch.model_validate_json(batch.model_dump_json(warnings=False))

    def _row_batch(self, row) -> BufferedBatch:
        try:
            batch = GatewayBatch.model_validate_json(row["body"])
            ledger = self._db.execute(
                "SELECT * FROM batch_ledger WHERE batch_id=?", (row["batch_id"],)
            ).fetchone()
            if (
                ledger is None
                or ledger["state"] != "QUEUED"
                or batch.body.batch_id != row["batch_id"]
                or batch.body.gateway_id != self.gateway_id
                or batch.content_sha256 != ledger["content_sha256"]
                or gateway_batch_digest(batch) != ledger["envelope_sha256"]
                or _uint(batch.body.capture_generation) != ledger["capture_generation"]
                or _uint(batch.body.capture_seq) != ledger["capture_seq"]
                or batch.body.source_id != ledger["source_id"]
                or batch.body.stream_id != ledger["stream_id"]
                or row["accounted_bytes"] != len(row["body"].encode("utf-8")) + _ROW_ALLOWANCE
            ):
                raise BufferCorruption("Persisted batch disagrees with immutable ledger")
            return BufferedBatch(
                batch,
                _time(row["queued_at"]),
                row["status"],
                row["attempts"],
                row["accounted_bytes"],
            )
        except (ValueError, TypeError, KeyError) as error:
            self._corrupt = True
            raise BufferCorruption("Invalid persisted buffered batch") from error

    def _metadata_bytes(self) -> int:
        total = sum(
            self._db.execute(f"SELECT coalesce(sum(metadata_bytes),0) FROM {table}").fetchone()[0]
            for table in ("batch_ledger", "source_progress", "missing_ranges", "loss_intervals")
        )
        if self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_checkpoint'"
        ).fetchone():
            total += self._db.execute(
                "SELECT coalesce(sum(metadata_bytes),0) FROM ledger_checkpoint"
            ).fetchone()[0]
        return total

    def _check_metadata_budget(self):
        if self._metadata_bytes() > self.limits.max_metadata_bytes:
            raise BufferFull(
                "METADATA_FULL: preserve loss/receipt evidence; reconcile before continuing"
            )

    def _queue_bytes(self) -> int:
        return self._db.execute(
            "SELECT coalesce(sum(accounted_bytes),0) FROM outbound_batches"
        ).fetchone()[0]

    @staticmethod
    def _intervals(events) -> tuple[tuple[int, int], ...]:
        intervals = []
        for event in events:
            if intervals and event.seq == intervals[-1][1] + 1:
                intervals[-1] = (intervals[-1][0], event.seq)
            else:
                intervals.append((event.seq, event.seq))
        return tuple(intervals)

    def _record_loss(self, buffered: BufferedBatch, *, now: datetime, reason: str) -> BufferLoss:
        body = buffered.batch.body
        evidence = {
            "loss_id": str(uuid4()),
            "batch_id": body.batch_id,
            "source_id": body.source_id,
            "stream_id": body.stream_id,
            "reason_code": reason,
            "detected_at": _iso(now),
            "event_ranges": [[str(start), str(end)] for start, end in self._intervals(body.events)],
            "affected_execution_ids": sorted(
                {event.execution_id for event in body.events if event.execution_id is not None}
            ),
            "samples": [
                sample.model_dump(mode="json", exclude={"reading"}) for sample in body.samples
            ],
            "reported_gaps": [gap.model_dump(mode="json") for gap in body.gaps],
        }
        if body.capture_time is not None:
            evidence["capture_time"] = body.capture_time.model_dump(mode="json")
        encoded = _json(evidence)
        self._db.execute(
            "INSERT INTO missing_ranges VALUES (?,?,?,?)",
            (
                evidence["loss_id"],
                body.batch_id,
                encoded,
                len(encoded.encode("utf-8")) + _ROW_ALLOWANCE,
            ),
        )
        self._db.execute("DELETE FROM outbound_batches WHERE batch_id=?", (body.batch_id,))
        self._db.execute(
            "UPDATE batch_ledger SET state='EVICTED' WHERE batch_id=?", (body.batch_id,)
        )
        intervals = tuple(
            StreamGap(
                from_seq=start,
                through_seq=end,
                irrecoverable=False,
                detected_at=now,
                reason_code=reason,
                reason="Buffered source events require original-history replay",
                affected_execution_ids=evidence["affected_execution_ids"],
            )
            for start, end in self._intervals(body.events)
        )
        for gap in (*intervals, *body.gaps):
            gap_json = gap.model_dump_json()
            self._db.execute(
                "INSERT INTO loss_intervals VALUES (?,?,?,?,NULL,?)",
                (
                    evidence["loss_id"],
                    _uint(gap.from_seq),
                    _uint(gap.through_seq),
                    gap_json,
                    len(gap_json.encode("utf-8")) + _ROW_ALLOWANCE,
                ),
            )
        return self._loss_from_json(encoded)

    @classmethod
    def _loss_from_json(cls, encoded: str | bytes) -> BufferLoss:
        try:
            value = json.loads(cls._loss_bytes(encoded))
            intervals = tuple(
                (_POSITIVE.validate_python(start), _POSITIVE.validate_python(end))
                for start, end in value["event_ranges"]
            )
            previous = 0
            for start, end in intervals:
                if start <= previous or end < start:
                    raise ValueError("Invalid lost source interval")
                previous = end
            executions = tuple(
                _IDENTIFIER.validate_python(item) for item in value["affected_execution_ids"]
            )
            if len(set(executions)) != len(executions):
                raise ValueError("Duplicate affected execution")
            for sample in value["samples"]:
                if set(sample) != {
                    "sample_id",
                    "tag_id",
                    "source_timestamp",
                    "server_timestamp",
                    "acquired_at",
                    "status_code",
                }:
                    raise ValueError("Invalid lost observation metadata")
                _IDENTIFIER.validate_python(sample["sample_id"])
                _IDENTIFIER.validate_python(sample["tag_id"])
                _time(sample["acquired_at"])
                for name in ("source_timestamp", "server_timestamp"):
                    if sample[name] is not None:
                        _time(sample[name])
                if type(sample["status_code"]) is not int or not 0 <= sample["status_code"] < 2**32:
                    raise ValueError("Invalid lost observation status")
            for gap in value["reported_gaps"]:
                StreamGap.model_validate_json(_json(gap))
            return BufferLoss(
                loss_id=_IDENTIFIER.validate_python(value["loss_id"]),
                batch_id=_IDENTIFIER.validate_python(value["batch_id"]),
                source_id=_IDENTIFIER.validate_python(value["source_id"]),
                stream_id=_IDENTIFIER.validate_python(value["stream_id"]),
                reason_code=_IDENTIFIER.validate_python(value["reason_code"]),
                detected_at=_time(value["detected_at"]),
                event_ranges=intervals,
                affected_execution_ids=executions,
                samples=tuple(value["samples"]),
                reported_gaps=tuple(value["reported_gaps"]),
                capture_time=(
                    CaptureTime.model_validate(value["capture_time"])
                    if value.get("capture_time") is not None
                    else None
                ),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise BufferCorruption("Invalid persisted loss evidence") from error

    def _ensure_progress(self, source_id: str, stream_id: str):
        progress = StreamProgress(
            source_id=source_id, stream_id=stream_id, verified_through_seq=0, latest_ingested_seq=0
        )
        encoded = progress.model_dump_json()
        self._db.execute(
            "INSERT OR IGNORE INTO source_progress VALUES (?,?,?,?,NULL,?)",
            (
                source_id,
                stream_id,
                _uint(0),
                encoded,
                len(encoded.encode("utf-8")) + _ROW_ALLOWANCE,
            ),
        )

    def _before_enqueue_commit(self):
        """Fault-injection seam; no public transport endpoint exposes this hook."""

    def _after_enqueue_commit(self):
        """State is durable when this fault-injection hook runs."""

    def enqueue(
        self, batch: GatewayBatch, *, now: datetime, latest_read_seq: int | None = None
    ) -> EnqueueResult:
        batch = self._validated_batch(batch)
        now = _time(now)
        body = batch.body
        if body.gateway_id != self.gateway_id:
            raise ValueError("Batch belongs to another gateway")
        read_seq = (
            max((event.seq for event in body.events), default=0)
            if latest_read_seq is None
            else latest_read_seq
        )
        _uint(read_seq)
        if body.events and read_seq < body.events[-1].seq:
            raise ValueError("Read position cannot precede events in this batch")
        encoded = _json(batch.model_dump(mode="json"))
        size = len(encoded.encode("utf-8")) + _ROW_ALLOWANCE
        # Commit useful maintenance separately: a subsequent collection failure must not
        # roll back an already eligible prefix. ACK handling never invokes retention.
        prospective_metadata = (
            self._metadata_bytes() + _ROW_ALLOWANCE + len(body.batch_id.encode("utf-8")) + 64
        )
        if not self._db.execute(
            "SELECT 1 FROM source_progress WHERE source_id=? AND stream_id=?",
            (body.source_id, body.stream_id),
        ).fetchone():
            progress = StreamProgress(
                source_id=body.source_id,
                stream_id=body.stream_id,
                verified_through_seq=0,
                latest_ingested_seq=0,
            )
            prospective_metadata += len(progress.model_dump_json().encode("utf-8")) + _ROW_ALLOWANCE
        # Start maintenance with headroom for the upcoming receipt and loss evidence.
        # This is best-effort headroom, not a promise that a large ACK cannot fill metadata.
        # The threshold only schedules eligible cleanup; the hard byte limit is unchanged.
        if prospective_metadata > self.limits.max_metadata_bytes * 9 // 10:
            oldest = self._db.execute(
                "SELECT * FROM batch_ledger ORDER BY ordinal LIMIT 1"
            ).fetchone()
            if (
                oldest is not None
                and oldest["state"] == "ACKNOWLEDGED"
                and now - self._load_ack(oldest["ack"]).committed_at >= self.limits.ledger_retention
            ):
                self.prune_acknowledged(now=now)
        with self._transaction():
            existing = self._db.execute(
                "SELECT * FROM batch_ledger WHERE batch_id=?", (body.batch_id,)
            ).fetchone()
            if existing is not None:
                if existing["content_sha256"] != batch.content_sha256 or existing[
                    "envelope_sha256"
                ] != gateway_batch_digest(batch):
                    raise BatchConflict("Same batch ID has different immutable content")
                return EnqueueResult(body.batch_id, existing["state"], True)
            if size > self.limits.max_bytes:
                raise BufferFull(
                    "BATCH_TOO_LARGE: capture must be split before assigning immutable IDs"
                )
            generation, last_seq = self.capture_position
            if generation != body.capture_generation or last_seq == UINT64_MAX:
                raise BufferFull("CAPTURE_EXHAUSTED_OR_REPLACED: explicit reconciliation required")
            if body.capture_seq != last_seq + 1:
                raise BatchConflict("Capture sequence must be the next durable position")
            losses = []
            rows = self._db.execute(
                "SELECT outbound_batches.* FROM outbound_batches "
                "JOIN batch_ledger USING(batch_id) ORDER BY ordinal"
            ).fetchall()
            for row in rows:
                buffered = self._row_batch(row)
                expired = now - buffered.queued_at >= self.limits.max_age
                too_large = self._queue_bytes() + size > self.limits.max_bytes
                if not expired and not too_large:
                    break
                if row["status"] == "INFLIGHT":
                    raise BufferFull(
                        "INFLIGHT_PRESSURE: retry/reconcile request before recording eviction"
                    )
                losses.append(
                    self._record_loss(
                        buffered,
                        now=now,
                        reason="BUFFER_AGE_LIMIT" if expired else "BUFFER_BYTE_LIMIT",
                    )
                )
            self._db.execute(
                "INSERT INTO batch_ledger (batch_id,content_sha256,envelope_sha256,"
                "capture_generation,capture_seq,"
                "source_id,stream_id,state,metadata_bytes) VALUES (?,?,?,?,?,?,?,'QUEUED',?)",
                (
                    body.batch_id,
                    batch.content_sha256,
                    gateway_batch_digest(batch),
                    _uint(body.capture_generation),
                    _uint(body.capture_seq),
                    body.source_id,
                    body.stream_id,
                    _ROW_ALLOWANCE + len(body.batch_id.encode("utf-8")) + 64,
                ),
            )
            self._db.execute(
                "INSERT INTO outbound_batches VALUES (?,?,?,?,'PENDING',0)",
                (body.batch_id, encoded, _iso(now), size),
            )
            self._ensure_progress(body.source_id, body.stream_id)
            self._db.execute(
                "UPDATE source_progress SET latest_read_seq=max(latest_read_seq,?) "
                "WHERE source_id=? AND stream_id=?",
                (_uint(read_seq), body.source_id, body.stream_id),
            )
            self._db.execute(
                "UPDATE gateway_identity SET last_capture_seq=?", (_uint(body.capture_seq),)
            )
            # Each immutable gap report owns individual intervals. Its eviction makes those
            # intervals pending again; only a verified API ACK finishes the reporting duty.
            for gap in body.gaps:
                self._db.execute(
                    "UPDATE loss_intervals SET report_batch_id=? WHERE gap=? "
                    "AND loss_id IN (SELECT loss.loss_id FROM missing_ranges loss "
                    "JOIN batch_ledger owner ON owner.batch_id=loss.batch_id "
                    "WHERE owner.source_id=? AND owner.stream_id=?) "
                    "AND (report_batch_id IS NULL OR report_batch_id IN "
                    "(SELECT batch_id FROM batch_ledger WHERE state='EVICTED'))",
                    (body.batch_id, gap.model_dump_json(), body.source_id, body.stream_id),
                )
            self._compact_lossless_metadata()
            self._check_metadata_budget()
            self._before_enqueue_commit()
        self._after_enqueue_commit()
        return EnqueueResult(body.batch_id, "QUEUED", False, tuple(losses))

    def lease_next(self) -> BufferedBatch | None:
        with self._transaction():
            row = self._db.execute(
                "SELECT outbound_batches.* FROM outbound_batches JOIN batch_ledger USING(batch_id) "
                "WHERE status='PENDING' ORDER BY ordinal LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._row_batch(row)
            self._db.execute(
                "UPDATE outbound_batches SET status='INFLIGHT',attempts=attempts+1 "
                "WHERE batch_id=?",
                (row["batch_id"],),
            )
            return self._row_batch(
                self._db.execute(
                    "SELECT * FROM outbound_batches WHERE batch_id=?", (row["batch_id"],)
                ).fetchone()
            )

    def retry(self, batch_id: str):
        batch_id = _IDENTIFIER.validate_python(batch_id)
        with self._transaction():
            if not self._db.execute(
                "SELECT 1 FROM outbound_batches WHERE batch_id=?", (batch_id,)
            ).fetchone():
                raise ValueError("Batch is no longer queued")
            self._db.execute(
                "UPDATE outbound_batches SET status='PENDING' WHERE batch_id=?", (batch_id,)
            )

    def _before_ack_commit(self):
        """Crash seam after delete/progress writes but before atomic commit."""

    def _after_ack_commit(self):
        """Crash seam after receipt/delete/progress are durable."""

    @staticmethod
    def _ack_bytes(value: str | bytes) -> bytes:
        if isinstance(value, str):
            return value.encode("utf-8")
        if not isinstance(value, bytes) or not value.startswith(_ACK_ENCODING):
            raise BufferCorruption("Unknown persisted ACK encoding")
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(value[len(_ACK_ENCODING) :], _MAX_ACK_BYTES + 1)
            if (
                len(raw) > _MAX_ACK_BYTES
                or not decoder.eof
                or decoder.unused_data
                or decoder.unconsumed_tail
            ):
                raise BufferCorruption("Invalid compressed ACK bounds")
            return raw
        except zlib.error as error:
            raise BufferCorruption("Invalid compressed ACK") from error

    @classmethod
    def _load_ack(cls, value: str | bytes) -> DurableAck:
        try:
            return DurableAck.model_validate_json(cls._ack_bytes(value))
        except (ValueError, TypeError) as error:
            raise BufferCorruption("Invalid persisted ACK") from error

    @staticmethod
    def _ack_stored_size(value: str | bytes) -> int:
        return len(value.encode("utf-8")) if isinstance(value, str) else len(value)

    @staticmethod
    def _loss_bytes(value: str | bytes) -> bytes:
        """Decode exact original evidence bytes with a bounded, complete zlib stream."""
        if isinstance(value, str):
            return value.encode("utf-8")
        if not isinstance(value, bytes) or not value.startswith(_LOSS_ENCODING):
            raise BufferCorruption("Unknown persisted loss encoding")
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(value[len(_LOSS_ENCODING) :], _MAX_LOSS_BYTES + 1)
            if (
                len(raw) > _MAX_LOSS_BYTES
                or not decoder.eof
                or decoder.unused_data
                or decoder.unconsumed_tail
            ):
                raise BufferCorruption("Invalid compressed loss bounds")
            return raw
        except zlib.error as error:
            raise BufferCorruption("Invalid compressed loss") from error

    def _compact_lossless_metadata(self, *, keep_batch_id=None, force=False):
        ack_count = self._compact_verified_receipts(keep_batch_id=keep_batch_id, force=force)
        loss_count = 0
        if force or self._metadata_bytes() > self.limits.max_metadata_bytes:
            for row in self._db.execute(
                "SELECT * FROM missing_ranges WHERE typeof(body)='text' ORDER BY rowid"
            ).fetchall():
                self._loss_from_json(row["body"])
                raw = self._loss_bytes(row["body"])
                if row["metadata_bytes"] != len(raw) + _ROW_ALLOWANCE:
                    raise BufferCorruption("Loss metadata accounting mismatch")
                # Keep oversized legacy TEXT readable without producing an unreadable BLOB.
                if len(raw) > _MAX_LOSS_BYTES:
                    continue
                compact = _LOSS_ENCODING + zlib.compress(raw)
                if len(compact) >= len(raw):
                    continue
                if self._loss_bytes(compact) != raw:
                    raise BufferCorruption("Loss compression verification failed")
                self._db.execute(
                    "UPDATE missing_ranges SET body=?,metadata_bytes=? WHERE loss_id=?",
                    (compact, len(compact) + _ROW_ALLOWANCE, row["loss_id"]),
                )
                loss_count += 1
        return ack_count, loss_count

    def compact_metadata(self) -> dict[str, int]:
        """Force lossless metadata compression in one fully verified transaction."""
        with self._transaction():
            self._verify()
            before = self._metadata_bytes()
            ack_count, loss_count = self._compact_lossless_metadata(force=True)
            self._verify()
            return {
                "metadata_bytes_before": before,
                "metadata_bytes_after": self._metadata_bytes(),
                "ack_rows_compacted": ack_count,
                "loss_rows_compacted": loss_count,
            }

    def _before_prune_commit(self):
        """Fault seam after checkpoint update and deletion, before atomic commit."""

    def _after_prune_commit(self):
        """The checkpoint and prefix deletion are durable when this hook runs."""

    def _validate_prune_row(self, row, *, generation, sequence):
        """Validate a bounded deletion candidate; startup verifies the complete database."""
        try:
            if (
                _decode(row["capture_generation"]) != generation
                or _decode(row["capture_seq"]) != sequence + 1
                or sequence + 1 > self.capture_position[1]
            ):
                raise BufferCorruption("Pruning prefix does not follow checkpoint")
            for name in ("batch_id", "source_id", "stream_id"):
                _IDENTIFIER.validate_python(row[name])
            for name in ("content_sha256", "envelope_sha256"):
                digest = row[name]
                if (
                    not isinstance(digest, str)
                    or len(digest) != 64
                    or any(char not in "0123456789abcdef" for char in digest)
                ):
                    raise BufferCorruption("Invalid pruning candidate digest")
            ack = self._load_ack(row["ack"])
            if (
                ack.batch_id != row["batch_id"]
                or ack.gateway_id != self.gateway_id
                or ack.content_sha256 != row["content_sha256"]
                or (ack.progress.source_id, ack.progress.stream_id)
                != (row["source_id"], row["stream_id"])
                or row["metadata_bytes"]
                != _ROW_ALLOWANCE
                + len(row["batch_id"].encode("utf-8"))
                + 64
                + self._ack_stored_size(row["ack"])
            ):
                raise BufferCorruption("Pruning receipt identity/accounting mismatch")
            return ack
        except (ValueError, TypeError, KeyError) as error:
            raise BufferCorruption("Invalid pruning candidate") from error

    def _prune_acknowledged(self, *, now: datetime, limit: int):
        checkpoint = self._read_checkpoint()
        before = self._metadata_bytes()
        cumulative = checkpoint["cumulative_sha256"]
        sequence = _decode(checkpoint["last_capture_seq"])
        removed = []
        for row in self._db.execute(
            "SELECT ledger.* FROM batch_ledger ledger ORDER BY ordinal LIMIT ?", (limit,)
        ).fetchall():
            if row["state"] != "ACKNOWLEDGED":
                break
            ack = self._validate_prune_row(
                row, generation=_decode(checkpoint["capture_generation"]), sequence=sequence
            )
            if now - ack.committed_at < self.limits.ledger_retention:
                break
            if any(
                self._db.execute(query, (row["batch_id"],)).fetchone()
                for query in (
                    "SELECT 1 FROM outbound_batches WHERE batch_id=?",
                    "SELECT 1 FROM missing_ranges WHERE batch_id=?",
                    "SELECT 1 FROM loss_intervals WHERE report_batch_id=?",
                )
            ):
                break
            sequence = _decode(row["capture_seq"])
            # Canonical receipt JSON makes the cumulative proof independent of compression.
            cumulative = self._checkpoint_digest(
                {
                    "previous_sha256": cumulative,
                    "capture_generation": row["capture_generation"],
                    "capture_seq": row["capture_seq"],
                    "batch_id": row["batch_id"],
                    "content_sha256": row["content_sha256"],
                    "envelope_sha256": row["envelope_sha256"],
                    "source_id": row["source_id"],
                    "stream_id": row["stream_id"],
                    "ack": ack.model_dump(mode="json"),
                }
            )
            removed.append(row["batch_id"])
        if removed:
            self._write_checkpoint(
                self._checkpoint_values(
                    generation=_decode(checkpoint["capture_generation"]),
                    sequence=sequence,
                    cumulative=cumulative,
                    deleted=checkpoint["deleted_count"] + len(removed),
                    updated=_iso(now),
                )
            )
            self._db.executemany(
                "DELETE FROM batch_ledger WHERE batch_id=?", ((item,) for item in removed)
            )
            self._before_prune_commit()
        remaining = self._db.execute(
            "SELECT capture_generation,capture_seq FROM batch_ledger ORDER BY ordinal LIMIT 1"
        ).fetchone()
        if remaining is None:
            if sequence != self.capture_position[1]:
                raise BufferCorruption("Checkpoint does not cover the capture position")
        elif (
            _decode(remaining["capture_generation"]) != _decode(checkpoint["capture_generation"])
            or _decode(remaining["capture_seq"]) != sequence + 1
            or _decode(remaining["capture_seq"]) > self.capture_position[1]
        ):
            raise BufferCorruption("Remaining ledger does not follow the checkpoint")
        after = self._metadata_bytes()
        return {
            "pruned_rows": len(removed),
            "metadata_bytes_before": before,
            "metadata_bytes_after": after,
            "metadata_bytes_reclaimed": before - after,
            "checkpoint_last_capture_seq": sequence,
            "checkpoint_deleted_count": checkpoint["deleted_count"] + len(removed),
        }

    def prune_acknowledged(self, *, now: datetime, limit: int = 1000) -> dict[str, int]:
        """Retain all evidence except an old, committed, unreferenced contiguous prefix.

        Logical byte accounting shrinks immediately; SQLite file allocation may be reused
        by subsequent writes and is intentionally not vacuumed on the capture connection.
        """
        now = _time(now)
        if type(limit) is not int or limit < 1:
            raise ValueError("Pruning limit must be an exact positive integer")
        with self._transaction():
            result = self._prune_acknowledged(now=now, limit=limit)
        self._after_prune_commit()
        return result

    def _compact_verified_receipts(self, *, keep_batch_id: str | None = None, force=False):
        """Losslessly encode terminal receipts under pressure, retaining all identity proof."""
        if not force and self._metadata_bytes() <= self.limits.max_metadata_bytes:
            return 0
        count = 0
        rows = self._db.execute(
            "SELECT ledger.* FROM batch_ledger ledger "
            "WHERE ledger.state='ACKNOWLEDGED' AND (? IS NULL OR ledger.batch_id<>?) "
            "AND typeof(ledger.ack)='text' "
            "AND NOT EXISTS (SELECT 1 FROM outbound_batches queued "
            "WHERE queued.batch_id=ledger.batch_id) "
            "AND NOT EXISTS (SELECT 1 FROM missing_ranges loss "
            "WHERE loss.batch_id=ledger.batch_id) "
            "AND NOT EXISTS (SELECT 1 FROM loss_intervals loss "
            "WHERE loss.report_batch_id=ledger.batch_id) ORDER BY ledger.ordinal",
            (keep_batch_id, keep_batch_id),
        ).fetchall()
        for row in rows:
            receipt = self._load_ack(row["ack"])
            if (
                receipt.batch_id != row["batch_id"]
                or receipt.gateway_id != self.gateway_id
                or receipt.content_sha256 != row["content_sha256"]
                or (receipt.progress.source_id, receipt.progress.stream_id)
                != (row["source_id"], row["stream_id"])
            ):
                raise BufferCorruption("Ledger receipt identity mismatch")
            current = self.progress(row["source_id"], row["stream_id"]).api_progress
            if (
                receipt.recorded_gaps
                or receipt.progress.unresolved_gaps
                or receipt.progress.latest_ingested_seq > current.verified_through_seq
                or any(
                    event.disposition != "APPLIED"
                    or event.seq > receipt.progress.verified_through_seq
                    or event.seq > current.verified_through_seq
                    for event in receipt.events
                )
            ):
                continue
            raw = self._ack_bytes(row["ack"])
            if len(raw) > _MAX_ACK_BYTES:
                continue
            compact = _ACK_ENCODING + zlib.compress(raw)
            if len(compact) >= len(raw):
                continue
            self._db.execute(
                "UPDATE batch_ledger SET ack=?,metadata_bytes=metadata_bytes-?+? WHERE batch_id=?",
                (compact, len(raw), len(compact), row["batch_id"]),
            )
            count += 1
        return count

    def acknowledge(self, ack: DurableAck) -> DurableAck:
        if not isinstance(ack, DurableAck):
            raise ValueError("Expected a typed committed DurableAck")
        ack = DurableAck.model_validate_json(ack.model_dump_json(warnings=False))
        with self._transaction():
            ledger = self._db.execute(
                "SELECT * FROM batch_ledger WHERE batch_id=?", (ack.batch_id,)
            ).fetchone()
            if (
                ledger is None
                or ack.gateway_id != self.gateway_id
                or ack.content_sha256 != ledger["content_sha256"]
            ):
                raise ValueError("ACK identity/hash does not match a known batch")
            if ledger["state"] == "ACKNOWLEDGED":
                previous = self._load_ack(ledger["ack"])
                if previous != ack:
                    raise BatchConflict("Committed receipt for this batch is immutable")
                return previous
            if ledger["state"] == "EVICTED":
                raise ValueError(
                    "Batch was evicted with explicit loss evidence; source reconciliation required"
                )
            row = self._db.execute(
                "SELECT * FROM outbound_batches WHERE batch_id=?", (ack.batch_id,)
            ).fetchone()
            if row is None:
                raise BufferCorruption("Queued ledger entry is missing its payload")
            batch = self._row_batch(row).batch
            validate_ack(batch, ack)
            source, stream = batch.body.source_id, batch.body.stream_id
            current_row = self._db.execute(
                "SELECT * FROM source_progress WHERE source_id=? AND stream_id=?", (source, stream)
            ).fetchone()
            current = StreamProgress.model_validate_json(current_row["api_progress"])
            old_pair = (current.verified_through_seq, current.latest_ingested_seq)
            new_pair = (ack.progress.verified_through_seq, ack.progress.latest_ingested_seq)
            newer = all(new >= old for new, old in zip(new_pair, old_pair, strict=True))
            older = all(new <= old for new, old in zip(new_pair, old_pair, strict=True))
            if not newer and not older:
                raise ValueError("Incomparable API frontiers require reconciliation")
            if newer:
                # Inbox coverage only grows. A delayed receipt may describe older holes even
                # with equal frontiers and a later wall clock. Existing coverage cannot reopen.
                gaps = []
                for incoming in ack.progress.unresolved_gaps:
                    for known in current.unresolved_gaps:
                        start = max(incoming.from_seq, known.from_seq)
                        end = min(incoming.through_seq, known.through_seq)
                        if start <= end:
                            evidence = known if known.irrecoverable else incoming
                            gaps.append(
                                evidence.model_copy(
                                    update={
                                        "from_seq": start,
                                        "through_seq": end,
                                        "affected_execution_ids": tuple(
                                            sorted(
                                                set(known.affected_execution_ids)
                                                | set(incoming.affected_execution_ids)
                                            )
                                        ),
                                    }
                                )
                            )
                    start = max(incoming.from_seq, current.latest_ingested_seq + 1)
                    if start <= incoming.through_seq:
                        gaps.append(incoming.model_copy(update={"from_seq": start}))
                gaps.sort(key=lambda gap: gap.from_seq)
                merged = ack.progress.model_copy(update={"unresolved_gaps": tuple(gaps)})
                merged = StreamProgress.model_validate_json(merged.model_dump_json())
                progress_json = merged.model_dump_json()
                self._db.execute(
                    "UPDATE source_progress SET api_progress=?,committed_at=?,metadata_bytes=? "
                    "WHERE source_id=? AND stream_id=?",
                    (
                        progress_json,
                        _iso(ack.committed_at),
                        len(progress_json.encode("utf-8")) + _ROW_ALLOWANCE,
                        source,
                        stream,
                    ),
                )
            ack_json = ack.model_dump_json()
            self._db.execute(
                "UPDATE batch_ledger SET state='ACKNOWLEDGED',ack=?,"
                "metadata_bytes=metadata_bytes+? WHERE batch_id=?",
                (ack_json, len(ack_json.encode("utf-8")), ack.batch_id),
            )
            self._db.execute("DELETE FROM outbound_batches WHERE batch_id=?", (ack.batch_id,))
            self._compact_lossless_metadata(keep_batch_id=ack.batch_id)
            self._check_metadata_budget()
            self._before_ack_commit()
        self._after_ack_commit()
        return ack

    def progress(self, source_id: str, stream_id: str) -> SourceProgress:
        source_id, stream_id = (
            _IDENTIFIER.validate_python(source_id),
            _IDENTIFIER.validate_python(stream_id),
        )
        row = self._db.execute(
            "SELECT * FROM source_progress WHERE source_id=? AND stream_id=?",
            (source_id, stream_id),
        ).fetchone()
        if row is None:
            return SourceProgress(
                source_id,
                stream_id,
                0,
                StreamProgress(
                    source_id=source_id,
                    stream_id=stream_id,
                    verified_through_seq=0,
                    latest_ingested_seq=0,
                ),
            )
        try:
            return SourceProgress(
                source_id,
                stream_id,
                _decode(row["latest_read_seq"]),
                StreamProgress.model_validate_json(row["api_progress"]),
            )
        except ValueError as error:
            self._corrupt = True
            raise BufferCorruption("Invalid persisted stream progress") from error

    def losses(self) -> tuple[BufferLoss, ...]:
        try:
            return tuple(
                self._loss_from_json(row[0])
                for row in self._db.execute("SELECT body FROM missing_ranges ORDER BY rowid")
            )
        except BufferCorruption:
            self._corrupt = True
            raise

    def pending_event_gaps(
        self, source_id: str, stream_id: str, *, limit: int = 100
    ) -> tuple[StreamGap, ...]:
        """Bounded original gap DTOs for a new immutable report batch; no auto-resolution.

        Overlapping evidence is delivered in separate batches. Raw sample losses stay in
        losses(): the current transport contract cannot claim analog-history recovery.
        """
        source_id = _IDENTIFIER.validate_python(source_id)
        stream_id = _IDENTIFIER.validate_python(stream_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Gap batch limit must be in 1..100")
        rows = self._db.execute(
            "SELECT intervals.gap FROM loss_intervals intervals "
            "JOIN missing_ranges loss USING(loss_id) "
            "JOIN batch_ledger owner ON owner.batch_id=loss.batch_id "
            "LEFT JOIN batch_ledger reporter ON reporter.batch_id=intervals.report_batch_id "
            "WHERE owner.source_id=? AND owner.stream_id=? "
            "AND (reporter.batch_id IS NULL OR reporter.state='EVICTED') "
            "ORDER BY intervals.from_seq,intervals.through_seq,loss.rowid",
            (source_id, stream_id),
        )
        gaps = []
        try:
            for row in rows:
                gap = StreamGap.model_validate_json(row[0])
                if gaps and gap.from_seq <= gaps[-1].through_seq:
                    continue
                gaps.append(gap)
                if len(gaps) == limit:
                    break
        except ValueError as error:
            self._corrupt = True
            raise BufferCorruption("Invalid persisted loss interval") from error
        return tuple(gaps)

    def status(self, *, now: datetime) -> BufferStatus:
        now = _time(now)
        counts = dict(
            self._db.execute("SELECT status,count(*) FROM outbound_batches GROUP BY status")
        )
        first = self._db.execute(
            "SELECT queued_at FROM outbound_batches JOIN batch_ledger USING(batch_id) "
            "ORDER BY ordinal LIMIT 1"
        ).fetchone()
        oldest = _time(first[0]) if first else None

        def disk_size(suffix):
            path = Path(str(self.path) + suffix)
            return path.stat().st_size if path.exists() else 0

        return BufferStatus(
            counts.get("PENDING", 0),
            counts.get("INFLIGHT", 0),
            self._queue_bytes(),
            self._metadata_bytes(),
            self._db.execute("SELECT count(*) FROM missing_ranges").fetchone()[0],
            oldest,
            oldest is not None and now - oldest >= self.limits.max_age,
            disk_size(""),
            disk_size("-wal"),
            disk_size("-shm"),
        )

    def _verify(self, *, check_budget=True):
        if self._db.execute("PRAGMA foreign_key_check").fetchone():
            raise BufferCorruption("Invalid buffer foreign keys")
        generation, last = self.capture_position
        for row in self._db.execute("SELECT * FROM outbound_batches"):
            batch = self._row_batch(row).batch
            if batch.body.capture_generation != generation or batch.body.capture_seq > last:
                raise BufferCorruption("Capture position does not cover buffered data")
        try:
            checkpoint_exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_checkpoint'"
            ).fetchone()
            previous = (
                _decode(self._read_checkpoint()["last_capture_seq"]) if checkpoint_exists else 0
            )
            if (
                not checkpoint_exists
                and self._db.execute("PRAGMA user_version").fetchone()[0] != _LEGACY_SCHEMA_VERSION
            ):
                raise BufferCorruption("Missing ledger checkpoint")
            for row in self._db.execute("SELECT * FROM batch_ledger ORDER BY ordinal"):
                capture = _decode(row["capture_seq"])
                if (
                    _decode(row["capture_generation"]) != generation
                    or capture != previous + 1
                    or capture > last
                ):
                    raise BufferCorruption("Invalid immutable capture ledger")
                previous = capture
                _IDENTIFIER.validate_python(row["batch_id"])
                _IDENTIFIER.validate_python(row["source_id"])
                _IDENTIFIER.validate_python(row["stream_id"])
                digest = row["content_sha256"]
                envelope = row["envelope_sha256"]
                if (
                    not isinstance(envelope, str)
                    or len(envelope) != 64
                    or any(char not in "0123456789abcdef" for char in envelope)
                ):
                    raise BufferCorruption("Invalid immutable envelope digest")
                if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise BufferCorruption("Invalid immutable digest")
                queued = self._db.execute(
                    "SELECT 1 FROM outbound_batches WHERE batch_id=?", (row["batch_id"],)
                ).fetchone()
                loss = self._db.execute(
                    "SELECT 1 FROM missing_ranges WHERE batch_id=?", (row["batch_id"],)
                ).fetchone()
                if bool(queued) != (row["state"] == "QUEUED") or bool(loss) != (
                    row["state"] == "EVICTED"
                ):
                    raise BufferCorruption("Ledger state disagrees with payload/loss ownership")
                expected = _ROW_ALLOWANCE + len(row["batch_id"].encode("utf-8")) + 64
                if row["state"] == "ACKNOWLEDGED":
                    ack = self._load_ack(row["ack"])
                    if (
                        ack.batch_id != row["batch_id"]
                        or ack.gateway_id != self.gateway_id
                        or ack.content_sha256 != digest
                        or ack.progress.source_id != row["source_id"]
                        or ack.progress.stream_id != row["stream_id"]
                    ):
                        raise BufferCorruption("Ledger receipt identity mismatch")
                    expected += self._ack_stored_size(row["ack"])
                elif row["ack"] is not None:
                    raise BufferCorruption("Unacknowledged ledger contains a receipt")
                if row["metadata_bytes"] != expected:
                    raise BufferCorruption("Ledger metadata accounting mismatch")
            if previous != last:
                raise BufferCorruption("Capture position disagrees with immutable ledger")
            for row in self._db.execute("SELECT * FROM source_progress"):
                progress = self.progress(row["source_id"], row["stream_id"])
                if (progress.api_progress.source_id, progress.api_progress.stream_id) != (
                    row["source_id"],
                    row["stream_id"],
                ) or row["metadata_bytes"] != len(
                    row["api_progress"].encode("utf-8")
                ) + _ROW_ALLOWANCE:
                    raise BufferCorruption("Stream progress identity/accounting mismatch")
                if row["committed_at"] is not None:
                    _time(row["committed_at"])
            for row in self._db.execute("SELECT * FROM missing_ranges"):
                loss = self._loss_from_json(row["body"])
                ledger = self._db.execute(
                    "SELECT * FROM batch_ledger WHERE batch_id=?", (loss.batch_id,)
                ).fetchone()
                if (
                    loss.loss_id != row["loss_id"]
                    or loss.batch_id != row["batch_id"]
                    or ledger is None
                    or (loss.source_id, loss.stream_id)
                    != (ledger["source_id"], ledger["stream_id"])
                    or row["metadata_bytes"] != self._ack_stored_size(row["body"]) + _ROW_ALLOWANCE
                ):
                    raise BufferCorruption("Loss evidence identity/accounting mismatch")
                expected_gaps = [
                    StreamGap(
                        from_seq=start,
                        through_seq=end,
                        irrecoverable=False,
                        detected_at=loss.detected_at,
                        reason_code=loss.reason_code,
                        reason="Buffered source events require original-history replay",
                        affected_execution_ids=loss.affected_execution_ids,
                    )
                    for start, end in loss.event_ranges
                ] + [StreamGap.model_validate(gap) for gap in loss.reported_gaps]
                intervals = self._db.execute(
                    "SELECT * FROM loss_intervals WHERE loss_id=?", (loss.loss_id,)
                ).fetchall()
                if len(intervals) != len(expected_gaps):
                    raise BufferCorruption("Loss intervals are incomplete")
                for interval in intervals:
                    gap = StreamGap.model_validate_json(interval["gap"])
                    if (
                        gap not in expected_gaps
                        or (_decode(interval["from_seq"]), _decode(interval["through_seq"]))
                        != (gap.from_seq, gap.through_seq)
                        or interval["metadata_bytes"]
                        != len(interval["gap"].encode("utf-8")) + _ROW_ALLOWANCE
                    ):
                        raise BufferCorruption("Loss interval disagrees with evidence")
                    if interval["report_batch_id"] is not None:
                        reporter = self._db.execute(
                            "SELECT * FROM batch_ledger WHERE batch_id=?",
                            (interval["report_batch_id"],),
                        ).fetchone()
                        if (reporter["source_id"], reporter["stream_id"]) != (
                            loss.source_id,
                            loss.stream_id,
                        ):
                            raise BufferCorruption("Loss reporter belongs to another stream")
                        if reporter["state"] == "QUEUED":
                            payload = self._db.execute(
                                "SELECT body FROM outbound_batches WHERE batch_id=?",
                                (reporter["batch_id"],),
                            ).fetchone()
                            reported = GatewayBatch.model_validate_json(payload[0]).body.gaps
                        elif reporter["state"] == "ACKNOWLEDGED":
                            reported = self._load_ack(reporter["ack"]).recorded_gaps
                        else:
                            reporter_loss = self._db.execute(
                                "SELECT body FROM missing_ranges WHERE batch_id=?",
                                (reporter["batch_id"],),
                            ).fetchone()
                            reported = tuple(
                                StreamGap.model_validate(item)
                                for item in self._loss_from_json(reporter_loss[0]).reported_gaps
                            )
                        if gap not in reported:
                            raise BufferCorruption("Loss reporter omitted its owned gap")
        except (ValueError, TypeError, KeyError) as error:
            raise BufferCorruption("Invalid persisted buffer ledger") from error
        if check_budget:
            self._check_metadata_budget()
