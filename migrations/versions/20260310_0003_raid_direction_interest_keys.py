"""Add raid direction to interest keys.

Revision ID: 20260310_0003
Revises: 20260307_0002
Create Date: 2026-03-10 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260310_0003"
down_revision = "20260307_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_interests",
        sa.Column("raid_direction", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "twitch_subscriptions",
        sa.Column("raid_direction", sa.String(length=16), nullable=False, server_default=""),
    )
    op.drop_constraint("uq_interest_unique_per_service", "service_interests", type_="unique")
    op.create_unique_constraint(
        "uq_interest_unique_per_service",
        "service_interests",
        [
            "service_account_id",
            "bot_account_id",
            "event_type",
            "broadcaster_user_id",
            "transport",
            "webhook_url",
            "raid_direction",
        ],
    )
    op.drop_constraint("uq_twitch_sub_dedupe", "twitch_subscriptions", type_="unique")
    op.create_unique_constraint(
        "uq_twitch_sub_dedupe",
        "twitch_subscriptions",
        [
            "bot_account_id",
            "event_type",
            "broadcaster_user_id",
            "raid_direction",
        ],
    )


def downgrade() -> None:
    op.drop_constraint("uq_twitch_sub_dedupe", "twitch_subscriptions", type_="unique")
    op.drop_constraint("uq_interest_unique_per_service", "service_interests", type_="unique")
    op.drop_column("twitch_subscriptions", "raid_direction")
    op.drop_column("service_interests", "raid_direction")
    op.create_unique_constraint(
        "uq_interest_unique_per_service",
        "service_interests",
        [
            "service_account_id",
            "bot_account_id",
            "event_type",
            "broadcaster_user_id",
            "transport",
            "webhook_url",
        ],
    )
    op.create_unique_constraint(
        "uq_twitch_sub_dedupe",
        "twitch_subscriptions",
        ["bot_account_id", "event_type", "broadcaster_user_id"],
    )
