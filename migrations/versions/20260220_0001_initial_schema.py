"""Initial schema and compatibility migration.

Revision ID: 20260220_0001
Revises:
Create Date: 2026-02-20 00:00:00
"""

from __future__ import annotations

from alembic import op

from app.models import Base

# revision identifiers, used by Alembic.
revision = "20260220_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=True)

    # Compatibility with older data snapshots.
    op.execute(
        "UPDATE service_interests SET event_type = 'stream.online' WHERE event_type = 'channel.online'"
    )
    op.execute(
        "UPDATE service_interests SET event_type = 'stream.offline' WHERE event_type = 'channel.offline'"
    )
    op.execute(
        "UPDATE twitch_subscriptions SET event_type = 'stream.online' WHERE event_type = 'channel.online'"
    )
    op.execute(
        "UPDATE twitch_subscriptions SET event_type = 'stream.offline' WHERE event_type = 'channel.offline'"
    )

    # Compatibility with older schema snapshots.
    op.execute(
        "ALTER TABLE IF EXISTS broadcaster_authorization_requests "
        "ADD COLUMN IF NOT EXISTS redirect_url TEXT"
    )
    op.execute(
        "ALTER TABLE IF EXISTS service_interests "
        "ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE IF EXISTS service_interests "
        "ADD COLUMN IF NOT EXISTS stale_marked_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE IF EXISTS service_interests "
        "ADD COLUMN IF NOT EXISTS delete_after TIMESTAMPTZ"
    )
    op.execute(
        "UPDATE service_interests "
        "SET last_heartbeat_at = COALESCE(last_heartbeat_at, updated_at) "
        "WHERE last_heartbeat_at IS NULL"
    )


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind, checkfirst=True)
