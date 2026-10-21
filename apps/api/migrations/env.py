"""Online-only migration environment with a shared transactional schema lock."""

from alembic import context
from sqlalchemy import text

from apps.api.database import SCHEMA_LOCK_ID, create_database_engine

config = context.config


def run_online(connection):
    with connection.begin():
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_ID})
        # Some migrations use frozen raw DDL without complete ORM metadata.
        # Refuse autogeneration instead of proposing deletion of those tables.
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise ValueError("Offline migration is unsupported; use an explicit PostgreSQL connection")

connection = config.attributes.get("connection")
if connection is not None:
    run_online(connection)
else:
    engine = create_database_engine()
    try:
        with engine.connect() as connection:
            run_online(connection)
    finally:
        engine.dispose()
