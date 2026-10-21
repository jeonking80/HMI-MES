"""Persistent test-only field publication overlays, separate from physical PLC inputs.

No callback sleeps, network disconnects, process writes or implicit fault clearing.
Publication reads committed configuration; setters never mutate publication memory.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import uuid4

from asyncua import ua
from pydantic import Field, TypeAdapter, model_validator

from apps.simulator import _field_channels_v1 as v1
from apps.simulator import _field_channels_v2 as v2
from apps.simulator.storage import SimulatorStorage, StorageCorruption, encode_uint64
from contracts.models import ContractModel, Identifier, UInt32, UInt64, UTCTimestamp
from contracts.tags.validate import load_contract
from contracts.transport import TagValue

Group = Literal["Infeed", "Sealer", "Outfeed"]
Mode = Literal["NORMAL", "FROZEN", "BAD", "UNCERTAIN", "READ_FAILURE", "LINK_DOWN", "DELAY"]
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
# Canonical CREATE literals; all discriminators are explicit, without DEFAULT.
_DDL = {
    "field_channel_faults": (
        "CREATE TABLE field_channel_faults(group_id TEXT PRIMARY KEY CHECK(group_id IN "
        "('Infeed','Sealer','Outfeed')),revision TEXT NOT NULL " + _REVISION_CHECK + ","
        "state_json TEXT NOT NULL CHECK(json_valid(state_json)),"
        "state_payload_version INTEGER NOT NULL CHECK(state_payload_version IN (1,2,3)))"
    ),
    "field_channel_audit": (
        "CREATE TABLE field_channel_audit(audit_id INTEGER PRIMARY KEY,group_id TEXT NOT NULL "
        "REFERENCES field_channel_faults(group_id),revision TEXT NOT NULL " + _REVISION_CHECK + ","
        "actor TEXT NOT NULL,changed_at TEXT NOT NULL,before_json TEXT NOT NULL "
        "CHECK(json_valid(before_json)),"
        "after_json TEXT NOT NULL CHECK(json_valid(after_json)),"
        "before_payload_version INTEGER NOT NULL CHECK(before_payload_version IN (1,2,3)),"
        "after_payload_version INTEGER NOT NULL CHECK(after_payload_version IN "
        "(1,2,3)),UNIQUE(group_id,revision))"
    ),
    "field_channel_schema": (
        "CREATE TABLE field_channel_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
        "version INTEGER NOT NULL CHECK(version=3))"
    ),
    "field_channel_audit_update": v1._DDL["field_channel_audit_update"],
    "field_channel_audit_delete": v1._DDL["field_channel_audit_delete"],
}
_OWNED = {"field_channel_schema", "field_channel_faults", "field_channel_audit"}
_STAGING = {"field_v3_heads_stage", "field_v3_audit_stage"}
_NONFIELD_TRIGGERS = {
    "source_events_archive_collision": (
        "trigger",
        "source_events",
        "CREATE TRIGGER source_events_archive_collision BEFORE INSERT ON source_events WHEN "
        "EXISTS (SELECT 1 FROM archived_event_keys WHERE event_id=NEW.event_id OR seq=NEW.seq"
        " OR (execution_id=NEW.execution_id AND item_id=NEW.item_id)) BEGIN SELECT "
        "RAISE(ABORT,'Archived source event/item collision'); END",
    ),
    "source_events_recorded": (
        "trigger",
        "source_events",
        "CREATE TRIGGER source_events_recorded AFTER INSERT ON source_events BEGIN INSERT "
        "INTO event_retention VALUES(NEW.event_id,retention_recorded_at()); END",
    ),
    "archived_keys_match_live": (
        "trigger",
        "archived_event_keys",
        "CREATE TRIGGER archived_keys_match_live BEFORE INSERT ON archived_event_keys WHEN "
        "NOT EXISTS (SELECT 1 FROM source_events WHERE event_id=NEW.event_id AND seq=NEW.seq "
        "AND execution_id IS NEW.execution_id AND item_id IS NEW.item_id AND "
        "json_extract(body,'$.kind')=NEW.kind AND retention_body_hash(body)=NEW.body_sha256) "
        "BEGIN SELECT RAISE(ABORT,'Archive requires original matching source event'); END",
    ),
    "archived_keys_no_update": (
        "trigger",
        "archived_event_keys",
        "CREATE TRIGGER archived_keys_no_update BEFORE UPDATE ON archived_event_keys BEGIN "
        "SELECT RAISE(ABORT,'Archived event keys are immutable'); END",
    ),
    "archived_keys_no_delete": (
        "trigger",
        "archived_event_keys",
        "CREATE TRIGGER archived_keys_no_delete BEFORE DELETE ON archived_event_keys BEGIN "
        "SELECT RAISE(ABORT,'Archived event keys are permanent'); END",
    ),
    "retention_identity_no_update": (
        "trigger",
        "retention_checkpoint",
        "CREATE TRIGGER retention_identity_no_update BEFORE UPDATE OF "
        "source_id,stream_id,counter_epoch ON retention_checkpoint BEGIN SELECT "
        "RAISE(ABORT,'Retention identity is immutable'); END",
    ),
    "retention_age_no_update": (
        "trigger",
        "event_retention",
        "CREATE TRIGGER retention_age_no_update BEFORE UPDATE ON event_retention BEGIN SELECT"
        " RAISE(ABORT,'Event retention age is immutable'); END",
    ),
    "runtime_final_immutable": (
        "trigger",
        "runtime_runs",
        "CREATE TRIGGER runtime_final_immutable BEFORE UPDATE ON runtime_runs WHEN "
        "json_extract(OLD.summary_json,'$.final')=1 BEGIN SELECT RAISE(ABORT,'Final summary "
        "is immutable'); END",
    ),
    "runtime_final_delete_immutable": (
        "trigger",
        "runtime_runs",
        "CREATE TRIGGER runtime_final_delete_immutable BEFORE DELETE ON runtime_runs WHEN "
        "json_extract(OLD.summary_json,'$.final')=1 BEGIN SELECT RAISE(ABORT,'Final summary "
        "is immutable'); END",
    ),
}


def _quote(value):
    return '"' + value.replace('"', '""') + '"'


class FieldEnvironmentUnavailable(ValueError):
    """Unsupported external schema; refuse without executing an untrusted recovery latch."""


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
    payload_version: Literal[3] = 3
    group_id: Group
    revision: UInt64 = 0
    mode: Mode = "NORMAL"
    frozen_checkpoint: GroupFrame | None = None
    normal_checkpoint: GroupFrame | None = None
    delay_ms: Annotated[int, Field(strict=True, ge=100, le=10_000)] | None = None

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
        if self.mode in {*_FAILURES, "DELAY"}:
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
        if self.mode == "DELAY":
            if self.delay_ms is None or self.delay_ms % 100:
                raise ValueError("DELAY requires a bounded duration in 100ms steps")
        elif self.delay_ms is not None:
            raise ValueError("Only DELAY has a duration")
        if self.revision == 0 and (self.mode != "NORMAL" or self.frozen_checkpoint is not None):
            raise ValueError("Initial revision is an unmodified NORMAL configuration")
        return self


class FieldFaultInput(ContractModel):
    mode: Mode
    expected_revision: UInt64
    delay_ms: Annotated[int, Field(strict=True, ge=100, le=10_000)] | None = None

    @model_validator(mode="after")
    def duration(self):
        if self.mode == "DELAY":
            if self.delay_ms is None or self.delay_ms % 100:
                raise ValueError("DELAY requires duration in 100ms steps")
        elif self.delay_ms is not None:
            raise ValueError("Only DELAY has duration")
        return self


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
        self._check_environment()
        with storage.transaction():
            self._check_environment()
            layout = self._layout(allow_absent=True)
            if layout == 0:
                self._create_canonical()
                storage.connection.execute("INSERT INTO main.field_channel_schema VALUES(1,3)")
                for group in GROUPS:
                    state = FieldFaultState(group_id=group)
                    storage.connection.execute(
                        "INSERT INTO main.field_channel_faults VALUES(?,?,?,3)",
                        (group, encode_uint64(0), state.model_dump_json()),
                    )
                self._before_initialize_commit()
            elif layout in (1, 2):
                self._check_foreign_keys()
                self._validate_history(layout=layout)
                self._migrate(layout)
            self._layout()
            self._check_foreign_keys()
            self._validate_history()
            self._check_environment()
            self._schema_version = self._schema_cookie()
            self._migration_step("before_commit")

    def _create_canonical(self):
        for statement in _DDL.values():
            statement = statement.replace("CREATE TABLE ", "CREATE TABLE main.", 1)
            statement = statement.replace("CREATE TRIGGER ", "CREATE TRIGGER main.", 1)
            self.storage.connection.execute(statement)

    @staticmethod
    def _environment_unavailable(message):
        # Unsupported namespace/triggers cannot safely execute storage's unqualified
        # corruption latch. ValueError rolls back without poisoning that owner unit.
        raise FieldEnvironmentUnavailable(message)

    def _check_namespace(self, *, allow_staging=False):
        db = self.storage.connection
        if {row[1] for row in db.execute("PRAGMA database_list")} - {"main", "temp"}:
            self._environment_unavailable("Attached schemas are unsupported for field persistence")
        temporary = [tuple(row) for row in db.execute("SELECT name,type FROM temp.sqlite_master")]
        expected_temp = {(name, "table") for name in _STAGING} if allow_staging else set()
        if set(temporary) != expected_temp:
            self._environment_unavailable("Unexpected temporary schema objects")

    def _check_environment(self, *, allow_staging=False):
        self._check_namespace(allow_staging=allow_staging)
        db = self.storage.connection
        for row in db.execute("PRAGMA main.table_list"):
            if row[2] in {"virtual", "shadow"}:
                self._environment_unavailable("Virtual/shadow tables are unsupported")
        if db.execute("SELECT 1 FROM main.sqlite_master WHERE type='view'").fetchone():
            self._environment_unavailable("Views are unsupported for field persistence")
        tables = {
            r[0] for r in db.execute("SELECT name FROM main.sqlite_master WHERE type='table'")
        }
        runtime = any(name.startswith("runtime_") for name in tables)
        required = {
            name: value
            for name, value in _NONFIELD_TRIGGERS.items()
            if runtime or not name.startswith("runtime_")
        }
        found = {}
        for name, kind, table, sql in db.execute(
            "SELECT name,type,tbl_name,sql FROM main.sqlite_master WHERE type='trigger'"
        ):
            if table.casefold() in _OWNED:
                continue  # The exact owned DDL check below rejects unknown component triggers.
            found[name] = (kind, table, sql)
        if found != required:
            self._environment_unavailable("Unknown or missing nonfield trigger protection")
        if db.execute("PRAGMA main.user_version").fetchone()[0] != 3:
            self._environment_unavailable("Unsupported storage schema")
        if runtime and (
            "runtime_schema_version" not in tables
            or [tuple(r) for r in db.execute("SELECT * FROM main.runtime_schema_version")]
            != [(1, 2)]
        ):
            self._environment_unavailable("Unsupported runtime metadata")
        for table in tables - _OWNED:
            for fk in db.execute(f"PRAGMA main.foreign_key_list({_quote(table)})"):
                if fk[2].casefold() in _OWNED:
                    self._environment_unavailable(
                        "External inbound field foreign keys are unsupported"
                    )

    def _check_foreign_keys(self):
        # Call only after namespace/trigger safety and exact owned layout recognition.
        # Invalid external FK DDL is unsupported; actual persisted row violations in
        # a supported schema are storage corruption and must poison the owner unit.
        try:
            violation = self.storage.connection.execute("PRAGMA main.foreign_key_check").fetchone()
        except sqlite3.DatabaseError as error:
            raise FieldEnvironmentUnavailable("Unsupported foreign key schema") from error
        if violation is not None:
            self._corrupt("Persisted rows violate foreign key integrity")

    @staticmethod
    def _digest(queries, db):
        digest, counts = hashlib.sha256(), []
        for query in queries:
            count = 0
            digest.update(b"table")
            for row in db.execute(query):
                count += 1
                digest.update(b"row")
                for item in row:
                    if item is None:
                        kind, raw = b"null", b""
                    elif isinstance(item, bytes):
                        kind, raw = b"blob", item
                    elif type(item) is int:
                        kind, raw = b"integer", str(item).encode()
                    elif type(item) is float:
                        kind, raw = b"float", item.hex().encode()
                    else:
                        kind, raw = b"text", item.encode()
                    digest.update(kind + len(raw).to_bytes(8, "big") + raw)
            counts.append(count)
        return tuple(counts), digest.digest()

    def _field_digest(self, layout=3, *, staging=False):
        heads = "temp.field_v3_heads_stage" if staging else "main.field_channel_faults"
        audits = "temp.field_v3_audit_stage" if staging else "main.field_channel_audit"
        head_id = "source_rowid" if staging else "rowid"
        hv = "1" if layout == 1 else "state_payload_version"
        av = "1,1" if layout == 1 else "before_payload_version,after_payload_version"
        return self._digest(
            (
                f"SELECT {head_id},group_id,revision,CAST(state_json AS BLOB),{hv} "
                f"FROM {heads} ORDER BY group_id",
                "SELECT audit_id,group_id,revision,actor,changed_at,CAST(before_json AS BLOB),"
                f"CAST(after_json AS BLOB),{av} FROM {audits} ORDER BY audit_id",
            ),
            self.storage.connection,
        )

    def _nonfield_digest(self):
        db = self.storage.connection
        ddl = [
            tuple(r)
            for r in db.execute(
                "SELECT name,type,tbl_name,sql FROM main.sqlite_master ORDER BY name"
            )
            if r[0] not in _DDL and r[2] not in _OWNED
        ]
        tables = [r[0] for r in ddl if r[1] == "table" and not r[0].startswith("sqlite_")]
        queries = []
        for table in tables:
            columns = [r[1] for r in db.execute(f"PRAGMA main.table_info({_quote(table)})")]
            sql = next(r[3] for r in ddl if r[0] == table)
            # Include accessible rowid as well as every value; WITHOUT ROWID tables use all columns.
            extra = "" if "WITHOUT ROWID" in sql.upper() else "rowid,"
            queries.append(
                f"SELECT {extra}* FROM main.{_quote(table)} ORDER BY "
                + ",".join(_quote(c) for c in columns)
            )
        return ddl, self._digest(queries, db), db.execute("PRAGMA main.user_version").fetchone()[0]

    def _migrate(self, layout):
        db = self.storage.connection
        original = self._field_digest(layout)
        outside = self._nonfield_digest()
        hv = "1 AS state_payload_version" if layout == 1 else "state_payload_version"
        av = (
            "1 AS before_payload_version,1 AS after_payload_version"
            if layout == 1
            else "before_payload_version,after_payload_version"
        )
        db.execute(
            "CREATE TEMP TABLE field_v3_heads_stage AS SELECT rowid AS source_rowid,"
            f"group_id,revision,state_json,{hv} FROM main.field_channel_faults"
        )
        self._migration_step("staged_heads")
        db.execute(
            "CREATE TEMP TABLE field_v3_audit_stage AS SELECT audit_id,group_id,revision,"
            f"actor,changed_at,before_json,after_json,{av} FROM main.field_channel_audit"
        )
        self._migration_step("staged_audit")
        self._check_environment(allow_staging=True)
        if original != self._field_digest(staging=True):
            self._corrupt("Staged field evidence differs from original")
        self._migration_step("staged_verified")
        for table in ("audit", "faults", "schema"):
            db.execute(f"DROP TABLE main.field_channel_{table}")
            self._migration_step("dropped_" + table)
        for name, statement in _DDL.items():
            statement = statement.replace("CREATE TABLE ", "CREATE TABLE main.", 1)
            statement = statement.replace("CREATE TRIGGER ", "CREATE TRIGGER main.", 1)
            db.execute(statement)
            self._migration_step("created_" + name)
        db.execute("INSERT INTO main.field_channel_schema VALUES(1,3)")
        db.execute(
            "INSERT INTO "
            "main.field_channel_faults(rowid,group_id,revision,state_json,state_payload_version)"
            " "
            "SELECT source_rowid,group_id,revision,state_json,state_payload_version FROM "
            "temp.field_v3_heads_stage"
        )
        self._migration_step("copied_heads")
        db.execute(
            "INSERT INTO "
            "main.field_channel_audit(audit_id,group_id,revision,actor,changed_at,before_json,after_json,before_payload_version,after_payload_version)"
            " "
            "SELECT "
            "audit_id,group_id,revision,actor,changed_at,before_json,after_json,before_payload_version,after_payload_version"
            " FROM temp.field_v3_audit_stage"
        )
        self._migration_step("copied_audit")
        self._layout()
        self._check_foreign_keys()
        self._validate_history()
        self._check_environment(allow_staging=True)
        if original != self._field_digest() or outside != self._nonfield_digest():
            self._corrupt("Field migration changed original or nonfield evidence")
        self._migration_step("validated")
        for table in _STAGING:
            db.execute(f"DROP TABLE temp.{table}")
            self._migration_step("dropped_stage_" + table)
        self._check_environment()
        self._migration_step("staging_dropped")

    def _corrupt(self, message, error=None):
        self.storage.mark_corrupt()
        raise StorageCorruption(message) from error

    def _schema_cookie(self):
        return self.storage.connection.execute("PRAGMA main.schema_version").fetchone()[0]

    def _layout(self, *, allow_absent=False):
        db = self.storage.connection
        owned = db.execute(
            "SELECT name,type,tbl_name,sql FROM main.sqlite_master WHERE lower(name) GLOB "
            "'field_channel_*' "
            "OR lower(tbl_name) IN "
            "('field_channel_schema','field_channel_faults','field_channel_audit')"
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
        for version, expected in ((3, _DDL), (2, v2._DDL), (1, v1._DDL)):
            if objects == expected:
                if [
                    tuple(row) for row in db.execute("SELECT * FROM main.field_channel_schema")
                ] != [(1, version)]:
                    self._corrupt("Unsupported field-channel component version")
                return version
        self._corrupt("Unknown, missing or partial field-channel component")

    def _migration_step(self, stage):
        """Internal failure seam; every migration step remains in the owner transaction."""

    @staticmethod
    def _parse(version, raw):
        if type(version) is not int or version not in (1, 2, 3):
            raise ValueError("Unknown field payload discriminator")
        if version in (1, 2):
            legacy = v2.FieldChannels._parse(version, raw)
            values = legacy.model_dump()
            values.pop("payload_version")
            return FieldFaultState.model_validate(values)
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or type(payload.get("payload_version")) is not int
            or payload["payload_version"] != 3
        ):
            raise ValueError("V3 payload must explicitly declare version 3")
        return FieldFaultState.model_validate_json(raw)

    @staticmethod
    def _transition(previous, updated):
        if (
            updated.group_id != previous.group_id
            or updated.revision != previous.revision + 1
            or updated.mode == previous.mode
            or (updated.mode in {"FROZEN", *_FAILURES, "DELAY"} and previous.mode != "NORMAL")
        ):
            raise ValueError("Invalid field revision transition")

    def _rows(self, layout=3):
        suffix = ",state_payload_version" if layout != 1 else ",1"
        return self.storage.connection.execute(
            "SELECT group_id,revision,state_json"
            + suffix
            + " FROM main.field_channel_faults ORDER BY group_id"
        ).fetchall()

    def _audit(self, layout=3, *, group=None):
        suffix = ",before_payload_version,after_payload_version" if layout != 1 else ",1,1"
        query = (
            "SELECT group_id,revision,actor,changed_at,before_json,after_json"
            + suffix
            + " FROM main.field_channel_audit"
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
        if (bv, av) not in {(1, 1), (1, 2), (2, 2), (1, 3), (2, 3), (3, 3)}:
            raise ValueError("Field payload downgrade is forbidden")
        if av in (1, 2):
            v2.FieldChannels._transition(previous, updated)
        else:
            self._transition(previous, updated)
        if updated.group_id != group or encode_uint64(updated.revision) != revision:
            raise ValueError("Field audit identity mismatch")
        return previous, updated

    def _validate_history(self, layout=3):
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
                    if (state, version) != (previous, pv) or (version >= 2 and raw != previous_raw):
                        raise ValueError("Field head does not match complete audit history")
        except (TypeError, ValueError, KeyError) as error:
            self._corrupt("Invalid persisted field-channel audit history", error)

    def _before_initialize_commit(self):
        """Internal atomic component-initialization failure seam."""

    def states(self) -> tuple[FieldFaultState, ...]:
        self._check_namespace()
        version = self._schema_cookie()
        if version != self._schema_version:
            self._check_environment()
            if self._layout() != 3:
                self._corrupt("Live field component cannot migrate or downgrade")
            self._check_foreign_keys()
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
                        payload_version >= 2 and audit[5] != raw
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
        self._check_namespace()
        group = _GROUP.validate_python(group)
        intent = FieldFaultInput.model_validate_json(intent.model_dump_json())
        actor, at = _ID.validate_python(actor), _TIME.validate_python(at)
        with self.storage.transaction():
            state = next(value for value in self.states() if value.group_id == group)
            if intent.expected_revision != state.revision:
                raise FieldFaultConflict("Field revision changed; read the current configuration")
            if intent.mode == state.mode:
                if intent.delay_ms != state.delay_ms:
                    raise FieldFaultConflict("Duration change requires actual NORMAL publication")
                return state
            if state.revision == _MAX:
                raise FieldFaultConflict("Field revision exhausted")
            checkpoint = None
            if intent.mode in {"FROZEN", *_FAILURES, "DELAY"}:
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
                normal_checkpoint=checkpoint if intent.mode in {*_FAILURES, "DELAY"} else None,
                delay_ms=intent.delay_ms,
            )
            raw_before, before_version = self.storage.connection.execute(
                "SELECT state_json,state_payload_version FROM main.field_channel_faults "
                "WHERE "
                "group_id=?",
                (group,),
            ).fetchone()
            raw_after = updated.model_dump_json()
            self.storage.connection.execute(
                "UPDATE main.field_channel_faults SET "
                "revision=?,state_json=?,state_payload_version=3 "
                "WHERE group_id=?",
                (encode_uint64(updated.revision), raw_after, group),
            )
            self.storage.connection.execute(
                "INSERT INTO main.field_channel_audit "
                "(group_id,revision,actor,changed_at,before_json,after_json,before_payload_version,after_payload_version)"
                " "
                "VALUES(?,?,?,?,?,?,?,3)",
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
        self._check_namespace()
        if self.storage.connection.in_transaction:
            raise FieldFaultConflict("Uncommitted configuration cannot be published")
        acquired_at, published_at = (
            _TIME.validate_python(acquired_at),
            _TIME.validate_python(published_at),
        )
        if set(values) != set(_TAGS):
            raise ValueError("Field publication requires the complete 48-tag dictionary")
        states = {state.group_id: state for state in self.states()}
        if any(state.mode == "DELAY" for state in states.values()):
            raise FieldFaultConflict("DELAY storage checkpoint has no publisher wiring yet")
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
