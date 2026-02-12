from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class BotAccount(Base):
    __tablename__ = "bot_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    twitch_user_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    twitch_login: Mapped[str] = mapped_column(String(80), nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    interests: Mapped[list["ServiceInterest"]] = relationship(back_populates="bot_account")
    twitch_subscriptions: Mapped[list["TwitchSubscription"]] = relationship(back_populates="bot_account")
    broadcaster_authorizations: Mapped[list["BroadcasterAuthorization"]] = relationship(
        back_populates="bot_account"
    )
    broadcaster_auth_requests: Mapped[list["BroadcasterAuthorizationRequest"]] = relationship(
        back_populates="bot_account"
    )
    service_access: Mapped[list["ServiceBotAccess"]] = relationship(back_populates="bot_account")


class ServiceAccount(Base):
    __tablename__ = "service_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    client_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    client_secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    interests: Mapped[list["ServiceInterest"]] = relationship(back_populates="service_account")
    broadcaster_authorizations: Mapped[list["BroadcasterAuthorization"]] = relationship(
        back_populates="service_account"
    )
    broadcaster_auth_requests: Mapped[list["BroadcasterAuthorizationRequest"]] = relationship(
        back_populates="service_account"
    )
    runtime_stats: Mapped["ServiceRuntimeStats | None"] = relationship(
        back_populates="service_account",
        cascade="all, delete-orphan",
        passive_deletes=True,
        single_parent=True,
    )
    bot_access: Mapped[list["ServiceBotAccess"]] = relationship(back_populates="service_account")


class ServiceInterest(Base):
    __tablename__ = "service_interests"
    __table_args__ = (
        UniqueConstraint(
            "service_account_id",
            "bot_account_id",
            "event_type",
            "broadcaster_user_id",
            "transport",
            "webhook_url",
            name="uq_interest_unique_per_service",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id", ondelete="CASCADE"), nullable=False
    )
    bot_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bot_accounts.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    broadcaster_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    transport: Mapped[str] = mapped_column(String(24), nullable=False, default="websocket")
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    service_account: Mapped["ServiceAccount"] = relationship(back_populates="interests")
    bot_account: Mapped["BotAccount"] = relationship(back_populates="interests")


class TwitchSubscription(Base):
    __tablename__ = "twitch_subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "bot_account_id", "event_type", "broadcaster_user_id", name="uq_twitch_sub_dedupe"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bot_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bot_accounts.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    broadcaster_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    twitch_subscription_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    bot_account: Mapped["BotAccount"] = relationship(back_populates="twitch_subscriptions")


class ChannelState(Base):
    __tablename__ = "channel_states"
    __table_args__ = (
        UniqueConstraint("bot_account_id", "broadcaster_user_id", name="uq_channel_state_per_bot_channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bot_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bot_accounts.id", ondelete="CASCADE"), nullable=False
    )
    broadcaster_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    is_live: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    game_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OAuthCallback(Base):
    __tablename__ = "oauth_callbacks"

    state: Mapped[str] = mapped_column(String(255), primary_key=True)
    code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BroadcasterAuthorization(Base):
    __tablename__ = "broadcaster_authorizations"
    __table_args__ = (
        UniqueConstraint(
            "service_account_id",
            "bot_account_id",
            "broadcaster_user_id",
            name="uq_broadcaster_auth_per_service_bot_channel",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id", ondelete="CASCADE"), nullable=False
    )
    bot_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bot_accounts.id", ondelete="CASCADE"), nullable=False
    )
    broadcaster_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    broadcaster_login: Mapped[str] = mapped_column(String(80), nullable=False)
    scopes_csv: Mapped[str] = mapped_column(Text, nullable=False, default="")
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    service_account: Mapped["ServiceAccount"] = relationship(back_populates="broadcaster_authorizations")
    bot_account: Mapped["BotAccount"] = relationship(back_populates="broadcaster_authorizations")


class BroadcasterAuthorizationRequest(Base):
    __tablename__ = "broadcaster_authorization_requests"

    state: Mapped[str] = mapped_column(String(255), primary_key=True)
    service_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id", ondelete="CASCADE"), nullable=False
    )
    bot_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bot_accounts.id", ondelete="CASCADE"), nullable=False
    )
    requested_scopes_csv: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    broadcaster_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    service_account: Mapped["ServiceAccount"] = relationship(back_populates="broadcaster_auth_requests")
    bot_account: Mapped["BotAccount"] = relationship(back_populates="broadcaster_auth_requests")


class ServiceRuntimeStats(Base):
    __tablename__ = "service_runtime_stats"

    service_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    is_connected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active_ws_connections: Mapped[int] = mapped_column(nullable=False, default=0)
    total_ws_connects: Mapped[int] = mapped_column(nullable=False, default=0)
    total_api_requests: Mapped[int] = mapped_column(nullable=False, default=0)
    total_events_sent_ws: Mapped[int] = mapped_column(nullable=False, default=0)
    total_events_sent_webhook: Mapped[int] = mapped_column(nullable=False, default=0)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_disconnected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_api_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_event_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    service_account: Mapped["ServiceAccount"] = relationship(back_populates="runtime_stats")


class ServiceBotAccess(Base):
    __tablename__ = "service_bot_access"
    __table_args__ = (
        UniqueConstraint("service_account_id", "bot_account_id", name="uq_service_bot_access"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_accounts.id", ondelete="CASCADE"), nullable=False
    )
    bot_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bot_accounts.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    service_account: Mapped["ServiceAccount"] = relationship(back_populates="bot_access")
    bot_account: Mapped["BotAccount"] = relationship(back_populates="service_access")
