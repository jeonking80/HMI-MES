"""Offline private App A account seed; never connects to Neon or accepts a DSN."""

from __future__ import annotations

import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path

from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = {
    "demo": {"operator", "production_manager", "equipment_admin"},
    "viewer": {"viewer"},
}


def render_demo(postgres, passwords, installer):
    """Apply canonical SQL to an empty disposable DB; export only bootstrap rows."""
    from apps.api.auth import UserAuth
    from apps.api.database import create_database_engine
    from deploy.generate_neon_init import REVISION, connect

    with connect(postgres.url, autocommit=True) as db:
        db.execute("CREATE DATABASE hmi_mes_demo")
    url = postgres.url.set(database="hmi_mes_demo")
    with connect(url) as db:
        db.execute(installer)
    engine = create_database_engine(url)
    try:
        auth = UserAuth(engine, allowed_origins={"https://demo.invalid"})
        for login, roles in ACCOUNTS.items():
            auth.bootstrap_user(login, passwords[login], roles=roles)
    finally:
        engine.dispose()
    statements = []
    with connect(url) as db:
        for table, columns, order in (
            ("users", "login,salt,password_hash,active,auth_version,must_change_password", "login"),
            ("user_roles", "login,role", "login,role"),
            ("auth_audit", "actor,target_login,action,occurred_at", "audit_id"),
        ):
            for row in db.execute(f"SELECT {columns} FROM {table} ORDER BY {order}"):
                statements.append(
                    sql.SQL("INSERT INTO public.{} ({}) VALUES ({});")
                    .format(
                        sql.Identifier(table),
                        sql.SQL(",").join(sql.Identifier(name) for name in columns.split(",")),
                        sql.SQL(",").join(sql.Literal(value) for value in row),
                    )
                    .as_string(db)
                )
    return f"""-- Private App A initial accounts. Run only after neon-init.sql, as its owner.
-- No password plaintext, source, work order, command or production fixture.
BEGIN;
SET LOCAL standard_conforming_strings = on;
DO $$ BEGIN
 IF current_database() <> 'hmi_mes_demo' THEN
  RAISE EXCEPTION 'Requires dedicated hmi_mes_demo database';
 END IF;
END $$;
SET LOCAL ROLE hmi_migrator;
SET LOCAL search_path = public;
LOCK TABLE public.users IN EXCLUSIVE MODE;
DO $$ BEGIN
 IF (SELECT count(*) FROM public.alembic_version) <> 1
 OR (SELECT version_num FROM public.alembic_version) <> '{REVISION}' THEN
  RAISE EXCEPTION 'Requires canonical14 initialization';
 END IF;
 IF EXISTS(SELECT 1 FROM public.users) THEN
  RAISE EXCEPTION 'Existing users; accounts are never reset';
 END IF;
END $$;
{chr(10).join(statements)}
COMMIT;
"""


def write_private(directory, generate):
    """Reserve both private paths exclusively before generating any credentials."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ValueError("Private output must be a real directory")
    directory.chmod(0o700)
    handles = []
    paths = [directory / "seed.sql", directory / "LOGIN.md"]
    try:
        for path in paths:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            handles.append(os.fdopen(descriptor, "w"))
        seed, login = generate()
        for handle, content in zip(handles, (seed, login), strict=True):
            handle.write(content)
    except BaseException:
        for handle in handles:
            handle.close()
        for path in paths[: len(handles)]:
            path.unlink(missing_ok=True)
        raise
    finally:
        for handle in handles:
            handle.close()


def generate():
    from deploy.generate_neon_init import disposable_postgres, render

    passwords = {login: secrets.token_urlsafe(32) for login in ACCOUNTS}
    with tempfile.TemporaryDirectory(prefix="hmi-neon-demo-") as temporary:
        with disposable_postgres(Path(temporary)) as postgres:
            seed = render_demo(postgres, passwords, render(postgres))
    login = """# 비공개 초기 로그인 정보

먼저 hmi_mes_demo에서 neon-init.sql을 실행한 뒤 같은 소유자로 seed.sql 전체를 실행하세요.
배포된 앱에서 아래 계정으로 로그인하고 최초 비밀번호를 변경하세요.
기존 계정이 있으면 SQL은 실행을 거절하며 비밀번호를 재설정하지 않습니다.
이 파일과 seed.sql을 Git, 채팅, 로그, 스크린샷에 공유하지 마세요.
조회 시연에는 viewer를 사용하고 demo 계정은 비공개로 유지하세요.
설비 연결·작업·명령·생산 실적은 생성하지 않습니다.

"""
    for name, roles in ACCOUNTS.items():
        login += f"## {name}\n\n권한: {', '.join(sorted(roles))}\n\n비밀번호: {passwords[name]}\n\n"
    return seed, login


def main():
    try:
        write_private(ROOT / ".local/neon-demo", generate)
    except Exception:
        print(
            "Private demo generation failed; check Docker and existing output files.",
            file=sys.stderr,
        )
        return 1
    print("Private files created: .local/neon-demo/seed.sql and LOGIN.md")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
