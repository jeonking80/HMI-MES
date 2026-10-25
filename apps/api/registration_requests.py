"""Registration input models; persisted definitions retain mandatory identities."""

from pydantic import field_validator, model_validator

from apps.api.work_orders import RecipeDefinition, WorkOrderDefinition
from contracts.models import Identifier


class RecipeRegistrationRequest(RecipeDefinition):
    id: Identifier | None = None
    version: Identifier | None = None
    request_id: Identifier | None = None

    @field_validator("id", "version", mode="before")
    @classmethod
    def empty_identity(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def require_identity_pair(self):
        if (self.id is None) != (self.version is None):
            raise ValueError("id and version must both be supplied or omitted")
        if self.id is None and self.request_id is None:
            raise ValueError("automatic registration requires request_id")
        return self


class WorkOrderRegistrationRequest(WorkOrderDefinition):
    id: Identifier | None = None
    request_id: Identifier | None = None

    @field_validator("id", mode="before")
    @classmethod
    def empty_identity(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def require_request_id(self):
        if self.id is None and self.request_id is None:
            raise ValueError("automatic registration requires request_id")
        return self
