"""Atomic application envelope; the shared v1 ScreenMessage remains unchanged."""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from apps.api.snapshot import SampleEvidence, SnapshotHealth, SnapshotRead
from contracts.models import ContractModel, Identifier
from contracts.tags.validate import load_contract
from contracts.transport import ScreenResyncRequired, ScreenSnapshot, ScreenUpdate

APP_SCREEN_MAX_BYTES = 131072
_TAGS = frozenset(tag["logical_name"] for tag in load_contract()["tags"])


class ScreenScope(ContractModel):
    line_id: Identifier
    source_id: Identifier
    stream_id: Identifier


class ScreenDataFrame(ContractModel):
    schema_version: Literal["1.0"]
    scope: ScreenScope
    screen: Annotated[ScreenSnapshot | ScreenUpdate, Field(discriminator="kind")]
    health: SnapshotHealth
    evidence: Annotated[tuple[SampleEvidence, ...], Field(max_length=48)]
    missing_tag_ids: Annotated[tuple[Identifier, ...], Field(max_length=48)]

    @model_validator(mode="after")
    def consistent_projection(self):
        if self.scope.line_id != self.screen.view.line_id:
            raise ValueError("screen line differs from frame scope")
        observed = {sample.tag_id for sample in self.screen.view.samples}
        evidence = [item.tag_id for item in self.evidence]
        missing = set(self.missing_tag_ids)
        if len(evidence) != len(set(evidence)) or set(evidence) != observed:
            raise ValueError("evidence must match each observed tag exactly once")
        if len(missing) != len(self.missing_tag_ids) or missing & observed or not missing <= _TAGS:
            raise ValueError("missing tags must be unique known tags disjoint from observations")
        return self


class ScreenResetFrame(ContractModel):
    schema_version: Literal["1.0"]
    scope: ScreenScope
    screen: ScreenResyncRequired


AppScreenFrame = ScreenDataFrame | ScreenResetFrame
APP_SCREEN_ADAPTER = TypeAdapter(AppScreenFrame)


def make_screen_frame(
    scope: ScreenScope,
    screen: ScreenSnapshot | ScreenUpdate | ScreenResyncRequired,
    snapshot: SnapshotRead | None = None,
) -> AppScreenFrame:
    """Build from one read, never fetch metadata or serialize subscription secrets."""
    if isinstance(screen, ScreenResyncRequired):
        if snapshot is not None:
            raise ValueError("reset cannot retain capture metadata")
        return ScreenResetFrame(schema_version="1.0", scope=scope, screen=screen)
    if snapshot is None or (
        scope.source_id != snapshot.source_id
        or scope.stream_id != snapshot.stream_id
        or screen.view != snapshot.screen.view
        or screen.emitted_at != snapshot.screen.emitted_at
    ):
        raise ValueError("screen and metadata must come from the same scoped projection")
    return ScreenDataFrame(
        schema_version="1.0",
        scope=scope,
        screen=screen,
        health=snapshot.health,
        evidence=snapshot.evidence,
        missing_tag_ids=snapshot.missing_tag_ids,
    )


def app_screen_schema() -> dict:
    """Independent application schema export; does not change shared schema tools."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **APP_SCREEN_ADAPTER.json_schema(mode="serialization"),
    }
