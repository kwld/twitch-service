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


class EventEnvelope(BaseModel):
    id: str
    subscription_type: str
    subscription_version: str
    event: dict
    event_timestamp: datetime
