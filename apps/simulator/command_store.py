"""Transaction-participating command journal; physical permission belongs to the PLC."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from pydantic import TypeAdapter

from apps.simulator.storage import SimulatorStorage, StorageCorruption
from contracts.models import (
    CommandRequest,
    CommandResult,
    CommandSubmission,
    Identifier,
    UTCTimestamp,
    canonical_command_payload,
    canonical_command_retry_payload,
    materialize_load_job_policy,
)

_IDENTIFIER = TypeAdapter(Identifier)
_TIMESTAMP = TypeAdapter(UTCTimestamp)
_SCHEMA_VERSION = 1


@contextmanager
def _unit(connection: sqlite3.Connection) -> Iterator[None]:
    if not connection.in_transaction:
        raise ValueError("Command journal writes require a caller-owned transaction")
    name = "command_" + uuid4().hex
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
        connection.execute(f"RELEASE SAVEPOINT {name}")
    except BaseException:
        connection.execute(f"ROLLBACK TO SAVEPOINT {name}")
        connection.execute(f"RELEASE SAVEPOINT {name}")
        raise


@dataclass(frozen=True)
class CommandRecord:
    request: CommandRequest
    submission: CommandSubmission
    result: CommandResult


@dataclass(frozen=True)
class JournalSubmission:
    submission: CommandSubmission
    result: CommandResult | None
    created: bool


class CommandStore:
    """Use the simulator's existing connection, lock and outer commit boundary.

    ``decide`` is a synchronous PLC receipt check, never an execution callback.
    Only ``created`` accepted receipts may schedule new work. A duplicate accepted
    receipt after restart remains unresolved and must be reconciled, never rerun.
    """

    @staticmethod
    def initialize_schema(connection: sqlite3.Connection) -> None:
        """Explicit, additive journal extension version 1; never changes user_version."""
        with _unit(connection):
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "command_schema_version" in tables:
                rows = connection.execute(
                    "SELECT singleton,version FROM command_schema_version"
                ).fetchall()
                if [tuple(row) for row in rows] != [(1, _SCHEMA_VERSION)]:
                    raise ValueError("Unsupported command schema; explicit migration is required")
                expected = (
                    "command_id",
                    "request_json",
                    "canonical_payload",
                    "submission_json",
                    "result_json",
                )
                columns = tuple(
                    row[1] for row in connection.execute("PRAGMA table_info(command_journal)")
                )
                if columns != expected:
                    raise ValueError(
                        "Invalid command journal schema; explicit migration is required"
                    )
                return
            if "command_journal" in tables:
                raise ValueError("Unversioned command journal; explicit migration is required")
            connection.execute(
                "CREATE TABLE command_schema_version ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
                "version INTEGER NOT NULL CHECK(version>=1))"
            )
            connection.execute(
                "CREATE TABLE command_journal ("
                "command_id TEXT PRIMARY KEY NOT NULL,"
                "request_json TEXT NOT NULL,"
                "canonical_payload TEXT NOT NULL,"
                "submission_json TEXT NOT NULL,"
                "result_json TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO command_schema_version VALUES (1,?)", (_SCHEMA_VERSION,)
            )

    def __init__(self, storage: SimulatorStorage) -> None:
        self._storage = storage
        self._connection = storage.connection
        row = self._connection.execute(
            "SELECT version FROM command_schema_version WHERE singleton=1"
        ).fetchone()
        if row is None or row[0] != _SCHEMA_VERSION:
            raise ValueError("Unsupported command schema; explicit migration is required")

    @contextmanager
    def _write_unit(self) -> Iterator[None]:
        if not self._connection.in_transaction:
            raise ValueError("Command journal writes require a caller-owned transaction")
        with self._storage.transaction():
            yield

    def lookup(self, command_id: str) -> CommandRecord | None:
        command_id = _IDENTIFIER.validate_python(command_id)
        row = self._connection.execute(
            "SELECT request_json,canonical_payload,submission_json,result_json "
            "FROM command_journal WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            request = CommandRequest.model_validate_json(row[0])
            submission = CommandSubmission.model_validate_json(row[2])
            result = CommandResult.model_validate_json(row[3])
            if (
                {request.command_id, submission.command_id, result.command_id} != {command_id}
                or canonical_command_payload(request) != row[1]
                or result.delivered_at != submission.received_at
                or result.updated_at < submission.received_at
                or submission.received_at < request.requested_at
                or (
                    submission.disposition == "ACCEPTED"
                    and submission.received_at >= request.expires_at
                )
                or result.status not in {"ACCEPTED", "SUCCEEDED", "FAILED", "REJECTED"}
            ):
                raise ValueError("Command identity, payload or receipt disagrees")
            if submission.disposition == "REJECTED":
                if (
                    result.status != "REJECTED"
                    or result.reason_code != submission.reason_code
                    or result.reason != submission.reason
                    or result.completed_at != submission.received_at
                ):
                    raise ValueError("Rejected submission and result disagree")
            elif result.status == "REJECTED":
                raise ValueError("Accepted receipt cannot become a rejected receipt")
            return CommandRecord(request, submission, result)
        except ValueError as error:
            self._storage.mark_corrupt()
            raise StorageCorruption("Invalid persisted command journal record") from error

    def pending_records(self) -> tuple[CommandRecord, ...]:
        """List unresolved receipts for reconciliation; this never schedules execution."""
        try:
            records = tuple(
                self.lookup(row[0])
                for row in self._connection.execute(
                    "SELECT command_id FROM command_journal ORDER BY command_id COLLATE BINARY"
                ).fetchall()
            )
        except ValueError as error:
            self._storage.mark_corrupt()
            raise StorageCorruption("Invalid persisted command journal identity") from error
        return tuple(
            record
            for record in records
            if record is not None and record.result.status == "ACCEPTED"
        )

    def submit(
        self,
        request: CommandRequest,
        *,
        received_at: datetime,
        decide: Callable[[CommandRequest], CommandSubmission],
    ) -> JournalSubmission:
        """Durably record first receipt, or return the original receipt before policy checks.

        The caller must commit before returning an acknowledgement or scheduling
        physical work. Policy must not perform side effects outside this database.
        """
        request = CommandRequest.model_validate_json(
            CommandRequest.model_validate(request).model_dump_json()
        )
        received_at = _TIMESTAMP.validate_python(received_at)
        with self._write_unit():
            existing = self.lookup(request.command_id)
            if existing is not None:
                original_canonical = canonical_command_payload(existing.request)
                if original_canonical == canonical_command_retry_payload(
                    request, original_canonical
                ):
                    return JournalSubmission(existing.submission, existing.result, False)
                conflict = CommandSubmission(
                    command_id=request.command_id,
                    disposition="REJECTED",
                    received_at=received_at,
                    reason_code="COMMAND_ID_CONFLICT",
                    reason="The command ID already belongs to a different canonical request",
                )
                return JournalSubmission(conflict, None, False)
            request = materialize_load_job_policy(request)
            if received_at < request.requested_at:
                raise ValueError("Receipt cannot precede the request timestamp")
            if received_at >= request.expires_at:
                submission = CommandSubmission(
                    command_id=request.command_id,
                    disposition="REJECTED",
                    received_at=received_at,
                    reason_code="REQUEST_EXPIRED",
                    reason="The first PLC receipt arrived at or after the request deadline",
                )
            else:
                submission = CommandSubmission.model_validate_json(
                    CommandSubmission.model_validate(decide(request)).model_dump_json()
                )
                if (
                    submission.command_id != request.command_id
                    or submission.received_at != received_at
                ):
                    raise ValueError("PLC decision must match this command and receipt timestamp")
            rejected = submission.disposition == "REJECTED"
            result = CommandResult(
                command_id=request.command_id,
                status="REJECTED" if rejected else "ACCEPTED",
                updated_at=received_at,
                delivered_at=received_at,
                completed_at=received_at if rejected else None,
                reason_code=submission.reason_code,
                reason=submission.reason,
            )
            self._connection.execute(
                "INSERT INTO command_journal "
                "(command_id,request_json,canonical_payload,submission_json,result_json) "
                "VALUES (?,?,?,?,?)",
                (
                    request.command_id,
                    request.model_dump_json(),
                    canonical_command_payload(request),
                    submission.model_dump_json(),
                    result.model_dump_json(),
                ),
            )
            return JournalSubmission(submission, result, True)

    def complete(self, command_id: str, result: CommandResult) -> CommandRecord:
        """Record caller-proven success/failure together with its physical/control state.

        This method supplies no completion evidence or authorization. Neither a
        timeout nor a restart is proof of failure. Known outcomes are immutable.
        """
        result = CommandResult.model_validate_json(
            CommandResult.model_validate(result).model_dump_json()
        )
        with self._write_unit():
            existing = self.lookup(command_id)
            if existing is None:
                raise ValueError("Cannot complete an unknown command")
            if result.command_id != command_id:
                raise ValueError("Completion command ID does not match")
            if result == existing.result:
                return existing
            if existing.result.status != "ACCEPTED":
                raise ValueError("Known command outcomes cannot be overwritten")
            if result.status not in {"SUCCEEDED", "FAILED"}:
                raise ValueError("Only proven success or failure can complete an accepted command")
            if (
                result.delivered_at != existing.result.delivered_at
                or result.updated_at < existing.result.updated_at
            ):
                raise ValueError(
                    "Completion must preserve the original receipt and timestamp order"
                )
            self._connection.execute(
                "UPDATE command_journal SET result_json=? WHERE command_id=?",
                (result.model_dump_json(), command_id),
            )
            return CommandRecord(existing.request, existing.submission, result)
