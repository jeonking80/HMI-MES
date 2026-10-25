"""Stable public reason codes for browser HTTP 409 responses."""

from enum import StrEnum


class ConflictReason(StrEnum):
    RESERVED_EXECUTION_NOT_SETTLED = "RESERVED_EXECUTION_NOT_SETTLED"
    WORK_ORDER_VERSION_CHANGED = "WORK_ORDER_VERSION_CHANGED"
    WORK_ORDER_ALREADY_BOUND = "WORK_ORDER_ALREADY_BOUND"
    FRESH_SOURCE_REQUIRED = "FRESH_SOURCE_REQUIRED"
    PHYSICAL_EXECUTION_MISMATCH = "PHYSICAL_EXECUTION_MISMATCH"
    ID_CONTENT_CONFLICT = "ID_CONTENT_CONFLICT"
    CONTROL_VERSION_STALE = "CONTROL_VERSION_STALE"
    STATE_NOT_ALLOWED = "STATE_NOT_ALLOWED"


class ConflictError(ValueError):
    """Domain conflict whose diagnostic message is never a public response."""

    default_reason_code = ConflictReason.STATE_NOT_ALLOWED

    def __init__(self, message: str, *, reason_code: ConflictReason | None = None):
        super().__init__(message)
        self.reason_code = ConflictReason(reason_code or self.default_reason_code)
