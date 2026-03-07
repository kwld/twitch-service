"""Add broadcaster identity cache.

Revision ID: 20260307_0002
Revises: 20260220_0001
Create Date: 2026-03-07 00:00:00
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260307_0002"
down_revision = "20260220_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS broadcaster_identities (
            broadcaster_user_id VARCHAR(64) PRIMARY KEY,
            broadcaster_login VARCHAR(80),
            broadcaster_display_name VARCHAR(120),
            last_resolved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS broadcaster_identities")
