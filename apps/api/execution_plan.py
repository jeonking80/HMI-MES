"""Immutable execution target quantity on the caller's existing transaction."""

from sqlalchemy import Connection, text

UINT32_MAX = 2**32 - 1


class ExecutionPlanConflict(ValueError):
    """An explicit plan differs or prior registration did not preserve a plan."""


def validate_target_quantity(value: int) -> int:
    if type(value) is not int or not 1 <= value <= UINT32_MAX:
        raise ValueError("target_quantity must be a strict integer in 1..4294967295")
    return value


def register_execution_plan(
    connection: Connection, execution_id: str, target_quantity: int, *, new_execution: bool
) -> None:
    """Never commit, enrich an older execution, or reset production counters."""
    quantity = validate_target_quantity(target_quantity)
    if new_execution:
        connection.execute(
            text(
                "INSERT INTO execution_plans(execution_id,target_quantity) VALUES (:id,:quantity)"
            ),
            {"id": execution_id, "quantity": quantity},
        )
        return
    existing = connection.execute(
        text("SELECT target_quantity FROM execution_plans WHERE execution_id=:id FOR UPDATE"),
        {"id": execution_id},
    ).scalar_one_or_none()
    if existing is None or existing != quantity:
        raise ExecutionPlanConflict("Existing execution plan is absent or differs")
