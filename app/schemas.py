from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


class CreateInterestRequest(BaseModel):
    bot_account_id: uuid.UUID
    event_type: str = Field(min_length=3, max_length=120)
    broadcaster_user_id: str = Field(min_length=1, max_length=64)
    transport: Literal["websocket", "webhook"] = "websocket"
    webhook_url: Optional[HttpUrl] = None


class InterestResponse(BaseModel):
    id: uuid.UUID
    service_account_id: uuid.UUID
    bot_account_id: uuid.UUID
    event_type: str
    broadcaster_user_id: str
    transport: str
    webhook_url: str | None
    created_at: datetime


class ServiceSubscriptionItem(BaseModel):
    interest_id: uuid.UUID
    bot_account_id: uuid.UUID
    event_type: str
    broadcaster_user_id: str
    local_transport: Literal["websocket", "webhook"]
    webhook_url: str | None
    created_at: datetime
    updated_at: datetime


class ServiceSubscriptionsResponse(BaseModel):
    total: int
    items: list[ServiceSubscriptionItem]


class ServiceSubscriptionTransportRow(BaseModel):
    event_type: str
    websocket: int
    webhook: int


class ServiceSubscriptionTransportSummaryResponse(BaseModel):
    total_subscriptions: int
    by_transport: dict[Literal["websocket", "webhook"], int]
    by_event_type: list[ServiceSubscriptionTransportRow]


class EventEnvelope(BaseModel):
    id: str
    subscription_type: str
    subscription_version: str
    event: dict
    event_timestamp: datetime


class EventSubCatalogItem(BaseModel):
    title: str
    event_type: str
    version: str
    description: str
    status: Literal["stable", "new", "beta"]
    twitch_transports: list[Literal["webhook", "websocket"]]
    best_transport: Literal["webhook", "websocket"]
    best_transport_reason: str


class EventSubCatalogResponse(BaseModel):
    source_url: str
    source_snapshot_date: str
    total_items: int
    total_unique_event_types: int
    webhook_preferred: list[EventSubCatalogItem]
    websocket_preferred: list[EventSubCatalogItem]
    all_items: list[EventSubCatalogItem]


class SendChatMessageRequest(BaseModel):
    bot_account_id: uuid.UUID
    broadcaster_user_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    reply_parent_message_id: str | None = Field(default=None, min_length=1, max_length=128)
    auth_mode: Literal["auto", "app", "user"] = "auto"


class SendChatMessageResponse(BaseModel):
    broadcaster_user_id: str
    sender_user_id: str
    message_id: str
    is_sent: bool
    auth_mode_used: Literal["app", "user"]
    bot_badge_eligible: bool
    bot_badge_reason: str
    drop_reason_code: str | None = None
    drop_reason_message: str | None = None


class CreateClipRequest(BaseModel):
    bot_account_id: uuid.UUID
    broadcaster_user_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=140)
    duration: float = Field(ge=5.0, le=60.0)
    has_delay: bool = False


class CreateClipResponse(BaseModel):
    clip_id: str
    edit_url: str
    status: Literal["processing", "ready"]
    title: str
    duration: float
    broadcaster_user_id: str
    created_at: str | None = None
    url: str | None = None
    embed_url: str | None = None
    thumbnail_url: str | None = None


class StartBroadcasterAuthorizationRequest(BaseModel):
    bot_account_id: uuid.UUID
    redirect_url: HttpUrl | None = None


class StartBroadcasterAuthorizationResponse(BaseModel):
    state: str
    authorize_url: str
    requested_scopes: list[str]
    expires_in_seconds: int


class BroadcasterAuthorizationResponse(BaseModel):
    id: uuid.UUID
    service_account_id: uuid.UUID
    bot_account_id: uuid.UUID
    broadcaster_user_id: str
    broadcaster_login: str
    scopes: list[str]
    authorized_at: datetime
    updated_at: datetime


class StartUserAuthorizationRequest(BaseModel):
    redirect_url: HttpUrl | None = None


class StartUserAuthorizationResponse(BaseModel):
    state: str
    authorize_url: str
    requested_scopes: list[str]
    expires_in_seconds: int


class UserAuthorizationSessionResponse(BaseModel):
    state: str
    status: str
    error: str | None
    twitch_user_id: str | None
    twitch_login: str | None
    twitch_display_name: str | None
    twitch_email: str | None
    scopes: list[str]
    access_token: str | None
    refresh_token: str | None
    token_expires_at: datetime | None
    created_at: datetime
    completed_at: datetime | None
