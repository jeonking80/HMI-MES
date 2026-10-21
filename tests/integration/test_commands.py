"""Durable command journal invariants against actual disposable PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, local

import pytest
import sqlalchemy as sa
from alembic import command

from apps.api.auth import AuthorizationError, Principal
from apps.api.bootstrap import LINE_ID, SITE_ID, bootstrap_reference_data
from apps.api.commands import (
    CommandConflict,
    CommandScopeError,
    apply_result,
    apply_submission,
    claim_next,
    command_permission,
    enqueue_command,
)
from apps.api.database import migration_config
from apps.api.ingestion import IngestionService
from apps.api.models import Line
from contracts.models import (
    CommandNotFound,
    CommandRequest,
    CommandResult,
    CommandSubmission,
    Parameters,
)
from contracts.transport import BatchBody, GatewayBatch, batch_digest

AT = datetime(2026, 10, 20, 12, tzinfo=UTC)
ACTOR = Principal("operator-1", frozenset({"operator"}), False)


@pytest.fixture
def journal_engine(migrated):
    bootstrap_reference_data(migrated)
    service = IngestionService(migrated)
    for suffix in ("1", "foreign"):
        service.configure_source(
            source_id=f"source-{suffix}",
            stream_id=f"stream-{suffix}",
            gateway_id=f"gateway-{suffix}",
            line_id=LINE_ID,
            capture_generation=1,
            now=AT,
        )
        service.register_execution(
            execution_id=f"run-{suffix}",
            source_id=f"source-{suffix}",
            stream_id=f"stream-{suffix}",
            work_order_id=f"job-{suffix}",
            recipe_version="recipe-1",
            counter_epoch="epoch-1",
            parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
            now=AT,
        )
    return migrated


def request(command_id="cmd-1", **changes):
    value = dict(
        command_id=command_id,
        target_id="Line",
        execution_id="run-1",
        command={"kind": "START", "args": {}},
        requested_by=ACTOR.login,
        requested_at=AT,
        expires_at=AT + timedelta(seconds=5),
        expected_control_version=42,
    )
    value.update(changes)
    return CommandRequest.model_validate(value)


def enqueue(journal_engine, value=None, **kwargs):
    with journal_engine.begin() as connection:
        return enqueue_command(
            connection,
            value or request(),
            principal=kwargs.pop("principal", ACTOR),
            line_id=kwargs.pop("line_id", LINE_ID),
            source_id=kwargs.pop("source_id", "source-1"),
            stream_id=kwargs.pop("stream_id", "stream-1"),
            now=kwargs.pop("now", AT),
            **kwargs,
        )


def counts(journal_engine):
    with journal_engine.connect() as connection:
        return tuple(
            connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
            for table in ("command_requests", "command_request_audit")
        )


def test_concurrent_same_id_returns_one_durable_request_and_actor_audit(journal_engine):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enqueue(journal_engine), range(2)))
    assert results[0] == results[1]
    assert results[0].status == "REQUESTED"
    assert results[0].delivered_at is None
    assert counts(journal_engine) == (1, 1)
    with journal_engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT r.actor,a.actor AS audit_actor,r.request,r.result,r.canonical_payload "
                    "FROM command_requests r JOIN command_request_audit a USING(command_id)"
                )
            )
            .mappings()
            .one()
        )
    assert row["actor"] == row["audit_actor"] == ACTOR.login
    assert row["request"]["expected_control_version"] == "42"
    assert row["result"]["status"] == "REQUESTED"


def test_same_id_replay_precedes_ttl_and_new_collision_never_overwrites(journal_engine):
    original = enqueue(journal_engine)
    assert enqueue(journal_engine, now=AT + timedelta(days=1)) == original
    with pytest.raises(CommandConflict):
        enqueue(journal_engine, request(expected_control_version=43))
    with pytest.raises(CommandConflict):
        enqueue(journal_engine, source_id="source-foreign", stream_id="stream-foreign")
    assert counts(journal_engine) == (1, 1)


def test_initial_expiry_is_terminal_never_delivered_and_replay_stays_expired(journal_engine):
    result = enqueue(journal_engine, now=AT + timedelta(seconds=5))
    assert result.status == "EXPIRED"
    assert result.completed_at == AT + timedelta(seconds=5)
    assert result.delivered_at is None
    assert enqueue(journal_engine, now=AT + timedelta(days=1)) == result
    assert enqueue(journal_engine, request("cmd-new")).status == "REQUESTED"


@pytest.mark.parametrize(
    "changes,scope",
    [
        ({"target_id": LINE_ID}, {}),
        ({"target_id": "Outfeed"}, {}),
        ({"execution_id": "run-foreign"}, {}),
        ({"execution_id": "never-registered"}, {}),
        ({}, {"line_id": "foreign-line"}),
        ({}, {"stream_id": "stream-foreign"}),
    ],
)
def test_only_original_execution_and_supported_plc_scope_are_allowed(
    journal_engine, changes, scope
):
    with pytest.raises(CommandScopeError):
        enqueue(journal_engine, request(**changes), **scope)
    assert counts(journal_engine) == (0, 0)


def test_failed_caller_transaction_rolls_back_request_and_audit(journal_engine):
    with pytest.raises(RuntimeError):
        with journal_engine.begin() as connection:
            enqueue_command(
                connection,
                request(),
                principal=ACTOR,
                line_id=LINE_ID,
                source_id="source-1",
                stream_id="stream-1",
                now=AT,
            )
            assert connection.in_transaction()
            raise RuntimeError("caller business write failed")
    assert counts(journal_engine) == (0, 0)
    assert enqueue(journal_engine).status == "REQUESTED"


@pytest.mark.parametrize(
    "principal,value",
    [
        (ACTOR, request(requested_by="someone-else")),
        (Principal(ACTOR.login, frozenset({"viewer"}), False), request()),
        (Principal(ACTOR.login, frozenset({"production_manager"}), False), request()),
        (Principal(ACTOR.login, ACTOR.roles, True), request()),
    ],
)
def test_authenticated_actor_and_command_permission_required(journal_engine, principal, value):
    with pytest.raises(AuthorizationError):
        enqueue(journal_engine, value, principal=principal)
    assert counts(journal_engine) == (0, 0)


def test_unresolved_source_serializes_new_requests_but_allows_safety_policy(journal_engine):
    def attempt(index):
        try:
            return enqueue(journal_engine, request(f"cmd-{index}")).status
        except CommandConflict:
            return "BLOCKED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ["BLOCKED", "REQUESTED"]
    for kind in ("PAUSE", "END_RUN", "RESET_FAULT"):
        assert (
            enqueue(journal_engine, request(f"safety-{kind}", command={"kind": kind})).status
            == "REQUESTED"
        )
    with pytest.raises(CommandConflict):
        enqueue(journal_engine, request("another-start"))


def test_unresolved_line_blocks_foreign_stream_but_distinct_lines_are_independent(journal_engine):
    enqueue(journal_engine)
    with pytest.raises(CommandConflict, match="Unresolved line"):
        enqueue(
            journal_engine,
            request("foreign-command", execution_id="run-foreign"),
            source_id="source-foreign",
            stream_id="stream-foreign",
        )
    independent_line = "independent-line"
    with journal_engine.begin() as connection:
        connection.execute(
            sa.insert(Line).values(
                id=independent_line,
                site_id=SITE_ID,
                code="INDEPENDENT",
                name="Independent line",
            )
        )
    service = IngestionService(journal_engine)
    service.configure_source(
        source_id="independent-source",
        stream_id="independent-stream",
        gateway_id="independent-gateway",
        line_id=independent_line,
        capture_generation=1,
        now=AT,
    )
    service.register_execution(
        execution_id="independent-run",
        source_id="independent-source",
        stream_id="independent-stream",
        work_order_id="independent-job",
        recipe_version="recipe-1",
        counter_epoch="independent-epoch",
        parameters=Parameters(temperature_c=150.0, speed_m_per_min=6.0),
        now=AT,
    )
    result = enqueue(
        journal_engine,
        request("independent-command", execution_id="independent-run"),
        line_id=independent_line,
        source_id="independent-source",
        stream_id="independent-stream",
    )
    assert result.status == "REQUESTED"
    assert counts(journal_engine) == (2, 2)


@pytest.mark.parametrize(
    "kind,permission",
    [
        ("LOAD_JOB", "load_job"),
        ("SET_PARAMETERS", "parameters_manage"),
    ],
)
def test_production_roles_and_unresolved_settings_policy(journal_engine, kind, permission):
    args = {
        "recipe_version": "recipe-1",
        "parameters": {"temperature_c": 150.0, "speed_m_per_min": 6.0},
    }
    if kind == "LOAD_JOB":
        args.update(work_order_id="job-1", target_quantity=10)
    value = request(command={"kind": kind, "args": args})
    assert command_permission(value) == permission
    production = Principal(ACTOR.login, frozenset({"production_manager"}), False)
    assert enqueue(journal_engine, value, principal=production).status == "REQUESTED"
    with pytest.raises(CommandConflict):
        enqueue(
            journal_engine,
            value.model_copy(update={"command_id": "next-command"}),
            principal=production,
        )
    if kind == "SET_PARAMETERS":
        with pytest.raises(AuthorizationError):
            enqueue(journal_engine, value)


def test_migration_downgrade_removes_only_command_tables(journal_engine):
    enqueue(journal_engine)
    with journal_engine.connect() as connection:
        command.downgrade(migration_config(connection), "0003_user_auth")
    with journal_engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM source_streams")).scalar_one() == 2
        assert connection.execute(sa.text("SELECT count(*) FROM users")).scalar_one() == 0
        assert (
            connection.execute(sa.text("SELECT to_regclass('command_requests')")).scalar_one()
            is None
        )
    with journal_engine.connect() as connection:
        command.upgrade(migration_config(connection), "head")
    assert counts(journal_engine) == (0, 0)


@pytest.mark.parametrize("rollback", [False, True])
def test_concurrent_ingest_and_enqueue_share_line_then_source_lock_order(journal_engine, rollback):
    """Force ingestion to own the line as enqueue starts acquiring its locks.

    With source-before-line, enqueue takes source while ingestion owns line,
    creating a real PostgreSQL FK/row-lock deadlock. Matching lock order lets
    ingestion finish and enqueue proceed (or roll back its caller transaction).
    """
    role = local()
    line_owned, enqueue_lock_started = Event(), Event()

    def before_execute(connection, cursor, statement, parameters, context, many):
        if getattr(role, "name", None) == "enqueue" and "FOR UPDATE" in statement:
            if "FROM lines" in statement:
                enqueue_lock_started.set()

    def after_execute(connection, cursor, statement, parameters, context, many):
        if "FOR UPDATE" not in statement:
            return
        if getattr(role, "name", None) == "ingest" and "FROM lines" in statement:
            line_owned.set()
            assert enqueue_lock_started.wait(5), "Enqueue never attempted its row locks"
        elif getattr(role, "name", None) == "enqueue" and "FROM source_streams" in statement:
            # This branch also makes the old, reversed order reproduce the
            # actual DB cycle, rather than just fail a statement-order assertion.
            enqueue_lock_started.set()

    body = BatchBody(
        batch_id="concurrent-ingest",
        gateway_id="gateway-1",
        source_id="source-1",
        stream_id="stream-1",
        capture_generation=1,
        capture_seq=1,
        captured_at=AT,
        samples=(
            {
                "sample_id": "concurrent-sample",
                "tag_id": "Line.State",
                "source_timestamp": AT,
                "server_timestamp": None,
                "acquired_at": AT,
                "status_code": 0,
                "reading": {"data_type": "Int32", "value": 2},
            },
        ),
    )

    def ingest():
        role.name = "ingest"
        return IngestionService(journal_engine).ingest(
            GatewayBatch(body=body, content_sha256=batch_digest(body)),
            authenticated_gateway_id="gateway-1",
            authenticated_source_id="source-1",
            authenticated_stream_id="stream-1",
            now=AT,
        )

    def journal():
        role.name = "enqueue"
        assert line_owned.wait(5), "Ingestion never acquired the line"
        try:
            with journal_engine.begin() as connection:
                connection.execute(sa.text("SET LOCAL lock_timeout='3s'"))
                result = enqueue_command(
                    connection,
                    request(),
                    principal=ACTOR,
                    line_id=LINE_ID,
                    source_id="source-1",
                    stream_id="stream-1",
                    now=AT,
                )
                if rollback:
                    raise RuntimeError("caller rolls back after concurrent ingestion")
                return result.status
        except RuntimeError:
            if not rollback:
                raise
            return "ROLLED_BACK"

    sa.event.listen(journal_engine, "before_cursor_execute", before_execute)
    sa.event.listen(journal_engine, "after_cursor_execute", after_execute)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ingested = pool.submit(ingest)
            enqueued = pool.submit(journal)
            assert ingested.result(timeout=10).durability == "COMMITTED"
            assert enqueued.result(timeout=10) == ("ROLLED_BACK" if rollback else "REQUESTED")
    finally:
        sa.event.remove(journal_engine, "before_cursor_execute", before_execute)
        sa.event.remove(journal_engine, "after_cursor_execute", after_execute)
    assert counts(journal_engine) == ((0, 0) if rollback else (1, 1))
    with journal_engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM ingest_batches")).scalar_one() == 1


GATEWAY = dict(
    authenticated_gateway_id="gateway-1",
    authenticated_source_id="source-1",
    authenticated_stream_id="stream-1",
)


def claim(engine, *, now=AT, **scope):
    with engine.begin() as connection:
        return claim_next(connection, **(GATEWAY | scope), now=now)


def reconcile(engine, remote, *, submission=False, now=AT, **scope):
    with engine.begin() as connection:
        method = apply_submission if submission else apply_result
        return method(connection, remote, **(GATEWAY | scope), now=now)


def accepted(command_id="cmd-1"):
    return CommandSubmission(command_id=command_id, disposition="ACCEPTED", received_at=AT)


def succeeded(command_id="cmd-1", execution_id="run-1"):
    return CommandResult(
        command_id=command_id,
        status="SUCCEEDED",
        delivered_at=AT,
        updated_at=AT + timedelta(seconds=1),
        completed_at=AT + timedelta(seconds=1),
        applied_control_version=43,
        evidence={"state": "RUNNING", "execution_id": execution_id},
    )


def test_concurrent_claim_exactly_one_submit_all_other_claims_query(journal_engine):
    enqueue(journal_engine)
    with ThreadPoolExecutor(max_workers=4) as pool:
        work = list(pool.map(lambda _: claim(journal_engine), range(4)))
    assert sorted(item.action for item in work) == ["QUERY", "QUERY", "QUERY", "SUBMIT"]
    assert {item.request.command_id for item in work} == {"cmd-1"}
    assert all(
        set(item.request.model_dump()) == {"schema_version", "command_id"}
        for item in work
        if item.action == "QUERY"
    )
    with journal_engine.connect() as connection:
        assert (
            connection.execute(
                sa.text(
                    "SELECT count(*) FROM command_request_audit WHERE action='DISPATCH_RESERVED'"
                )
            ).scalar_one()
            == 1
        )


def test_claim_rollback_preserves_requested_then_lost_response_is_query_only(journal_engine):
    enqueue(journal_engine)
    with pytest.raises(RuntimeError):
        with journal_engine.begin() as connection:
            assert claim_next(connection, **GATEWAY, now=AT).action == "SUBMIT"
            raise RuntimeError("claim transaction rolled back")
    assert enqueue(journal_engine).status == "REQUESTED"
    assert claim(journal_engine).action == "SUBMIT"  # Response is deliberately discarded.
    assert claim(journal_engine, now=AT + timedelta(days=1)).action == "QUERY"
    result = enqueue(journal_engine, now=AT + timedelta(days=1))
    assert result.status == "OUTCOME_UNKNOWN"
    assert result.reason_code == "DISPATCH_RESERVED"


def test_only_requested_expires_and_unknown_not_found_never_resubmits(journal_engine):
    enqueue(journal_engine)
    assert claim(journal_engine, now=AT + timedelta(seconds=5)) is None
    assert enqueue(journal_engine).status == "EXPIRED"
    enqueue(journal_engine, request("cmd-2"))
    assert claim(journal_engine).action == "SUBMIT"
    remote = CommandNotFound(code="COMMAND_NOT_FOUND", command_id="cmd-2", reason="No PLC record")
    result = reconcile(journal_engine, remote)
    assert result.status == "OUTCOME_UNKNOWN"
    assert result.reason_code == "PLC_COMMAND_NOT_FOUND"
    assert claim(journal_engine, now=AT + timedelta(days=1)).action == "QUERY"
    assert reconcile(journal_engine, accepted("cmd-2"), submission=True) == result
    with journal_engine.connect() as connection:
        evidence = connection.execute(
            sa.text("SELECT evidence FROM command_request_audit WHERE action='RESULT_NOT_FOUND'")
        ).scalar_one()
    assert evidence == remote.model_dump(mode="json")


def test_real_plc_result_timestamps_preserved_terminal_immutable_stale_acceptance_ignored(
    journal_engine,
):
    enqueue(journal_engine)
    claim(journal_engine, now=AT + timedelta(seconds=2))
    # PLC receipt clock is earlier than API reservation; copying the complete
    # validated remote result avoids combining clocks into an invalid DTO.
    result = reconcile(journal_engine, accepted(), submission=True)
    assert result.status == "ACCEPTED" and result.delivered_at == result.updated_at == AT
    final = succeeded()
    assert reconcile(journal_engine, final) == final
    assert reconcile(journal_engine, final) == final
    assert reconcile(journal_engine, accepted(), submission=True) == final
    assert claim(journal_engine) is None
    conflicting = final.model_copy(update={"applied_control_version": 44})
    with pytest.raises(CommandConflict, match="immutable"):
        reconcile(journal_engine, conflicting)
    assert enqueue(journal_engine) == final


@pytest.mark.parametrize(
    "scope",
    [
        {"authenticated_gateway_id": "wrong-gateway"},
        {
            "authenticated_source_id": "source-foreign",
            "authenticated_stream_id": "stream-foreign",
            "authenticated_gateway_id": "gateway-foreign",
        },
    ],
)
def test_dispatch_and_receipts_require_current_exact_gateway_scope(journal_engine, scope):
    enqueue(journal_engine)
    with pytest.raises(CommandScopeError):
        reconcile(journal_engine, accepted(), submission=True, **scope)
    if scope["authenticated_gateway_id"] == "wrong-gateway":
        with pytest.raises(CommandScopeError):
            claim(journal_engine, **scope)
    assert enqueue(journal_engine).status == "REQUESTED"


def test_unreserved_or_non_plc_status_and_foreign_completion_evidence_rejected(journal_engine):
    enqueue(journal_engine)
    with pytest.raises(CommandConflict, match="reservation"):
        reconcile(journal_engine, accepted(), submission=True)
    claim(journal_engine)
    with pytest.raises(CommandConflict, match="not a PLC"):
        reconcile(
            journal_engine, CommandResult(command_id="cmd-1", status="REQUESTED", updated_at=AT)
        )
    with pytest.raises(CommandScopeError, match="execution"):
        reconcile(journal_engine, succeeded(execution_id="run-foreign"))
    assert enqueue(journal_engine).status == "OUTCOME_UNKNOWN"


def test_query_polling_fairness_is_durable_across_unresolved_commands(journal_engine):
    enqueue(journal_engine)
    assert claim(journal_engine).action == "SUBMIT"
    enqueue(journal_engine, request("pause", command={"kind": "PAUSE"}))
    assert claim(journal_engine, now=AT + timedelta(milliseconds=1)).action == "SUBMIT"
    # Equal/backward API wall clocks cannot keep choosing the same command.
    work = [claim(journal_engine, now=AT) for _ in range(4)]
    assert all(item.action == "QUERY" for item in work)
    assert [item.request.command_id for item in work] == ["cmd-1", "pause", "cmd-1", "pause"]


def test_receipt_and_exact_audit_roll_back_together(journal_engine):
    enqueue(journal_engine)
    claim(journal_engine)
    before = counts(journal_engine)
    with pytest.raises(RuntimeError):
        with journal_engine.begin() as connection:
            apply_result(connection, succeeded(), **GATEWAY, now=AT)
            raise RuntimeError("receipt transaction rolls back")
    assert counts(journal_engine) == before
    assert enqueue(journal_engine).status == "OUTCOME_UNKNOWN"
    assert reconcile(journal_engine, succeeded()).status == "SUCCEEDED"


def test_gateway_binding_is_rechecked_after_reservation(journal_engine):
    enqueue(journal_engine)
    claim(journal_engine)
    with journal_engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE source_streams SET gateway_id='replacement-gateway' "
                "WHERE source_id='source-1'"
            )
        )
    with pytest.raises(CommandScopeError):
        claim(journal_engine)
    with pytest.raises(CommandScopeError):
        reconcile(journal_engine, accepted(), submission=True)
    assert claim(journal_engine, authenticated_gateway_id="replacement-gateway").action == "QUERY"
    assert (
        reconcile(
            journal_engine, succeeded(), authenticated_gateway_id="replacement-gateway"
        ).status
        == "SUCCEEDED"
    )


def test_dispatch_migration_downgrade_refuses_to_delete_new_evidence(journal_engine):
    enqueue(journal_engine)
    claim(journal_engine)
    before = counts(journal_engine)
    with journal_engine.connect() as connection:
        original_revision = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    with pytest.raises(sa.exc.IntegrityError):
        with journal_engine.connect() as connection:
            command.downgrade(migration_config(connection), "0004_commands")
    assert counts(journal_engine) == before
    with journal_engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == original_revision
        )
    assert claim(journal_engine).action == "QUERY"


def test_plc_id_collision_latches_unknown_and_same_execution_results_cannot_clear_it(
    journal_engine,
):
    enqueue(journal_engine)
    assert claim(journal_engine).action == "SUBMIT"
    collision = CommandSubmission(
        command_id="cmd-1",
        disposition="REJECTED",
        received_at=AT,
        reason_code="COMMAND_ID_CONFLICT",
        reason="ID already belongs to another canonical request",
    )
    original = reconcile(journal_engine, collision, submission=True)
    assert original.status == "OUTCOME_UNKNOWN"
    assert original.reason_code == "COMMAND_ID_CONFLICT"
    assert original.completed_at is None
    with pytest.raises(CommandConflict, match="Unresolved line"):
        enqueue(journal_engine, request("new-start"))
    # Same execution is insufficient: the PLC lookup can describe another
    # command with different arguments, version or requester under this ID.
    assert reconcile(journal_engine, succeeded()) == original
    assert reconcile(journal_engine, accepted(), submission=True) == original
    rejection = CommandResult(
        command_id="cmd-1",
        status="REJECTED",
        updated_at=AT,
        delivered_at=AT,
        completed_at=AT,
        reason_code="PLC_REJECTION",
        reason="Other payload was rejected",
    )
    assert reconcile(journal_engine, rejection) == original
    assert claim(journal_engine, now=AT + timedelta(days=1)).action == "QUERY"
    with pytest.raises(CommandConflict, match="Unresolved line"):
        enqueue(journal_engine, request("still-blocked"))
    with journal_engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT collision_latched,result FROM command_requests WHERE command_id='cmd-1'"
                )
            )
            .mappings()
            .one()
        )
        evidence = (
            connection.execute(
                sa.text(
                    "SELECT evidence FROM command_request_audit WHERE command_id='cmd-1' "
                    "AND action='RESULT_RECORDED' ORDER BY id"
                )
            )
            .scalars()
            .all()
        )
    assert row["collision_latched"] is True
    assert row["result"] == original.model_dump(mode="json")
    assert evidence == [succeeded().model_dump(mode="json"), rejection.model_dump(mode="json")]


def test_late_collision_receipt_does_not_replace_known_terminal_result(journal_engine):
    enqueue(journal_engine)
    claim(journal_engine)
    final = reconcile(journal_engine, succeeded())
    collision = CommandSubmission(
        command_id="cmd-1",
        disposition="REJECTED",
        received_at=AT,
        reason_code="COMMAND_ID_CONFLICT",
        reason="Payload collision",
    )
    assert reconcile(journal_engine, collision, submission=True) == final
    assert enqueue(journal_engine) == final
    with pytest.raises(CommandConflict, match="Unresolved line"):
        enqueue(journal_engine, request("unsafe-after-late-collision"))
    with journal_engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT collision_latched,result FROM command_requests WHERE command_id='cmd-1'"
                )
            )
            .mappings()
            .one()
        )
        receipt = connection.execute(
            sa.text("SELECT evidence FROM command_request_audit WHERE action='SUBMISSION_RECORDED'")
        ).scalar_one()
    assert row["collision_latched"] is True
    assert row["result"] == final.model_dump(mode="json")
    assert receipt == collision.model_dump(mode="json")
