"""Durable database-owner recovery restriction, visible to every pooled connection."""

from sqlalchemy import text

MARKER_PREFIX = "HMI_MES_RECOVERY_REQUIRED:"


def recovery_required(connection):
    marker = connection.execute(
        text(
            "SELECT pg_catalog.shobj_description(oid,'pg_database') FROM pg_catalog.pg_database "
            "WHERE datname=pg_catalog.current_database()"
        )
    ).scalar_one()
    return isinstance(marker, str) and marker.startswith(MARKER_PREFIX)
