"""Persistent test-only field publication overlays, separate from physical PLC inputs.

No callback sleeps, network disconnects, process writes or implicit fault clearing.
Publication reads committed configuration; setters never mutate publication memory.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from asyncua import ua
from pydantic import Field, TypeAdapter, model_validator

from apps.simulator.storage import SimulatorStorage, StorageCorruption, encode_uint64
from contracts.models import ContractModel, Identifier, UInt32, UInt64, UTCTimestamp
from contracts.tags.validate import load_contract
from contracts.transport import TagValue

Group = Literal["Infeed", "Sealer", "Outfeed"]
Mode = Literal["NORMAL", "FROZEN", "BAD", "UNCERTAIN"]
GROUPS = ("Infeed", "Sealer", "Outfeed")
_TAGS = {tag["logical_name"]: tag for tag in load_contract()["tags"]}
_GROUP_TAGS = {
    group: tuple(name for name in _TAGS if name.startswith(group + ".")) for group in GROUPS
}
_GROUP = TypeAdapter(Group)
_ID = TypeAdapter(Identifier)
_TIME = TypeAdapter(UTCTimestamp)
_MAX = 2**64 - 1

_REVISION_CHECK = (
    "CHECK(length(revision)=20 AND revision NOT GLOB '*[^0-9]*' "
    "AND revision<='18446744073709551615')"
)
_DDL = {
    "field_channel_schema": (
        "CREATE TABLE field_channel_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
        "version INTEGER NOT NULL CHECK(version=1))"
    ),
    "field_channel_faults": (
        "CREATE TABLE field_channel_faults(group_id TEXT PRIMARY KEY CHECK(group_id IN "
        "('Infeed','Sealer','Outfeed')),revision TEXT NOT NULL " + _REVISION_CHECK + ","
        "state_json TEXT NOT NULL CHECK(json_valid(state_json)))"
    ),
    "field_channel_audit": (
        "CREATE TABLE field_channel_audit(audit_id INTEGER PRIMARY KEY,group_id TEXT NOT NULL "
        "REFERENCES field_channel_faults(group_id),revision TEXT NOT NULL " + _REVISION_CHECK + ","
        "actor TEXT NOT NULL,changed_at TEXT NOT NULL,"
        "before_json TEXT NOT NULL CHECK(json_valid(before_json)),"
        "after_json TEXT NOT NULL CHECK(json_valid(after_json)),UNIQUE(group_id,revision))"
    ),
    "field_channel_audit_update": (
        "CREATE TRIGGER field_channel_audit_update BEFORE UPDATE ON field_channel_audit "
        "BEGIN SELECT RAISE(ABORT,'Field fault audit is append only'); END"
    ),
    "field_channel_audit_delete": (
        "CREATE TRIGGER field_channel_audit_delete BEFORE DELETE ON field_channel_audit "
        "BEGIN SELECT RAISE(ABORT,'Field fault audit is append only'); END"
    ),
}


class FieldFaultConflict(ValueError):
    """A stale revision or unavailable genuine Good frame cannot change this fault."""


class PublishedField(ContractModel):
    tag_id: Identifier
    reading: TagValue
    status_code: UInt32
    source_timestamp: UTCTimestamp

    @model_validator(mode="after")
    def dictionary(self) -> Self:
        tag = _TAGS.get(self.tag_id)
        if tag is None or tag["data_type"] != self.reading.data_type:
            raise ValueError("Published field must have its exact dictionary type")
        if self.reading.value is None:
            raise ValueError("This field overlay unit does not synthesize missing readings")
        contract = load_contract()
        if (
            tag["enum"] is not None
            and str(self.reading.value) not in contract["enums"][tag["enum"]]
        ):
            raise ValueError("Published enum value must exist in the dictionary")
        return self


class GroupFrame(ContractModel):
    group_id: Group
    frame_id: Identifier
    published_at: UTCTimestamp
    revision: UInt64
    mode: Mode
    fields: Annotated[tuple[PublishedField, ...], Field(min_length=1, max_length=12)]

    @model_validator(mode="after")
    def complete(self) -> Self:
        names = [field.tag_id for field in self.fields]
        if len(set(names)) != len(names) or set(names) != set(_GROUP_TAGS[self.group_id]):
            raise ValueError("One complete exact group is required, without duplicates")
        return self

    @property
    def all_good(self):
        return all(field.status_code >> 30 == 0 for field in self.fields)


class FieldFaultState(ContractModel):
    group_id: Group
    revision: UInt64 = 0
    mode: Mode = "NORMAL"
    frozen_checkpoint: GroupFrame | None = None

    @model_validator(mode="after")
    def checkpoint(self) -> Self:
        if self.mode == "FROZEN":
            if (
                self.frozen_checkpoint is None
                or self.frozen_checkpoint.group_id != self.group_id
                or not self.frozen_checkpoint.all_good
                or self.frozen_checkpoint.mode != "NORMAL"
                or self.frozen_checkpoint.revision != self.revision - 1
            ):
                raise ValueError("FROZEN requires an exact original Good typed group checkpoint")
        elif self.frozen_checkpoint is not None:
            raise ValueError("Only FROZEN carries a checkpoint")
        if self.revision == 0 and (self.mode != "NORMAL" or self.frozen_checkpoint is not None):
            raise ValueError("Initial revision is an unmodified NORMAL configuration")
        return self


class FieldFaultInput(ContractModel):
    mode: Mode
    expected_revision: UInt64


class FieldChannelStatus(ContractModel):
    configured: FieldFaultState
    published: GroupFrame | None = None
    pending: bool


class FieldChannels:
    """One owning SQLite/event-loop thread; publication memory is only observed state."""

    def __init__(self, storage: SimulatorStorage):
        self.storage = storage
        self._published: dict[str, GroupFrame] = {}
        self._schema_version = None
        with storage.transaction():
            self._initialize(allow_create=True)
            self._schema_version = storage.connection.execute("PRAGMA schema_version").fetchone()[0]
            self._validate_history()

    def _corrupt(self, message, error=None):
        self.storage.mark_corrupt()
        raise StorageCorruption(message) from error

    def _initialize(self, *, allow_create=False):
        db = self.storage.connection
        expected = {
            "field_channel_schema": ("singleton", "version"),
            "field_channel_faults": ("group_id", "revision", "state_json"),
            "field_channel_audit": (
                "audit_id",
                "group_id",
                "revision",
                "actor",
                "changed_at",
                "before_json",
                "after_json",
            ),
        }
        owned = db.execute(
            "SELECT name,type,tbl_name,sql FROM sqlite_master WHERE name GLOB 'field_channel_*' "
            "OR tbl_name IN ('field_channel_schema','field_channel_faults','field_channel_audit')"
        ).fetchall()
        autoindexes = {
            "sqlite_autoindex_field_channel_faults_1": "field_channel_faults",
            "sqlite_autoindex_field_channel_audit_1": "field_channel_audit",
        }
        objects = {}
        seen_autoindexes = set()
        for name, kind, table, sql in owned:
            if (
                name in autoindexes
                and kind == "index"
                and table == autoindexes[name]
                and sql is None
            ):
                seen_autoindexes.add(name)
            else:
                objects[name] = sql
        if objects and seen_autoindexes != set(autoindexes):
            self._corrupt("Missing field-channel uniqueness indexes")
        if objects:
            if set(objects) != set(_DDL):
                self._corrupt("Partial or unknown field-channel component cannot be initialized")
            for name, frozen_sql in _DDL.items():
                if objects[name] != frozen_sql:
                    self._corrupt("Unknown field-channel DDL or audit protection")
            for table, columns in expected.items():
                if tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})")) != columns:
                    self._corrupt("Unknown field-channel table layout")
            if [tuple(row) for row in db.execute("SELECT * FROM field_channel_schema")] != [(1, 1)]:
                self._corrupt("Unsupported field-channel component version")
            return
        if not allow_create:
            self._corrupt("Initialized field-channel component is missing")
        for statement in _DDL.values():
            db.execute(statement)
        db.execute("INSERT INTO field_channel_schema VALUES(1,1)")
        for group in GROUPS:
            state = FieldFaultState(group_id=group)
            db.execute(
                "INSERT INTO field_channel_faults VALUES(?,?,?)",
                (group, encode_uint64(0), state.model_dump_json()),
            )
        self._before_initialize_commit()

    def _validate_history(self):
        """Full startup validation; normal publication checks only bounded current heads."""
        last = {group: FieldFaultState(group_id=group) for group in GROUPS}
        try:
            for group, revision, actor, at, before, after in self.storage.connection.execute(
                "SELECT group_id,revision,actor,changed_at,before_json,after_json "
                "FROM field_channel_audit ORDER BY group_id,revision"
            ):
                _GROUP.validate_python(group)
                _ID.validate_python(actor)
                _TIME.validate_python(at)
                previous = FieldFaultState.model_validate_json(before)
                updated = FieldFaultState.model_validate_json(after)
                if (
                    previous != last[group]
                    or updated.group_id != group
                    or updated.revision != previous.revision + 1
                    or encode_uint64(updated.revision) != revision
                    or updated.mode == previous.mode
                    or (updated.mode == "FROZEN" and previous.mode != "NORMAL")
                ):
                    raise ValueError("Field audit is not a complete immutable revision chain")
                last[group] = updated
            if {state.group_id: state for state in self.states()} != last:
                raise ValueError("Field current heads do not match their complete audit history")
        except (TypeError, ValueError) as error:
            self._corrupt("Invalid persisted field-channel audit history", error)

    def _before_initialize_commit(self):
        """Internal atomic component-initialization failure seam."""

    def states(self) -> tuple[FieldFaultState, ...]:
        db = self.storage.connection
        version = db.execute("PRAGMA schema_version").fetchone()[0]
        if version != self._schema_version:
            self._initialize()
            previous_version = self._schema_version
            self._schema_version = version
            try:
                self._validate_history()
            except Exception:
                self._schema_version = previous_version
                raise
        try:
            rows = db.execute(
                "SELECT group_id,revision,state_json FROM field_channel_faults ORDER BY group_id"
            ).fetchall()
            if len(rows) != 3 or {row[0] for row in rows} != set(GROUPS):
                raise ValueError("Exactly three field configurations are required")
            result = []
            for group, revision, payload in rows:
                state = FieldFaultState.model_validate_json(payload)
                if state.group_id != group or encode_uint64(state.revision) != revision:
                    raise ValueError("Stored field identity/revision disagrees with its payload")
                audit = db.execute(
                    "SELECT revision,actor,changed_at,before_json,after_json FROM "
                    "field_channel_audit WHERE group_id=? ORDER BY revision DESC LIMIT 1",
                    (group,),
                ).fetchone()
                if state.revision == 0:
                    if audit is not None:
                        raise ValueError(
                            "Initial field configuration cannot carry a mutation audit"
                        )
                else:
                    if audit is None:
                        raise ValueError("Field mutation audit is missing")
                    audit_revision, actor, at, before, after = audit
                    if audit_revision != revision:
                        raise ValueError("Field head does not match its latest audit revision")
                    _ID.validate_python(actor)
                    _TIME.validate_python(at)
                    previous = FieldFaultState.model_validate_json(before)
                    persisted = FieldFaultState.model_validate_json(after)
                    if (
                        persisted != state
                        or previous.group_id != group
                        or previous.revision != state.revision - 1
                        or previous.mode == state.mode
                        or (state.mode == "FROZEN" and previous.mode != "NORMAL")
                    ):
                        raise ValueError("Field mutation evidence disagrees with the current state")
                result.append(state)
            return tuple(result)
        except (TypeError, ValueError) as error:
            self._corrupt("Invalid persisted field-channel configuration", error)

    def describe(self) -> tuple[FieldChannelStatus, ...]:
        return tuple(
            FieldChannelStatus(
                configured=state,
                published=self._published.get(state.group_id),
                pending=(
                    self._published.get(state.group_id) is None
                    or self._published[state.group_id].revision != state.revision
                ),
            )
            for state in self.states()
        )

    def set_fault(self, group: Group, intent: FieldFaultInput, *, actor: str, at: datetime):
        group = _GROUP.validate_python(group)
        intent = FieldFaultInput.model_validate_json(intent.model_dump_json())
        actor, at = _ID.validate_python(actor), _TIME.validate_python(at)
        with self.storage.transaction():
            state = next(value for value in self.states() if value.group_id == group)
            if intent.expected_revision != state.revision:
                raise FieldFaultConflict("Field revision changed; read the current configuration")
            if intent.mode == state.mode:
                return state
            if state.revision == _MAX:
                raise FieldFaultConflict("Field revision exhausted")
            checkpoint = None
            if intent.mode == "FROZEN":
                checkpoint = self._published.get(group)
                if (
                    checkpoint is None
                    or not checkpoint.all_good
                    or checkpoint.revision != state.revision
                ):
                    raise FieldFaultConflict("The last published complete group is not Good")
            updated = FieldFaultState(
                group_id=group,
                revision=state.revision + 1,
                mode=intent.mode,
                frozen_checkpoint=checkpoint,
            )
            self.storage.connection.execute(
                "UPDATE field_channel_faults SET revision=?,state_json=? WHERE group_id=?",
                (encode_uint64(updated.revision), updated.model_dump_json(), group),
            )
            self.storage.connection.execute(
                "INSERT INTO field_channel_audit "
                "(group_id,revision,actor,changed_at,before_json,after_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    group,
                    encode_uint64(updated.revision),
                    actor,
                    at.isoformat(),
                    state.model_dump_json(),
                    updated.model_dump_json(),
                ),
            )
            self._before_fault_commit()
            return updated

    def _before_fault_commit(self):
        """Internal atomic config/audit failure seam; publication memory stays untouched."""

    def prepare(
        self,
        values: dict,
        *,
        acquired_at: datetime,
        published_at: datetime,
        status_code: int,
    ):
        """Build a complete committed overlay, without publishing or mutating memory."""
        if self.storage.connection.in_transaction:
            raise FieldFaultConflict("Uncommitted configuration cannot be published")
        acquired_at, published_at = (
            _TIME.validate_python(acquired_at),
            _TIME.validate_python(published_at),
        )
        if set(values) != set(_TAGS):
            raise ValueError("Field publication requires the complete 48-tag dictionary")
        states = {state.group_id: state for state in self.states()}
        fields = {
            name: PublishedField(
                tag_id=name,
                reading={"data_type": tag["data_type"], "value": values[name]},
                status_code=status_code,
                source_timestamp=acquired_at,
            )
            for name, tag in _TAGS.items()
        }
        frames = {}
        for group, state in states.items():
            original = tuple(fields[name] for name in _GROUP_TAGS[group])
            if state.mode == "FROZEN" and all(value.status_code >> 30 == 0 for value in original):
                selected = state.frozen_checkpoint.fields
            elif state.mode in {"BAD", "UNCERTAIN"}:
                injected = (
                    ua.StatusCodes.BadSensorFailure
                    if state.mode == "BAD"
                    else ua.StatusCodes.UncertainSensorNotAccurate
                )
                selected = tuple(
                    value.model_copy(update={"status_code": injected})
                    if value.status_code >> 30 < injected >> 30
                    else value
                    for value in original
                )
            else:
                selected = original
            for value in selected:
                fields[value.tag_id] = value
            frames[group] = GroupFrame(
                group_id=group,
                frame_id="field-frame:" + str(uuid4()),
                published_at=published_at,
                revision=state.revision,
                mode=state.mode,
                fields=selected,
            )
        return fields, frames

    def published(self, frames: dict[str, GroupFrame]) -> None:
        """Called immediately after the OPC pointer swap, with no await in between."""
        self._published = frames
