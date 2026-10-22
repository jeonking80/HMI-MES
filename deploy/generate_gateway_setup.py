"""Offline gateway binding artifacts. No DSN, network call, DB write or deployment.

Run from the repository with: .venv/bin/python -m deploy.generate_gateway_setup
Supply the existing simulator and gateway durable identities; never invent new
identities for an existing runtime. Execute generated SQL explicitly as the Neon
owner authorized to SET ROLE hmi_migrator. Configure Vercel/redeploy separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from psycopg import sql
from pydantic import TypeAdapter

from apps.api.database import SCHEMA_LOCK_ID
from apps.api.internal import ServiceCredential
from contracts.models import Identifier, UInt64

REVISION = "0014_mixed_alarm_runtime"
_ID = TypeAdapter(Identifier)
_UINT = TypeAdapter(UInt64)


@dataclass(frozen=True)
class GatewaySetup:
    source_id: str
    stream_id: str
    gateway_id: str
    line_id: str
    capture_generation: int
    key_id: str
    expires_at: datetime

    def __post_init__(self):
        for value in (self.source_id, self.stream_id, self.gateway_id, self.line_id, self.key_id):
            _ID.validate_python(value)
        _UINT.validate_python(self.capture_generation)
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() != UTC.utcoffset(None)
        ):
            raise ValueError("Expiry must be explicit UTC")
        if self.expires_at <= datetime.now(UTC):
            raise ValueError("Expiry must be in the future")


def source_sql(setup: GatewaySetup) -> str:
    """Match configure_source lock order; no overwrite, recovery or cursor reset."""
    key = int.from_bytes(
        hashlib.sha256(("gateway:" + setup.gateway_id).encode()).digest()[:8], "big", signed=True
    )
    body = sql.SQL("""
DECLARE existing public.source_streams%ROWTYPE;
BEGIN
 IF (SELECT count(*) FROM public.alembic_version) <> 1
   OR (SELECT min(version_num) FROM public.alembic_version) <> {head} THEN
  RAISE EXCEPTION 'Expected schema revision required';
 END IF;
 IF (SELECT count(*) FROM public.tag_definitions) <> 48 THEN
  RAISE EXCEPTION 'Expected reference tags required';
 END IF;
 PERFORM pg_catalog.pg_advisory_xact_lock({gateway_lock});
 PERFORM id FROM public.lines WHERE id={line} FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'Registered line required'; END IF;
 SELECT * INTO existing FROM public.source_streams
   WHERE source_id={source} AND stream_id={stream} FOR UPDATE;
 IF FOUND THEN
  IF (existing.gateway_id,existing.line_id,existing.capture_generation)
    IS DISTINCT FROM ({gateway},{line},{generation}) THEN
   RAISE EXCEPTION 'Immutable source binding mismatch';
  END IF;
  IF NOT EXISTS(SELECT 1 FROM public.stream_progress
    WHERE source_id={source} AND stream_id={stream}) THEN
   RAISE EXCEPTION 'Existing source progress missing';
  END IF;
 ELSE
  INSERT INTO public.source_streams
   (source_id,stream_id,gateway_id,line_id,capture_generation,conflicted,
    reconciliation_required,registered_at)
   VALUES ({source},{stream},{gateway},{line},{generation},false,false,transaction_timestamp());
  INSERT INTO public.stream_progress
   (source_id,stream_id,verified_through_seq,latest_ingested_seq,updated_at)
   VALUES ({source},{stream},0,0,transaction_timestamp());
 END IF;
END
""").format(
        head=sql.Literal(REVISION),
        gateway_lock=sql.Literal(key),
        line=sql.Literal(setup.line_id),
        source=sql.Literal(setup.source_id),
        stream=sql.Literal(setup.stream_id),
        gateway=sql.Literal(setup.gateway_id),
        generation=sql.Literal(setup.capture_generation),
    )
    guard = (
        "BEGIN IF current_database()<>'hmi_mes_demo' THEN "
        "RAISE EXCEPTION 'Dedicated database required'; END IF; END"
    )
    return (
        sql.SQL(
            "-- Offline source binding only; no token, password or execution data.\n"
            "BEGIN;\nDO {};\nSET LOCAL ROLE hmi_migrator;\n"
            "SET LOCAL search_path=pg_catalog,public;\n"
            "SELECT pg_catalog.pg_advisory_xact_lock_shared({});\nDO {};\nCOMMIT;\n"
        )
        .format(sql.Literal(guard), sql.Literal(SCHEMA_LOCK_ID), sql.Literal(body.as_string()))
        .as_string()
    )


def generate(setup: GatewaySetup, output: Path) -> None:
    """Exclusive private directory; fail without replacing any existing artifact."""
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).digest()
    ServiceCredential(
        setup.key_id, setup.gateway_id, setup.source_id, setup.stream_id, digest, setup.expires_at
    )
    credentials = [
        dict(
            key_id=setup.key_id,
            gateway_id=setup.gateway_id,
            source_id=setup.source_id,
            stream_id=setup.stream_id,
            token_sha256=digest.hex(),
            expires_at=setup.expires_at.isoformat(),
        )
    ]
    files = {
        "token.secret": token + "\n",
        "vercel-gateway-credentials.json": json.dumps(credentials, ensure_ascii=False, indent=2)
        + "\n",
        "prepare-source.sql": source_sql(setup),
    }
    output = Path(output)
    # mkdir is exclusive even for an existing symlink. No parent directories are created.
    output.mkdir(mode=0o700)
    fd = None
    created = []
    try:
        fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        os.fchmod(fd, 0o700)
        for name, content in files.items():
            file_fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
            )
            created.append(name)
            with os.fdopen(file_fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        os.fsync(fd)
    except BaseException:
        if fd is not None:
            for name in created:
                os.unlink(name, dir_fd=fd)
        output.rmdir()
        raise
    finally:
        if fd is not None:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-id", "stream-id", "gateway-id", "line-id", "key-id"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--capture-generation", type=int, required=True)
    parser.add_argument("--expires-at", required=True, help="Explicit future UTC ISO8601 timestamp")
    parser.add_argument(
        "--output-directory", type=Path, required=True, help="New private directory"
    )
    args = parser.parse_args(argv)
    try:
        setup = GatewaySetup(
            args.source_id,
            args.stream_id,
            args.gateway_id,
            args.line_id,
            args.capture_generation,
            args.key_id,
            datetime.fromisoformat(args.expires_at),
        )
        generate(setup, args.output_directory)
    except Exception:
        print(
            "Gateway setup generation failed; check inputs and new output directory.",
            file=sys.stderr,
        )
        return 1
    print(
        "Gateway setup artifacts generated. "
        "SQL execution and Vercel configuration are separate steps."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
