"""Add authorization source to EventSub interest keys.

Revision ID: 20260321_0004
Revises: 20260310_0003
Create Date: 2026-03-21 00:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "20260321_0004"
down_revision = "20260310_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "service_interests",
        sa.Column("authorization_source", sa.String(length=32), nullable=False, server_default="broadcaster"),
    )
    op.add_column(
        "twitch_subscriptions",
        sa.Column("authorization_source", sa.String(length=32), nullable=False, server_default="broadcaster"),
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
            "authorization_source",
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
            "authorization_source",
            "raid_direction",
        ],
    )


def downgrade() -> None:
    op.drop_constraint("uq_twitch_sub_dedupe", "twitch_subscriptions", type_="unique")
    op.drop_constraint("uq_interest_unique_per_service", "service_interests", type_="unique")
    op.drop_column("twitch_subscriptions", "authorization_source")
    op.drop_column("service_interests", "authorization_source")
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
    op.create_unique_constraint(
        "uq_twitch_sub_dedupe",
        "twitch_subscriptions",
        ["bot_account_id", "event_type", "broadcaster_user_id", "raid_direction"],
    )
