"""Dedicated guest identity marker and distributed bounded admission windows."""

from alembic import op

revision = "0017_guest_viewer"
down_revision = "0016_downtime_reasons"
branch_labels = None
depends_on = None

_BASE_ACTIONS = (
    "'user_bootstrapped','login_succeeded','password_changed',"
    "'session_revoked','roles_changed','active_changed'"
)


def upgrade():
    op.execute(
        "CREATE TABLE guest_viewer_accounts (login TEXT PRIMARY KEY "
        "REFERENCES users(login) ON DELETE RESTRICT CHECK (login='guest-viewer'), "
        "provisioned_at TIMESTAMPTZ NOT NULL)"
    )
    op.execute(
        "CREATE TABLE guest_rate_windows (peer_digest BYTEA PRIMARY KEY "
        "CHECK (octet_length(peer_digest)=32), window_started_at TIMESTAMPTZ NOT NULL, "
        "attempts INTEGER NOT NULL CHECK (attempts BETWEEN 1 AND 6))"
    )
    op.execute("ALTER TABLE auth_audit DROP CONSTRAINT auth_audit_action_check")
    op.execute(
        f"ALTER TABLE auth_audit ADD CONSTRAINT auth_audit_action_check "
        f"CHECK (action IN ({_BASE_ACTIONS},'GUEST'))"
    )


def downgrade():
    # Refuse loss of guest audit evidence: review/export before destructive rollback.
    op.execute("ALTER TABLE auth_audit DROP CONSTRAINT auth_audit_action_check")
    op.execute(
        f"ALTER TABLE auth_audit ADD CONSTRAINT auth_audit_action_check "
        f"CHECK (action IN ({_BASE_ACTIONS}))"
    )
    op.execute("DROP TABLE guest_rate_windows")
    op.execute("DROP TABLE guest_viewer_accounts")
