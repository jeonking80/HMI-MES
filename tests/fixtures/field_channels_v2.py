"""Persistent test-only field publication overlays, separate from physical PLC inputs.

No callback sleeps, network disconnects, process writes or implicit fault clearing.
Publication reads committed configuration; setters never mutate publication memory.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from asyncua import ua
from pydantic import Field, TypeAdapter, model_validator

from apps.simulator import _field_channels_v1 as v1
from apps.simulator.storage import SimulatorStorage, StorageCorruption, encode_uint64
from contracts.models import ContractModel, Identifier, UInt32, UInt64, UTCTimestamp
from contracts.tags.validate import load_contract
from contracts.transport import TagValue

Group = Literal["Infeed", "Sealer", "Outfeed"]
Mode = Literal["NORMAL", "FROZEN", "BAD", "UNCERTAIN", "READ_FAILURE", "LINK_DOWN"]
_FAILURES = {
    "READ_FAILURE": ua.StatusCodes.BadCommunicationError,
    "LINK_DOWN": ua.StatusCodes.BadNotConnected,
}
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
# ADD COLUMN preserves the original table SQL prefix and all existing JSON TEXT.
_DDL = dict(v1._DDL)
_DDL["field_channel_schema"] = v1._DDL["field_channel_schema"].replace("version=1", "version=2")
_ADDITIONS = (
    ("field_channel_faults", "state_payload_version"),
    ("field_channel_audit", "before_payload_version"),
    ("field_channel_audit", "after_payload_version"),
)
for _table, _column in _ADDITIONS:
    _addition = f", {_column} INTEGER NOT NULL DEFAULT 1 CHECK({_column} IN (1,2))"
    if _table == "field_channel_audit":
        _DDL[_table] = _DDL[_table].replace(
            ",UNIQUE(group_id,revision)", _addition + ",UNIQUE(group_id,revision)"
        )
    else:
        _DDL[_table] = _DDL[_table][:-1] + _addition + ")"


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
    payload_version: Literal[2] = 2
    group_id: Group
    revision: UInt64 = 0
    mode: Mode = "NORMAL"
    frozen_checkpoint: GroupFrame | None = None
    normal_checkpoint: GroupFrame | None = None

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
        if self.mode in _FAILURES:
            checkpoint = self.normal_checkpoint
            if (
                checkpoint is None
                or checkpoint.group_id != self.group_id
                or checkpoint.mode != "NORMAL"
                or checkpoint.revision != self.revision - 1
            ):
                raise ValueError("Failure requires the exact original NORMAL group checkpoint")
        elif self.normal_checkpoint is not None:
            raise ValueError("Only a failure mode carries a NORMAL checkpoint")
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
            layout = self._layout(allow_absent=True)
            if layout == 0:
                for statement in _DDL.values():
                    storage.connection.execute(statement)
                storage.connection.execute("INSERT INTO field_channel_schema VALUES(1,2)")
                for group in GROUPS:
                    state = FieldFaultState(group_id=group)
                    storage.connection.execute(
                        "INSERT INTO field_channel_faults VALUES(?,?,?,2)",
                        (group, encode_uint64(0), state.model_dump_json()),
                    )
                self._before_initialize_commit()
            elif layout == 1:
                self._validate_history(layout=1)
                before = self._legacy_digest()
                for table, column in _ADDITIONS:
                    storage.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL "
                        f"DEFAULT 1 CHECK({column} IN (1,2))"
                    )
                    self._migration_step(column)
                storage.connection.execute("DROP TABLE field_channel_schema")
                storage.connection.execute(_DDL["field_channel_schema"])
                storage.connection.execute("INSERT INTO field_channel_schema VALUES(1,2)")
                self._migration_step("metadata")
                self._layout()
                self._validate_history()
                if before != self._legacy_digest():
                    self._corrupt("Migration changed original field evidence")
                self._migration_step("validated")
            self._layout()
            self._validate_history()
            self._schema_version = self._schema_cookie()
            self._migration_step("before_commit")

    def _corrupt(self, message, error=None):
        self.storage.mark_corrupt()
        raise StorageCorruption(message) from error

    def _schema_cookie(self):
        return self.storage.connection.execute("PRAGMA schema_version").fetchone()[0]

    def _layout(self, *, allow_absent=False):
        db = self.storage.connection
        owned = db.execute(
            "SELECT name,type,tbl_name,sql FROM sqlite_master WHERE name GLOB 'field_channel_*' "
            "OR tbl_name IN ('field_channel_schema','field_channel_faults','field_channel_audit')"
        ).fetchall()
        if not owned and allow_absent:
            return 0
        autoindexes = {
            "sqlite_autoindex_field_channel_faults_1": "field_channel_faults",
            "sqlite_autoindex_field_channel_audit_1": "field_channel_audit",
        }
        objects, seen = {}, set()
        for name, kind, table, sql in owned:
            if (
                name in autoindexes
                and kind == "index"
                and table == autoindexes[name]
                and sql is None
            ):
                seen.add(name)
            else:
                objects[name] = sql
        if seen != set(autoindexes):
            self._corrupt("Missing field-channel uniqueness indexes")
        for version, expected in ((2, _DDL), (1, v1._DDL)):
            if objects == expected:
                if [tuple(row) for row in db.execute("SELECT * FROM field_channel_schema")] != [
                    (1, version)
                ]:
                    self._corrupt("Unsupported field-channel component version")
                return version
        self._corrupt("Unknown, missing or partial field-channel component")

    def _migration_step(self, stage):
        """Internal failure seam; every migration step remains in the owner transaction."""

    def _legacy_digest(self):
        digest = hashlib.sha256()
        db = self.storage.connection
        count = 0
        for query in (
            "SELECT group_id,revision,CAST(state_json AS BLOB) FROM field_channel_faults "
            "ORDER BY group_id",
            "SELECT audit_id,group_id,revision,actor,changed_at,CAST(before_json AS BLOB),"
            "CAST(after_json AS BLOB) FROM field_channel_audit ORDER BY audit_id",
        ):
            digest.update(b"table")
            for row in db.execute(query):
                count += 1
                for item in row:
                    raw = item if isinstance(item, bytes) else str(item).encode()
                    digest.update(len(raw).to_bytes(8, "big"))
                    digest.update(raw)
        return count, digest.digest()

    @staticmethod
    def _parse(version, raw):
        if type(version) is not int or version not in (1, 2):
            raise ValueError("Unknown field payload discriminator")
        if version == 1:
            legacy = v1.FieldFaultState.model_validate_json(raw)
            return FieldFaultState.model_validate(legacy.model_dump())
        # payload_version has a constructor default, but is mandatory in durable V2 JSON.
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or type(payload.get("payload_version")) is not int
            or payload["payload_version"] != 2
        ):
            raise ValueError("V2 payload must explicitly declare version 2")
        return FieldFaultState.model_validate_json(raw)

    @staticmethod
    def _transition(previous, updated):
        if (
            updated.group_id != previous.group_id
            or updated.revision != previous.revision + 1
            or updated.mode == previous.mode
            or (updated.mode in {"FROZEN", *_FAILURES} and previous.mode != "NORMAL")
        ):
            raise ValueError("Invalid field revision transition")

    def _rows(self, layout=2):
        suffix = ",state_payload_version" if layout == 2 else ",1"
        return self.storage.connection.execute(
            "SELECT group_id,revision,state_json"
            + suffix
            + " FROM field_channel_faults ORDER BY group_id"
        ).fetchall()

    def _audit(self, layout=2, *, group=None):
        suffix = ",before_payload_version,after_payload_version" if layout == 2 else ",1,1"
        query = (
            "SELECT group_id,revision,actor,changed_at,before_json,after_json"
            + suffix
            + " FROM field_channel_audit"
        )
        if group is not None:
            return self.storage.connection.execute(
                query + " WHERE group_id=? ORDER BY revision DESC LIMIT 1", (group,)
            ).fetchone()
        return self.storage.connection.execute(query + " ORDER BY group_id,revision")

    def _audit_states(self, row):
        group, revision, actor, at, before, after, bv, av = row
        _GROUP.validate_python(group)
        _ID.validate_python(actor)
        _TIME.validate_python(at)
        previous, updated = self._parse(bv, before), self._parse(av, after)
        if (bv, av) not in {(1, 1), (1, 2), (2, 2)}:
            raise ValueError("Field payload downgrade is forbidden")
        self._transition(previous, updated)
        if updated.group_id != group or encode_uint64(updated.revision) != revision:
            raise ValueError("Field audit identity mismatch")
        return previous, updated

    def _validate_history(self, layout=2):
        """Stream exact V1/V2 audit lineage without rewriting any persisted payload."""
        try:
            rows = self._rows(layout)
            if len(rows) != 3 or {row[0] for row in rows} != set(GROUPS):
                raise ValueError("Exactly three field configurations are required")
            heads = {}
            for group, revision, raw, version in rows:
                state = self._parse(version, raw)
                if state.group_id != group or encode_uint64(state.revision) != revision:
                    raise ValueError("Field head identity mismatch")
                heads[group] = (state, version, raw)
            last = {}
            for row in self._audit(layout):
                previous, updated = self._audit_states(row)
                group, _, _, _, _, raw, bv, av = row
                if group not in last:
                    if previous != FieldFaultState(group_id=group):
                        raise ValueError("Field history must begin at the initial state")
                elif (previous, bv) != last[group][:2]:
                    raise ValueError("Field audit lineage disagrees")
                last[group] = (updated, av, raw)
            for group, (state, version, raw) in heads.items():
                if group not in last:
                    if state != FieldFaultState(group_id=group):
                        raise ValueError("Field history is missing")
                else:
                    previous, pv, previous_raw = last[group]
                    if (state, version) != (previous, pv) or (version == 2 and raw != previous_raw):
                        raise ValueError("Field head does not match complete audit history")
        except (TypeError, ValueError, KeyError) as error:
            self._corrupt("Invalid persisted field-channel audit history", error)

    def _before_initialize_commit(self):
        """Internal atomic component-initialization failure seam."""

    def states(self) -> tuple[FieldFaultState, ...]:
        version = self._schema_cookie()
        if version != self._schema_version:
            if self._layout() != 2:
                self._corrupt("Live field component cannot migrate or downgrade")
            self._validate_history()
            self._schema_version = version
        try:
            rows = self._rows()
            if len(rows) != 3 or {row[0] for row in rows} != set(GROUPS):
                raise ValueError("Exactly three field configurations are required")
            result = []
            for group, revision, raw, payload_version in rows:
                state = self._parse(payload_version, raw)
                if state.group_id != group or encode_uint64(state.revision) != revision:
                    raise ValueError("Stored field identity/revision disagrees")
                audit = self._audit(group=group)
                if state.revision == 0:
                    if audit is not None:
                        raise ValueError("Initial field state cannot have audit rows")
                else:
                    if audit is None:
                        raise ValueError("Missing field mutation audit")
                    _, persisted = self._audit_states(audit)
                    if (persisted, audit[7]) != (state, payload_version) or (
                        payload_version == 2 and audit[5] != raw
                    ):
                        raise ValueError("Latest field audit does not match current head")
                result.append(state)
            return tuple(result)
        except (TypeError, ValueError, KeyError) as error:
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
            if intent.mode in {"FROZEN", *_FAILURES}:
                checkpoint = self._published.get(group)
                if (
                    state.mode != "NORMAL"
                    or checkpoint is None
                    or checkpoint.mode != "NORMAL"
                    or checkpoint.group_id != group
                    or (intent.mode == "FROZEN" and not checkpoint.all_good)
                    or checkpoint.revision != state.revision
                ):
                    raise FieldFaultConflict(
                        "An actual current NORMAL frame is required; FROZEN also requires Good"
                    )
            updated = FieldFaultState(
                group_id=group,
                revision=state.revision + 1,
                mode=intent.mode,
                frozen_checkpoint=checkpoint if intent.mode == "FROZEN" else None,
                normal_checkpoint=checkpoint if intent.mode in _FAILURES else None,
            )
            raw_before, before_version = self.storage.connection.execute(
                "SELECT state_json,state_payload_version FROM field_channel_faults WHERE "
                "group_id=?",
                (group,),
            ).fetchone()
            raw_after = updated.model_dump_json()
            self.storage.connection.execute(
                "UPDATE field_channel_faults SET revision=?,state_json=?,state_payload_version=2 "
                "WHERE group_id=?",
                (encode_uint64(updated.revision), raw_after, group),
            )
            self.storage.connection.execute(
                "INSERT INTO field_channel_audit "
                "(group_id,revision,actor,changed_at,before_json,after_json,before_payload_version,after_payload_version)"
                " "
                "VALUES(?,?,?,?,?,?,?,2)",
                (
                    group,
                    encode_uint64(updated.revision),
                    actor,
                    at.isoformat(),
                    raw_before,
                    raw_after,
                    before_version,
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
            if state.mode in _FAILURES:
                selected = tuple(
                    value.model_copy(update={"status_code": _FAILURES[state.mode]})
                    for value in state.normal_checkpoint.fields
                )
            elif state.mode == "FROZEN" and all(value.status_code >> 30 == 0 for value in original):
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
