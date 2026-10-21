"""Frozen component V1 DDL and payload parsers. Never broaden for V2 modes."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

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
