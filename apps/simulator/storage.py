"""Single-owner durable process snapshots and production events, without PLC/auth/transport."""

from __future__ import annotations

import fcntl
import hashlib
import itertools
import math
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, TypeAdapter

from apps.simulator.process import (
    ItemDelta,
    ProcessState,
    StepResult,
    fresh_execution,
    scrap,
    step,
)
from contracts.models import (
    EVENT_ADAPTER,
    UINT64_MAX,
    Event,
    EventPage,
    EventReadError,
    FaultEvent,
    FaultPayload,
    Identifier,
    ProductionEvent,
    ReadEventsRequest,
    ScrapEvent,
    StateEvent,
    StatePayload,
    UTCTimestamp,
)

_IDENTIFIER = TypeAdapter(Identifier)
_TIMESTAMP = TypeAdapter(UTCTimestamp)
_QUANTITIES = ("input_quantity", "good_quantity", "reject_quantity", "scrap_quantity")
_PROJECTIONS = (*_QUANTITIES, "wip_quantity", "decision_cursor", "tick")
_SCHEMA_VERSION = 3
_RETENTION = timedelta(days=7)
_RETENTION_SECONDS = _RETENTION.total_seconds()
_CLOCK_DRIFT_SECONDS = 2.0


def encode_uint64(value: int) -> str:
    if type(value) is not int or not 0 <= value <= UINT64_MAX:
        raise ValueError("Expected an exact UInt64 integer")
    return f"{value:020d}"


def decode_uint64(value: str) -> int:
    if (
        not isinstance(value, str)
        or len(value) != 20
        or not value.isascii()
        or not value.isdecimal()
        or value > str(UINT64_MAX)
    ):
        raise ValueError("Invalid fixed-width SQLite UInt64")
    return int(value)


def _uint_column(name: str, *, nullable: bool = False) -> str:
    required = "" if nullable else "NOT NULL "
    null_check = f"{name} IS NULL OR " if nullable else ""
    return (
        f"{name} TEXT {required}COLLATE BINARY CHECK({null_check}(typeof({name})='text' "
        f"AND length({name})=20 AND {name} NOT GLOB '*[^0-9]*' "
        f"AND {name}<='18446744073709551615'))"
    )


@dataclass(frozen=True)
class SimulatorIdentity:
    source_id: str
    stream_id: str
    counter_epoch: str


class _ControlEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    equipment_id: Identifier = "Line"
    execution_id: Identifier | None = None
    occurred_at: UTCTimestamp


class StateEventInput(_ControlEventInput):
    kind: Literal["STATE_CHANGED"] = "STATE_CHANGED"
    payload: StatePayload


class FaultEventInput(_ControlEventInput):
    kind: Literal["FAULT_CHANGED"] = "FAULT_CHANGED"
    payload: FaultPayload


class RecoveryRequired(ValueError):
    pass


class StorageCorruption(ValueError):
    pass


class RetentionBlocked(ValueError):
    """Maintenance is conservatively blocked; source collection may continue."""


@dataclass(frozen=True)
class RetentionResult:
    pruned_count: int
    pruned_through_seq: int
    retained_from_seq: int


class SimulatorStorage:
    """Own one SQLite file on a local POSIX filesystem; no background ticking occurs."""

    def __init__(
        self,
        path: str | Path,
        *,
        source_id: str | None = None,
        stream_id: str | None = None,
        counter_epoch: str | None = None,
        utc_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if str(path) == ":memory:":
            raise ValueError("Simulator storage requires a durable file")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_nlink != 1:
            raise ValueError(
                "Hardlinked SQLite files are unsupported; use one canonical database file"
            )
        self._connection: sqlite3.Connection | None = None
        self._transaction_depth = 0
        self._transaction_generation = 0
        self._commit_callbacks = []
        self._transaction_corrupt = False
        self._utc_clock = utc_clock
        self._monotonic_clock = monotonic_clock
        self._utc_anchor = _TIMESTAMP.validate_python(utc_clock())
        self._monotonic_anchor = monotonic_clock()
        if type(self._monotonic_anchor) not in (int, float) or not math.isfinite(
            self._monotonic_anchor
        ):
            raise ValueError("Monotonic clock must return a finite number")
        self._clock_elapsed = 0.0
        self._retention_clock_frozen = False
        self._lock = os.open(str(self.path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not self.path.exists():
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
            self._connection = sqlite3.connect(self.path, isolation_level=None, timeout=5)
            self._connection.row_factory = sqlite3.Row
            self._connection.create_function("retention_recorded_at", 0, self._recorded_at)
            self._connection.create_function(
                "retention_body_hash",
                1,
                lambda body: hashlib.sha256(body.encode("utf-8")).hexdigest(),
                deterministic=True,
            )
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._initialize_schema()
            with self._transaction():
                row = self._db.execute(
                    "SELECT * FROM simulator_identity WHERE singleton=1"
                ).fetchone()
                supplied = (source_id, stream_id, counter_epoch)
                if row is None:
                    values = tuple(
                        _IDENTIFIER.validate_python(value if value is not None else str(uuid4()))
                        for value in supplied
                    )
                    self._db.execute(
                        "INSERT INTO simulator_identity "
                        "(singleton,source_id,stream_id,counter_epoch,last_seq,retained_from_seq,"
                        "recovery_required) VALUES (1,?,?,?,?,?,1)",
                        (*values, encode_uint64(0), encode_uint64(1)),
                    )
                else:
                    values = tuple(
                        row[name] for name in ("source_id", "stream_id", "counter_epoch")
                    )
                    if any(
                        value is not None and value != old
                        for value, old in zip(supplied, values, strict=True)
                    ):
                        raise ValueError("Existing simulator identity cannot be replaced on open")
                    for value in values:
                        _IDENTIFIER.validate_python(value)
                    self._db.execute(
                        "UPDATE simulator_identity SET recovery_required=1 WHERE singleton=1"
                    )
                self.identity = SimulatorIdentity(*values)
                self._bind_retention_identity(bootstrap=row is None)
        except BaseException:
            self.close()
            raise

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise ValueError("Simulator storage is closed")
        return self._connection

    def _initialize_schema(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version == _SCHEMA_VERSION:
            return
        if version in (1, 2):
            # Both migration stages and all extension tables share the outer commit.
            with self.transaction():
                if version == 1:
                    self._migrate_schema_v1()
                self._migrate_schema_v2()
            return
        if (
            version != 0
            or self._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
        ):
            raise ValueError("Unsupported SQLite schema; explicit migration is required")
        projections = ",".join(_uint_column(name) for name in _PROJECTIONS)
        self._db.executescript(
            f"""
            BEGIN IMMEDIATE;
            CREATE TABLE executions (
                execution_id TEXT PRIMARY KEY NOT NULL,
                snapshot TEXT NOT NULL,
                {projections}
            );
            CREATE TABLE simulator_identity (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                source_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                counter_epoch TEXT NOT NULL,
                {_uint_column("last_seq")},
                {_uint_column("retained_from_seq")},
                active_execution_id TEXT REFERENCES executions(execution_id) ON DELETE RESTRICT,
                recovery_required INTEGER NOT NULL CHECK(recovery_required IN (0,1)),
                CHECK(retained_from_seq>'00000000000000000000'),
                UNIQUE(source_id,stream_id)
            );
            {self._events_table_sql("source_events")};
            CREATE INDEX ix_events_execution ON source_events(execution_id,seq);
            PRAGMA user_version=2;
            COMMIT;
            """
        )
        self._migrate_schema_v2()

    @staticmethod
    def _events_table_sql(name: str) -> str:
        return f"""
            CREATE TABLE {name} (
                event_id TEXT PRIMARY KEY NOT NULL,
                source_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                {_uint_column("seq")},
                execution_id TEXT REFERENCES executions(execution_id) ON DELETE RESTRICT,
                {_uint_column("item_id", nullable=True)},
                body TEXT NOT NULL,
                CHECK(seq>'00000000000000000000'),
                CHECK(item_id IS NULL OR item_id>'00000000000000000000'),
                CHECK(json_valid(body)),
                CHECK(CASE json_extract(body,'$.kind')
                    WHEN 'PRODUCTION_GOOD' THEN execution_id IS NOT NULL AND item_id IS NOT NULL
                    WHEN 'PRODUCTION_REJECT' THEN execution_id IS NOT NULL AND item_id IS NOT NULL
                    WHEN 'WIP_SCRAP' THEN execution_id IS NOT NULL AND item_id IS NOT NULL
                    WHEN 'STATE_CHANGED' THEN item_id IS NULL
                    WHEN 'FAULT_CHANGED' THEN item_id IS NULL
                    ELSE 0 END),
                UNIQUE(source_id,stream_id,seq),
                UNIQUE(execution_id,item_id),
                FOREIGN KEY(source_id,stream_id)
                    REFERENCES simulator_identity(source_id,stream_id) ON DELETE RESTRICT
            )
        """

    def _migrate_schema_v1(self) -> None:
        """Rebuild only the event table; SQLite DDL and user_version commit together."""
        with self.transaction():
            self._db.execute(self._events_table_sql("source_events_v2"))
            self._db.execute(
                "INSERT INTO source_events_v2 "
                "SELECT event_id,source_id,stream_id,seq,execution_id,item_id,body "
                "FROM source_events"
            )
            self._db.execute("DROP TABLE source_events")
            self._db.execute("ALTER TABLE source_events_v2 RENAME TO source_events")
            self._db.execute("CREATE INDEX ix_events_execution ON source_events(execution_id,seq)")
            self._db.execute("PRAGMA user_version=2")

    def _before_retention_migration_commit(self) -> None:
        """Internal failure seam inside the atomic v2-to-v3 migration."""

    def _migrate_schema_v2(self) -> None:
        """Add retained-prefix evidence without inventing missing historical events."""
        with self.transaction():
            identity = self._db.execute(
                "SELECT * FROM simulator_identity WHERE singleton=1"
            ).fetchone()
            if identity is not None and decode_uint64(identity["retained_from_seq"]) != 1:
                raise StorageCorruption("Cannot migrate an unsupported deleted source prefix")
            statements = (
                "CREATE TABLE retention_checkpoint (singleton INTEGER PRIMARY KEY "
                "CHECK(singleton=1),source_id TEXT NOT NULL,stream_id TEXT NOT NULL,"
                f"counter_epoch TEXT NOT NULL,{_uint_column('pruned_through_seq')})",
                "CREATE TABLE execution_checkpoints (execution_id TEXT PRIMARY KEY "
                "REFERENCES executions(execution_id) ON DELETE RESTRICT,"
                + ",".join(_uint_column(name) for name in ("good", "reject", "scrap"))
                + ")",
                "CREATE TABLE archived_event_keys (event_id TEXT PRIMARY KEY NOT NULL,"
                f"{_uint_column('seq')},execution_id TEXT REFERENCES executions(execution_id) "
                f"ON DELETE RESTRICT,{_uint_column('item_id', nullable=True)},kind TEXT NOT NULL,"
                "body_sha256 TEXT NOT NULL CHECK(length(body_sha256)=64),"
                "UNIQUE(seq),UNIQUE(execution_id,item_id),"
                "CHECK(seq>'00000000000000000000'),"
                "CHECK(item_id IS NULL OR item_id>'00000000000000000000'),"
                "CHECK(CASE kind WHEN 'PRODUCTION_GOOD' THEN execution_id IS NOT NULL "
                "AND item_id IS NOT NULL WHEN 'PRODUCTION_REJECT' THEN execution_id IS NOT NULL "
                "AND item_id IS NOT NULL WHEN 'WIP_SCRAP' THEN execution_id IS NOT NULL "
                "AND item_id IS NOT NULL WHEN 'STATE_CHANGED' THEN item_id IS NULL "
                "WHEN 'FAULT_CHANGED' THEN item_id IS NULL ELSE 0 END))",
                "CREATE TABLE event_retention (event_id TEXT PRIMARY KEY NOT NULL REFERENCES "
                "source_events(event_id) ON DELETE CASCADE, recorded_at TEXT NOT NULL)",
                "CREATE TRIGGER source_events_archive_collision BEFORE INSERT ON source_events "
                "WHEN EXISTS (SELECT 1 FROM archived_event_keys WHERE event_id=NEW.event_id "
                "OR seq=NEW.seq OR (execution_id=NEW.execution_id AND item_id=NEW.item_id)) "
                "BEGIN SELECT RAISE(ABORT,'Archived source event/item collision'); END",
                "CREATE TRIGGER source_events_recorded AFTER INSERT ON source_events BEGIN "
                "INSERT INTO event_retention VALUES(NEW.event_id,retention_recorded_at()); END",
                "CREATE TRIGGER archived_keys_match_live BEFORE INSERT ON archived_event_keys "
                "WHEN NOT EXISTS (SELECT 1 FROM source_events WHERE event_id=NEW.event_id "
                "AND seq=NEW.seq AND execution_id IS NEW.execution_id AND item_id IS NEW.item_id "
                "AND json_extract(body,'$.kind')=NEW.kind "
                "AND retention_body_hash(body)=NEW.body_sha256) "
                "BEGIN SELECT RAISE(ABORT,'Archive requires original matching source event'); END",
                "CREATE TRIGGER archived_keys_no_update BEFORE UPDATE ON archived_event_keys "
                "BEGIN SELECT RAISE(ABORT,'Archived event keys are immutable'); END",
                "CREATE TRIGGER archived_keys_no_delete BEFORE DELETE ON archived_event_keys "
                "BEGIN SELECT RAISE(ABORT,'Archived event keys are permanent'); END",
                "CREATE TRIGGER retention_identity_no_update BEFORE UPDATE OF "
                "source_id,stream_id,counter_epoch ON retention_checkpoint "
                "BEGIN SELECT RAISE(ABORT,'Retention identity is immutable'); END",
                "CREATE TRIGGER retention_age_no_update BEFORE UPDATE ON event_retention "
                "BEGIN SELECT RAISE(ABORT,'Event retention age is immutable'); END",
            )
            for statement in statements:
                self._db.execute(statement)
            if identity is not None:
                self._db.execute(
                    "INSERT INTO retention_checkpoint VALUES (1,?,?,?,?)",
                    (
                        *(identity[key] for key in ("source_id", "stream_id", "counter_epoch")),
                        encode_uint64(0),
                    ),
                )
            # Legacy rows get a new complete protection week, never an invented old age.
            self._db.execute(
                "INSERT INTO event_retention SELECT event_id,? FROM source_events",
                (self._utc_anchor.isoformat(),),
            )
            self._db.execute("PRAGMA user_version=3")
            self._before_retention_migration_commit()

    def _bind_retention_identity(self, *, bootstrap: bool) -> None:
        row = self._db.execute("SELECT * FROM retention_checkpoint WHERE singleton=1").fetchone()
        expected = (self.identity.source_id, self.identity.stream_id, self.identity.counter_epoch)
        if row is None:
            if not bootstrap or any(
                self._db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                for table in ("source_events", "archived_event_keys", "executions")
            ):
                raise StorageCorruption("Missing immutable retention identity binding")
            self._db.execute(
                "INSERT INTO retention_checkpoint VALUES (1,?,?,?,?)",
                (*expected, encode_uint64(0)),
            )
        elif tuple(row[key] for key in ("source_id", "stream_id", "counter_epoch")) != expected:
            raise StorageCorruption("Retention checkpoint belongs to another source/stream/epoch")

    def _retention_time(self) -> datetime:
        """A fixed UTC anchor advanced only by this owner's monotonic elapsed time."""
        try:
            observed = self._monotonic_clock()
            if type(observed) not in (int, float) or not math.isfinite(observed):
                raise ValueError("Invalid monotonic clock")
            elapsed = observed - self._monotonic_anchor
            if elapsed < self._clock_elapsed:
                raise ValueError("Monotonic clock regressed")
            effective = self._utc_anchor + timedelta(seconds=elapsed)
            self._clock_elapsed = elapsed
            actual = _TIMESTAMP.validate_python(self._utc_clock())
            if abs((actual - effective).total_seconds()) > _CLOCK_DRIFT_SECONDS:
                raise ValueError("UTC clock disagrees with monotonic elapsed time")
        except (ValueError, TypeError, OverflowError):
            self._retention_clock_frozen = True
        return self._utc_anchor + timedelta(seconds=self._clock_elapsed)

    def _recorded_at(self) -> str:
        # Clock disagreement freezes deletion only; normal source collection continues.
        return self._retention_time().isoformat()

    @property
    def connection(self) -> sqlite3.Connection:
        """Internal component connection; writes must use transaction(), never commit directly."""
        return self._db

    @property
    def transaction_generation(self) -> int:
        """Process-local outer transaction identity; never a persisted control version."""
        return self._transaction_generation

    @contextmanager
    def transaction(self):
        """Compose process and component writes atomically; nested units use savepoints.

        Returned events/results are provisional until the outer context exits successfully.
        A detected storage corruption poisons the entire unit, even if caught by a caller.
        """
        outer = self._transaction_depth == 0
        if outer and self._db.in_transaction:
            raise ValueError("Use SimulatorStorage.transaction for transaction ownership")
        marker = f"simulator_unit_{self._transaction_depth}"
        callback_start = len(self._commit_callbacks)
        if outer:
            self._transaction_corrupt = False
            self._db.execute("BEGIN IMMEDIATE")
            self._transaction_generation += 1
        else:
            self._db.execute(f"SAVEPOINT {marker}")
        self._transaction_depth += 1
        try:
            yield self._db
            if self._transaction_corrupt:
                raise StorageCorruption("Transaction encountered storage corruption")
            if outer:
                self._db.commit()
            else:
                self._db.execute(f"RELEASE SAVEPOINT {marker}")
        except BaseException as error:
            del self._commit_callbacks[callback_start:]
            if isinstance(error, StorageCorruption):
                self._transaction_corrupt = True
            if outer:
                self._db.rollback()
                if self._transaction_corrupt:
                    self._db.execute(
                        "UPDATE simulator_identity SET recovery_required=1 WHERE singleton=1"
                    )
            else:
                self._db.execute(f"ROLLBACK TO SAVEPOINT {marker}")
                self._db.execute(f"RELEASE SAVEPOINT {marker}")
            raise
        finally:
            self._transaction_depth -= 1
        if outer:
            callbacks, self._commit_callbacks = self._commit_callbacks, []
            for callback in callbacks:
                callback()

    def _transaction(self):
        return self.transaction()

    def __enter__(self) -> SimulatorStorage:
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if getattr(self, "_lock", None) is not None:
            os.close(self._lock)
            self._lock = None

    @property
    def recovery_required(self) -> bool:
        return bool(
            self._db.execute(
                "SELECT recovery_required FROM simulator_identity WHERE singleton=1"
            ).fetchone()[0]
        )

    def _require_recovered(self) -> None:
        if self.recovery_required:
            raise RecoveryRequired(
                "Validate and explicitly acknowledge recovery before changing state"
            )

    def mark_corrupt(self) -> None:
        """Latch component corruption, including when the caller catches its exception."""
        if self._transaction_depth:
            self._transaction_corrupt = True
        else:
            self._db.execute("UPDATE simulator_identity SET recovery_required=1 WHERE singleton=1")

    @staticmethod
    def _validated_state(state: ProcessState) -> ProcessState:
        if not isinstance(state, ProcessState):
            raise ValueError("Expected a ProcessState")
        restored = ProcessState.from_json(state.to_json())
        _IDENTIFIER.validate_python(restored.execution_id)
        for name in _PROJECTIONS:
            encode_uint64(getattr(restored, name))
        for item in restored.slots:
            if item is not None:
                encode_uint64(item.item_id)
        return restored

    @staticmethod
    def _row_state(row: sqlite3.Row) -> ProcessState:
        try:
            state = SimulatorStorage._validated_state(ProcessState.from_json(row["snapshot"]))
            if state.execution_id != row["execution_id"] or any(
                decode_uint64(row[name]) != getattr(state, name) for name in _PROJECTIONS
            ):
                raise StorageCorruption("Snapshot and execution counter projections differ")
            return state
        except StorageCorruption:
            raise
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise StorageCorruption(
                "Invalid persisted execution snapshot or projections"
            ) from error

    def _execution_states(self) -> tuple[dict[str, ProcessState], str | None]:
        states = {
            row["execution_id"]: self._row_state(row)
            for row in self._db.execute("SELECT * FROM executions")
        }
        active_id = self._db.execute(
            "SELECT active_execution_id FROM simulator_identity WHERE singleton=1"
        ).fetchone()[0]
        if (states and active_id not in states) or (
            active_id is not None and active_id not in states
        ):
            raise StorageCorruption("Active execution snapshot is missing")
        if any(state.wip_quantity for key, state in states.items() if key != active_id):
            raise StorageCorruption("Inactive execution still owns WIP")
        return states, active_id

    def _event_from_row(self, row: sqlite3.Row, states: dict[str, ProcessState]) -> Event:
        try:
            return self._checked_event_from_row(row, states)
        except StorageCorruption:
            raise
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise StorageCorruption("Invalid persisted source event") from error

    def _checked_event_from_row(self, row: sqlite3.Row, states: dict[str, ProcessState]) -> Event:
        recorded = self._db.execute(
            "SELECT recorded_at FROM event_retention WHERE event_id=?", (row["event_id"],)
        ).fetchone()
        if recorded is None:
            raise StorageCorruption("Source event is missing durable retention age")
        _TIMESTAMP.validate_python(recorded[0])
        event = EVENT_ADAPTER.validate_json(row["body"])
        if (
            event.seq != decode_uint64(row["seq"])
            or event.event_id != row["event_id"]
            or event.source_id != row["source_id"]
            or event.stream_id != row["stream_id"]
            or event.source_id != self.identity.source_id
            or event.stream_id != self.identity.stream_id
            or event.execution_id != row["execution_id"]
            or (event.execution_id is not None and event.execution_id not in states)
        ):
            raise StorageCorruption("Event identity or sequence does not match storage")
        if isinstance(event, (StateEvent, FaultEvent)):
            if row["item_id"] is not None:
                raise StorageCorruption("Control events cannot dispose a production item")
            return event
        if (
            event.payload.counter_epoch != self.identity.counter_epoch
            or event.payload.quantity != 1
            or event.equipment_id != ("Line" if event.kind == "WIP_SCRAP" else "Outfeed")
        ):
            raise StorageCorruption("Event identity, quantity or sequence does not match storage")
        state = states[event.execution_id]
        item_id = decode_uint64(row["item_id"])
        if not 1 <= item_id <= state.input_quantity or any(
            item is not None and item.item_id == item_id for item in state.slots
        ):
            raise StorageCorruption("Disposed item conflicts with execution snapshot")
        return event

    def update_process_parameters(
        self,
        previous: ProcessState,
        *,
        temperature_c: float,
        speed_m_per_min: float,
    ) -> ProcessState:
        """Persist only setpoint/speed; caller enforces PLC state, limits and authorization."""
        previous = self._validated_state(previous)
        config = replace(
            previous.config,
            temperature_sp_c=temperature_c,
            speed_m_per_min=speed_m_per_min,
        )
        updated = self._validated_state(replace(previous, config=config))
        with self.transaction():
            self._require_recovered()
            states, active_id = self._execution_states()
            if states.get(active_id) != previous:
                raise ValueError("Stale or non-active execution snapshot")
            self._save_state(updated)
        return updated

    def append_control_events(
        self, descriptors: tuple[StateEventInput | FaultEventInput, ...]
    ) -> tuple[StateEvent | FaultEvent, ...]:
        """Allocate source identity/UUID/sequence for typed control events in this unit."""
        if type(descriptors) is not tuple or any(
            not isinstance(value, (StateEventInput, FaultEventInput)) for value in descriptors
        ):
            raise ValueError("Expected a tuple of typed control event descriptors")
        # Revalidate even model_construct/model_copy instances and mutable nested payloads.
        descriptors = tuple(
            type(value).model_validate(value.model_dump(mode="python", warnings=False))
            for value in descriptors
        )
        with self.transaction():
            # Safety/restart history may be recorded while motion remains recovery-gated.
            states, _ = self._execution_states()
            _, last_seq = self._bounds()
            if len(descriptors) > UINT64_MAX - last_seq:
                raise ValueError("Event sequence exhausted; never wrap or silently replace stream")
            events = []
            for offset, value in enumerate(descriptors, 1):
                if value.execution_id is not None and value.execution_id not in states:
                    raise ValueError("Control event references an unknown execution")
                event_type = StateEvent if isinstance(value, StateEventInput) else FaultEvent
                event = event_type(
                    **value.model_dump(mode="python"),
                    event_id=str(uuid4()),
                    source_id=self.identity.source_id,
                    stream_id=self.identity.stream_id,
                    seq=last_seq + offset,
                )
                self._db.execute(
                    "INSERT INTO source_events "
                    "(event_id,source_id,stream_id,seq,execution_id,item_id,body) "
                    "VALUES (?,?,?,?,?,NULL,?)",
                    (
                        event.event_id,
                        event.source_id,
                        event.stream_id,
                        encode_uint64(event.seq),
                        event.execution_id,
                        event.model_dump_json(),
                    ),
                )
                events.append(event)
            self._db.execute(
                "UPDATE simulator_identity SET last_seq=? WHERE singleton=1",
                (encode_uint64(last_seq + len(events)),),
            )
        return tuple(events)

    def load_execution(self, execution_id: str | None = None) -> ProcessState | None:
        if execution_id is None:
            execution_id = self._db.execute(
                "SELECT active_execution_id FROM simulator_identity WHERE singleton=1"
            ).fetchone()[0]
        row = self._db.execute(
            "SELECT * FROM executions WHERE execution_id=?", (execution_id,)
        ).fetchone()
        try:
            return None if row is None else self._row_state(row)
        except StorageCorruption:
            self.mark_corrupt()
            raise

    def _save_state(self, state: ProcessState, *, insert: bool = False) -> None:
        values = [state.to_json(), *(encode_uint64(getattr(state, name)) for name in _PROJECTIONS)]
        if insert:
            names = ",".join(("snapshot", *_PROJECTIONS, "execution_id"))
            placeholders = ",".join("?" for _ in range(len(values) + 1))
            self._db.execute(
                f"INSERT INTO executions ({names}) VALUES ({placeholders})",
                (*values, state.execution_id),
            )
        else:
            assignments = ",".join(f"{name}=?" for name in ("snapshot", *_PROJECTIONS))
            self._db.execute(
                f"UPDATE executions SET {assignments} WHERE execution_id=?",
                (*values, state.execution_id),
            )

    def register_execution(self, state: ProcessState) -> None:
        """Persist an explicit fresh empty execution; this is not PLC LOAD_JOB authorization."""
        state = self._validated_state(state)
        with self._transaction():
            self._require_recovered()
            states, active_id = self._execution_states()
            previous = states.get(active_id)
            expected = fresh_execution(
                previous, state.execution_id, config=state.config, seed=state.rng_state
            )
            if expected != state:
                raise ValueError("Only an empty fresh_execution snapshot can be registered")
            self._save_state(state, insert=True)
            self._db.execute(
                "UPDATE simulator_identity SET active_execution_id=? WHERE singleton=1",
                (state.execution_id,),
            )

    @staticmethod
    def _validate_transition(previous: ProcessState, result: StepResult) -> None:
        if not isinstance(result, StepResult) or type(result.deltas) is not tuple:
            raise ValueError("Expected an immutable StepResult")
        SimulatorStorage._validated_state(result.state)
        for delta in result.deltas:
            if not isinstance(delta, ItemDelta):
                raise ValueError("Expected typed item deltas")
            encode_uint64(delta.item_id)
            _IDENTIFIER.validate_python(delta.execution_id)
            if delta.item_id == 0 or delta.outcome not in {"GOOD", "REJECT", "SCRAP"}:
                raise ValueError("Invalid item disposition")
        # Reuse the pure process functions; this checks consistency, never final PLC permission.
        if result.deltas and all(delta.outcome == "SCRAP" for delta in result.deltas):
            if len({delta.reason for delta in result.deltas}) == 1:
                candidate = scrap(
                    previous,
                    tuple(delta.item_id for delta in result.deltas),
                    reason=result.deltas[0].reason,
                )
                if candidate == result:
                    return
        else:
            for run, admit, heater_on in itertools.product((False, True), repeat=3):
                try:
                    candidate = step(previous, run=run, admit=admit, heater_on=heater_on)
                except ValueError:
                    continue
                if candidate == result:
                    return
        raise ValueError("Deltas and snapshot are not one consistent process transition")

    def _before_transition_commit(self) -> None:
        """Internal fault-injection seam; no external endpoint exposes it."""

    def _after_transition_commit(self) -> None:
        """Internal fault-injection seam; state is already durable when called."""

    def apply_transition(
        self, previous: ProcessState, result: StepResult, *, occurred_at: datetime
    ) -> tuple[ProductionEvent | ScrapEvent, ...]:
        previous = self._validated_state(previous)
        self._validate_transition(previous, result)
        timestamp = _TIMESTAMP.validate_python(occurred_at)
        with self._transaction():
            self._require_recovered()
            states, active_id = self._execution_states()
            if states.get(active_id) != previous:
                raise ValueError("Stale or non-active execution snapshot")
            last_seq = decode_uint64(
                self._db.execute(
                    "SELECT last_seq FROM simulator_identity WHERE singleton=1"
                ).fetchone()[0]
            )
            if len(result.deltas) > UINT64_MAX - last_seq:
                raise ValueError("Event sequence exhausted; never wrap or silently replace stream")
            events = []
            for offset, delta in enumerate(result.deltas, 1):
                common = {
                    "event_id": str(uuid4()),
                    "source_id": self.identity.source_id,
                    "stream_id": self.identity.stream_id,
                    "seq": last_seq + offset,
                    "execution_id": delta.execution_id,
                    "occurred_at": timestamp,
                }
                payload = {"quantity": 1, "counter_epoch": self.identity.counter_epoch}
                if delta.outcome == "SCRAP":
                    event = ScrapEvent(
                        **common,
                        equipment_id="Line",
                        kind="WIP_SCRAP",
                        payload={
                            **payload,
                            "reason_code": "EXPLICIT_WIP_SCRAP",
                            "reason": delta.reason,
                        },
                    )
                else:
                    event = ProductionEvent(
                        **common,
                        equipment_id="Outfeed",
                        kind=f"PRODUCTION_{delta.outcome}",
                        payload=payload,
                    )
                self._db.execute(
                    "INSERT INTO source_events "
                    "(event_id,source_id,stream_id,seq,execution_id,item_id,body) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        event.event_id,
                        event.source_id,
                        event.stream_id,
                        encode_uint64(event.seq),
                        event.execution_id,
                        encode_uint64(delta.item_id),
                        event.model_dump_json(),
                    ),
                )
                events.append(event)
            self._save_state(result.state)
            self._db.execute(
                "UPDATE simulator_identity SET last_seq=? WHERE singleton=1",
                (encode_uint64(last_seq + len(events)),),
            )
            self._before_transition_commit()
            self._commit_callbacks.append(self._after_transition_commit)
        return tuple(events)

    def _bounds(self) -> tuple[int, int]:
        row = self._db.execute(
            "SELECT retained_from_seq,last_seq FROM simulator_identity WHERE singleton=1"
        ).fetchone()
        try:
            return decode_uint64(row[0]), decode_uint64(row[1])
        except (ValueError, TypeError) as error:
            raise StorageCorruption("Invalid persisted source bounds") from error

    def read_events(self, request: ReadEventsRequest) -> EventPage | EventReadError:
        request = ReadEventsRequest.model_validate(request)
        self._db.execute("BEGIN")
        try:
            self._require_recovered()
            first, last = self._bounds()
            common = {
                "source_id": self.identity.source_id,
                "stream_id": self.identity.stream_id,
                "after_seq": request.after_seq,
                "retained_from_seq": first,
                "retained_through_seq": last,
            }
            if (request.source_id, request.stream_id) != (
                self.identity.source_id,
                self.identity.stream_id,
            ):
                response = EventReadError(
                    **common,
                    code="STREAM_MISMATCH",
                    reason="Requested source/stream does not match",
                )
            elif request.after_seq > last:
                response = EventReadError(
                    **common, code="CURSOR_AHEAD", reason="Requested cursor is beyond source head"
                )
            elif request.after_seq + 1 < first:
                response = EventReadError(
                    **common,
                    code="RETENTION_GAP",
                    missing_from_seq=request.after_seq + 1,
                    missing_through_seq=first - 1,
                    reason="Requested events precede retained source history",
                )
            else:
                rows = self._db.execute(
                    "SELECT * FROM source_events WHERE seq>? ORDER BY seq LIMIT ?",
                    (encode_uint64(request.after_seq), request.limit),
                ).fetchall()
                states, _ = self._execution_states()
                events = tuple(self._event_from_row(row, states) for row in rows)
                page_last = events[-1].seq if events else request.after_seq
                response = EventPage(
                    **common,
                    events=events,
                    page_last_seq=page_last,
                    has_more=page_last < last,
                )
            self._db.commit()
            return response
        except BaseException:
            self._db.rollback()
            self._db.execute("UPDATE simulator_identity SET recovery_required=1 WHERE singleton=1")
            raise

    def acknowledge_recovery(self) -> None:
        """Internal consistency gate only; does not authorize motion or replace PLC recovery."""
        if not self._transaction_depth and self._db.in_transaction:
            raise ValueError("Use SimulatorStorage.transaction for transaction ownership")
        # Latch before checking, so a failed recheck also leaves an already-open store gated.
        self._db.execute("UPDATE simulator_identity SET recovery_required=1 WHERE singleton=1")
        with self._transaction():
            states, _ = self._execution_states()
            first, last = self._bounds()
            if not 1 <= first <= last + 1:
                raise StorageCorruption("Invalid retained sequence bounds")
            expected = first
            totals = self._retention_totals(states, first)
            for row in self._db.execute("SELECT * FROM source_events ORDER BY seq"):
                event = self._event_from_row(row, states)
                if event.seq != expected:
                    raise StorageCorruption("Retained source events are not consecutive")
                if isinstance(event, (ProductionEvent, ScrapEvent)):
                    kind = (
                        "SCRAP"
                        if event.kind == "WIP_SCRAP"
                        else event.kind.removeprefix("PRODUCTION_")
                    )
                    totals[event.execution_id][kind] += 1
                expected += 1
            if expected != last + 1:
                raise StorageCorruption("Source head does not match retained events")
            for execution_id, state in states.items():
                if (
                    totals[execution_id]["GOOD"],
                    totals[execution_id]["REJECT"],
                    totals[execution_id]["SCRAP"],
                ) != (state.good_quantity, state.reject_quantity, state.scrap_quantity):
                    raise StorageCorruption(
                        "Snapshot production counters do not match durable events"
                    )
            self._db.execute("UPDATE simulator_identity SET recovery_required=0 WHERE singleton=1")

    def _retention_totals(
        self, states: dict[str, ProcessState], first: int
    ) -> dict[str, dict[str, int]]:
        """Independently compare compact archive keys, checkpoint counters and live keys."""
        try:
            row = self._db.execute("SELECT * FROM retention_checkpoint").fetchall()
            if len(row) != 1 or row[0]["singleton"] != 1:
                raise StorageCorruption("Invalid retention checkpoint ownership")
            through = decode_uint64(row[0]["pruned_through_seq"])
            names = ("source_id", "stream_id", "counter_epoch")
            bound_identity = tuple(_IDENTIFIER.validate_python(row[0][key]) for key in names)
            current_identity = self._db.execute(
                "SELECT source_id,stream_id,counter_epoch FROM simulator_identity WHERE singleton=1"
            ).fetchone()
            if bound_identity != tuple(current_identity) or bound_identity != (
                self.identity.source_id,
                self.identity.stream_id,
                self.identity.counter_epoch,
            ):
                raise StorageCorruption("Retention checkpoint identity changed")
            if through != first - 1:
                raise StorageCorruption("Retention checkpoint does not match retained prefix")
            totals = {key: dict.fromkeys(("GOOD", "REJECT", "SCRAP"), 0) for key in states}
            expected = 1
            for archived in self._db.execute("SELECT * FROM archived_event_keys ORDER BY seq"):
                sequence = decode_uint64(archived["seq"])
                if sequence != expected or sequence > through:
                    raise StorageCorruption("Archived source prefix is not consecutive")
                expected += 1
                _IDENTIFIER.validate_python(archived["event_id"])
                execution = archived["execution_id"]
                if execution is not None and execution not in states:
                    raise StorageCorruption("Archived event references missing execution")
                digest = archived["body_sha256"]
                if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise StorageCorruption("Invalid archived body digest")
                kind = archived["kind"]
                if kind in ("STATE_CHANGED", "FAULT_CHANGED"):
                    if archived["item_id"] is not None:
                        raise StorageCorruption("Archived control event disposes an item")
                elif kind in ("PRODUCTION_GOOD", "PRODUCTION_REJECT", "WIP_SCRAP"):
                    state = states[execution]
                    item_id = decode_uint64(archived["item_id"])
                    if not 1 <= item_id <= state.input_quantity or any(
                        item is not None and item.item_id == item_id for item in state.slots
                    ):
                        raise StorageCorruption("Archived item conflicts with process snapshot")
                    outcome = "SCRAP" if kind == "WIP_SCRAP" else kind.removeprefix("PRODUCTION_")
                    totals[execution][outcome] += 1
                else:
                    raise StorageCorruption("Invalid archived event kind")
            if expected != through + 1:
                raise StorageCorruption("Archived evidence does not cover checkpoint prefix")
            checkpoints = {}
            for checkpoint in self._db.execute("SELECT * FROM execution_checkpoints"):
                key = checkpoint["execution_id"]
                if key not in states:
                    raise StorageCorruption("Disposition checkpoint references missing execution")
                checkpoints[key] = {
                    kind: decode_uint64(checkpoint[kind.lower()])
                    for kind in ("GOOD", "REJECT", "SCRAP")
                }
            for key, values in totals.items():
                if checkpoints.get(key, dict.fromkeys(values, 0)) != values:
                    raise StorageCorruption("Archived dispositions disagree with checkpoint totals")
            collision = self._db.execute(
                "SELECT 1 FROM source_events live JOIN archived_event_keys archived "
                "ON live.event_id=archived.event_id OR live.seq=archived.seq OR "
                "(live.execution_id=archived.execution_id AND live.item_id=archived.item_id) "
                "LIMIT 1"
            ).fetchone()
            if collision or self._db.execute("PRAGMA foreign_key_check").fetchone():
                raise StorageCorruption("Archived/live keys overlap or foreign keys are invalid")
            if (
                self._db.execute("SELECT count(*) FROM event_retention").fetchone()[0]
                != (self._db.execute("SELECT count(*) FROM source_events").fetchone()[0])
            ):
                raise StorageCorruption("Event retention ages do not cover exactly live events")
            return totals
        except (ValueError, TypeError, KeyError, OverflowError) as error:
            raise StorageCorruption("Invalid durable retention evidence") from error

    def _before_retention_commit(self) -> None:
        """Internal crash seam after archive/delete/checkpoint writes, before outer commit."""

    def _after_retention_commit(self) -> None:
        """Internal crash seam; deferred until the composition owner's durable commit."""

    @staticmethod
    def _retention_expired(sequence: int, recorded_at: datetime, cutoff: datetime) -> bool:
        # PositiveUInt64 cannot represent max+1 as an empty retained prefix.
        return sequence < UINT64_MAX and recorded_at <= cutoff

    def prune_events(
        self, *, controller_guard: Callable[[], None], limit: int = 1000
    ) -> RetentionResult:
        """Trusted internal maintenance; callers must never expose this directly over a network.

        The required bound controller guard must validate its durable current projection,
        final summaries, unresolved faults and accepted commands inside this same unit.
        A boolean permission flag or prior cached check cannot replace that validation.
        """
        if not callable(controller_guard):
            raise ValueError("A current controller retention guard is required")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Retention batch limit must be in 1..1000")
        with self.transaction():
            self._require_recovered()
            # Blocked maintenance never changes source history, so avoid a full
            # archive/process scan on every optional scheduler poll during the hold.
            effective = self._retention_time()
            if self._retention_clock_frozen:
                raise RetentionBlocked(
                    "CLOCK_FROZEN: UTC/monotonic disagreement; collection continues"
                )
            if self._clock_elapsed < _RETENTION_SECONDS:
                raise RetentionBlocked("STARTUP_HOLD: require seven days of monotonic owner uptime")
            self.acknowledge_recovery()
            if controller_guard() is not None:
                raise ValueError(
                    "Controller retention guard must raise when blocked, return None otherwise"
                )
            cutoff = effective - _RETENTION
            first, _ = self._bounds()
            states, _ = self._execution_states()
            rows = self._db.execute(
                "SELECT live.*,age.recorded_at FROM source_events live "
                "JOIN event_retention age USING(event_id) ORDER BY seq LIMIT ?",
                (limit,),
            ).fetchall()
            count = 0
            through = first - 1
            for row in rows:
                event = self._event_from_row(row, states)
                recorded_at = _TIMESTAMP.validate_python(row["recorded_at"])
                if not self._retention_expired(event.seq, recorded_at, cutoff):
                    break
                self._db.execute(
                    "INSERT INTO archived_event_keys VALUES (?,?,?,?,?,?)",
                    (
                        event.event_id,
                        row["seq"],
                        event.execution_id,
                        row["item_id"],
                        event.kind,
                        hashlib.sha256(row["body"].encode("utf-8")).hexdigest(),
                    ),
                )
                if isinstance(event, (ProductionEvent, ScrapEvent)):
                    key = event.execution_id
                    current = self._db.execute(
                        "SELECT good,reject,scrap FROM execution_checkpoints WHERE execution_id=?",
                        (key,),
                    ).fetchone()
                    values = [0, 0, 0] if current is None else [decode_uint64(v) for v in current]
                    outcome = (
                        "SCRAP"
                        if event.kind == "WIP_SCRAP"
                        else event.kind.removeprefix("PRODUCTION_")
                    )
                    values[("GOOD", "REJECT", "SCRAP").index(outcome)] += 1
                    self._db.execute(
                        "INSERT INTO execution_checkpoints VALUES (?,?,?,?) "
                        "ON CONFLICT(execution_id) DO UPDATE SET good=excluded.good,"
                        "reject=excluded.reject,scrap=excluded.scrap",
                        (key, *(encode_uint64(value) for value in values)),
                    )
                self._db.execute("DELETE FROM source_events WHERE event_id=?", (event.event_id,))
                through = event.seq
                count += 1
            self._db.execute(
                "UPDATE retention_checkpoint SET pruned_through_seq=? WHERE singleton=1",
                (encode_uint64(through),),
            )
            self._db.execute(
                "UPDATE simulator_identity SET retained_from_seq=? WHERE singleton=1",
                (encode_uint64(through + 1),),
            )
            self._before_retention_commit()
            self._commit_callbacks.append(self._after_retention_commit)
        return RetentionResult(count, through, through + 1)
