from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.bot_auth import ensure_bot_access_token
from app.eventsub_authorization import (
    DEFAULT_AUTHORIZATION_SOURCE,
    normalize_interest_authorization_source,
    normalize_persisted_authorization_source,
)
from app.eventsub_catalog import (
    EVENTSUB_CATALOG,
    KNOWN_EVENT_TYPES,
    KNOWN_TWITCH_SCOPES,
    SOURCE_SNAPSHOT_DATE,
    SOURCE_URL,
    best_transport_for_service,
    recommended_bot_scopes,
    recommended_broadcaster_scopes,
    required_scope_any_of_groups,
    requires_moderator_user_id,
    supported_twitch_transports,
)
from app.models import (
    BotAccount,
    BroadcasterAuthorization,
    BroadcasterAuthorizationRequest,
    ChannelState,
    ServiceAccount,
    ServiceInterest,
    ServiceUserAuthRequest,
)
from app.schemas import (
    ActiveTwitchSubscriptionItem,
    ActiveTwitchSubscriptionsResponse,
    BroadcasterAuthorizationResponse,
    CreateInterestRequest,
    EventSubCatalogItem,
    EventSubCatalogResponse,
    EventSubScopeRequirement,
    InterestResponse,
    ResubscribeBroadcasterRequest,
    ResubscribeBroadcasterResponse,
    RetainedInterestStatusItem,
    RetainedInterestStatusResponse,
    ResolveEventSubScopesRequest,
    ResolveEventSubScopesResponse,
    ServiceSubscriptionItem,
    ServiceSubscriptionsResponse,
    ServiceSubscriptionTransportRow,
    ServiceSubscriptionTransportSummaryResponse,
    StartBroadcasterAuthorizationRequest,
    StartBroadcasterAuthorizationResponse,
    StartMinimalBroadcasterAuthorizationRequest,
    StartUserAuthorizationRequest,
    StartUserAuthorizationResponse,
    UserAuthorizationSessionResponse,
)


def register_service_routes(
    app: FastAPI,
    *,
    session_factory,
    twitch_client,
    eventsub_manager,
    service_auth,
    interest_registry,
    logger,
    issue_ws_token: Callable[[uuid.UUID], Awaitable[tuple[str, int]]],
    record_service_trace: Callable[..., Awaitable[None]],
    split_csv: Callable[[str | None], list[str]],
    filter_working_interests: Callable[[object, list[ServiceInterest]], Awaitable[list[ServiceInterest]]],
    service_allowed_bot_ids: Callable[[object, uuid.UUID], Awaitable[set[uuid.UUID]]],
    ensure_service_can_access_bot: Callable[[object, uuid.UUID, uuid.UUID], Awaitable[None]],
    ensure_default_stream_interests: Callable[..., Awaitable[list[ServiceInterest]]],
    validate_webhook_target_url: Callable[[str], Awaitable[None]],
    normalize_broadcaster_id_or_login: Callable[[str], str],
    broadcaster_auth_scopes: tuple[str, ...],
    service_user_auth_scopes: tuple[str, ...],
) -> None:
    scope_value_pattern = re.compile(r"^[a-z][a-z0-9:_-]*$")
    max_custom_scopes = 64
    max_scope_value_len = 80

    def _normalize_scopes(raw_scopes: list[str] | None) -> list[str]:
        if not raw_scopes:
            return []
        values = sorted({str(scope).strip() for scope in raw_scopes if str(scope).strip()})
        if len(values) > max_custom_scopes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Too many scopes provided (max {max_custom_scopes})",
            )
        for scope_value in values:
            if len(scope_value) > max_scope_value_len:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Scope '{scope_value}' exceeds max length {max_scope_value_len}",
                )
            if not scope_value_pattern.match(scope_value):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Scope '{scope_value}' has invalid format",
                )
            if scope_value not in KNOWN_TWITCH_SCOPES:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unsupported Twitch scope '{scope_value}'",
                )
        return values

    def _validate_event_types(raw_event_types: list[str] | None) -> list[str]:
        requested = [str(x).strip().lower() for x in (raw_event_types or []) if str(x).strip()]
        invalid_types = [x for x in requested if x not in KNOWN_EVENT_TYPES]
        if invalid_types:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Unsupported event_types for broadcaster authorization: "
                    + ", ".join(sorted(set(invalid_types)))
                ),
            )
        return requested

    def _normalize_raid_direction(event_type: str, raid_direction: str | None) -> str:
        normalized_event = event_type.strip().lower()
        if normalized_event != "channel.raid":
            return ""
        direction = str(raid_direction or "").strip().lower()
        if direction in {"incoming", "outgoing"}:
            return direction
        return str(getattr(eventsub_manager, "raid_direction", "incoming") or "incoming").strip().lower()

    def _resolve_scope_set(
        *,
        scope_mode: str,
        requested_event_types: list[str],
        custom_scopes: list[str],
        include_base_scope: bool,
    ) -> list[str]:
        scope_set: set[str] = set()
        if include_base_scope:
            scope_set.update(broadcaster_auth_scopes)
        if scope_mode == "minimal":
            return sorted(scope_set)
        if scope_mode == "recommended":
            for event_type in requested_event_types:
                scope_set.update(recommended_broadcaster_scopes(event_type))
            return sorted(scope_set)
        if scope_mode == "custom":
            scope_set.update(custom_scopes)
            return sorted(scope_set)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported scope_mode '{scope_mode}'",
        )

    def _scopes_satisfy_required_groups(scopes: set[str], required_scope_groups: list[set[str]]) -> bool:
        return all(any(item in scopes for item in group) for group in required_scope_groups)

    async def _resolve_interest_authorization_source(
        *,
        session,
        service: ServiceAccount,
        bot: BotAccount,
        event_type: str,
        broadcaster_user_id: str,
        requested_source: str,
        upstream_transport: str,
    ) -> str:
        normalized_source = normalize_interest_authorization_source(event_type, requested_source)
        if normalized_source == "auto" and not required_scope_any_of_groups(event_type):
            return DEFAULT_AUTHORIZATION_SOURCE

        required_scope_groups = required_scope_any_of_groups(event_type)
        if not required_scope_groups:
            return DEFAULT_AUTHORIZATION_SOURCE

        auth_row = await session.scalar(
            select(BroadcasterAuthorization).where(
                BroadcasterAuthorization.service_account_id == service.id,
                BroadcasterAuthorization.bot_account_id == bot.id,
                BroadcasterAuthorization.broadcaster_user_id == broadcaster_user_id,
            )
        )
        broadcaster_scopes = {
            x.strip() for x in str(getattr(auth_row, "scopes_csv", "") or "").split(",") if x.strip()
        }
        broadcaster_ok = _scopes_satisfy_required_groups(broadcaster_scopes, required_scope_groups)

        bot_scopes: set[str] = set()
        should_check_bot_scopes = (
            upstream_transport == "websocket"
            or normalized_source == "bot_moderator"
            or (normalized_source == "auto" and not broadcaster_ok)
        )
        if should_check_bot_scopes:
            token = await ensure_bot_access_token(session, twitch_client, bot)
            token_info = await twitch_client.validate_user_token(token)
            bot_scopes = {x.strip() for x in token_info.get("scopes", []) if str(x).strip()}

        bot_ok = _scopes_satisfy_required_groups(bot_scopes, required_scope_groups)

        moderator_bound_event = requires_moderator_user_id(event_type)

        if moderator_bound_event and upstream_transport == "websocket" and normalized_source == "broadcaster":
            normalized_source = "bot_moderator"
        if moderator_bound_event and upstream_transport == "websocket" and normalized_source == "auto":
            if bot_ok:
                return "bot_moderator"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This subscription will use EventSub websocket transport, so it must use the bot moderator identity. "
                    "Re-authorize the bot with the required moderator scopes."
                ),
            )

        if normalized_source == "broadcaster":
            if broadcaster_ok:
                return "broadcaster"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Broadcaster authorization is missing required scopes for this subscription type. "
                    "Start a broadcaster grant for this channel or switch authorization_source to bot_moderator."
                ),
            )
        if normalized_source == "bot_moderator":
            if bot_ok:
                return "bot_moderator"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Bot token is missing required scopes for this subscription type. "
                    "Re-authorize the bot with moderator scopes or use broadcaster authorization."
                ),
            )

        if broadcaster_ok:
            return "broadcaster"
        if bot_ok:
            return "bot_moderator"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No valid authorization source is available for this subscription type. "
                "Either complete a broadcaster grant for this channel or re-authorize the bot with the required moderator scopes."
            ),
        )

    @app.post("/v1/ws-token")
    async def create_service_ws_token(service: ServiceAccount = Depends(service_auth)):
        action_id = str(uuid.uuid4())
        ws_token, expires_in_seconds = await issue_ws_token(service.id)
        await record_service_trace(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.ws_token.request",
            target="/v1/ws-token",
            payload={"_action_id": action_id, "_action_status": "completed"},
        )
        payload = {
            "ws_token": ws_token,
            "token": ws_token,
            "wsToken": ws_token,
            "expires_in_seconds": expires_in_seconds,
        }
        await record_service_trace(
            service_account_id=service.id,
            direction="outgoing",
            local_transport="http",
            event_type="service.ws_token.issued",
            target="/v1/ws-token",
            payload=payload,
        )
        return payload

    @app.post("/v1/user-auth/start", response_model=StartUserAuthorizationResponse)
    async def start_service_user_authorization(
        req: StartUserAuthorizationRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        state = secrets.token_urlsafe(24)
        scopes_csv = ",".join(service_user_auth_scopes)
        async with session_factory() as session:
            session.add(
                ServiceUserAuthRequest(
                    state=state,
                    service_account_id=service.id,
                    requested_scopes_csv=scopes_csv,
                    redirect_url=str(req.redirect_url) if req.redirect_url else None,
                    status="pending",
                )
            )
            await session.commit()
        authorize_url = twitch_client.build_authorize_url_with_scopes(
            state=state,
            scopes=" ".join(service_user_auth_scopes),
            force_verify=True,
        )
        return StartUserAuthorizationResponse(
            state=state,
            authorize_url=authorize_url,
            requested_scopes=list(service_user_auth_scopes),
            expires_in_seconds=600,
        )

    @app.get("/v1/user-auth/session/{state}", response_model=UserAuthorizationSessionResponse)
    async def get_service_user_authorization_session(
        state: str,
        service: ServiceAccount = Depends(service_auth),
    ):
        async with session_factory() as session:
            row = await session.get(ServiceUserAuthRequest, state)
            if not row or row.service_account_id != service.id:
                raise HTTPException(status_code=404, detail="User auth session not found")
        return UserAuthorizationSessionResponse(
            state=row.state,
            status=row.status,
            error=row.error,
            twitch_user_id=row.twitch_user_id,
            twitch_login=row.twitch_login,
            twitch_display_name=row.twitch_display_name,
            twitch_email=row.twitch_email,
            scopes=split_csv(row.requested_scopes_csv),
            access_token=row.access_token,
            refresh_token=row.refresh_token,
            token_expires_at=row.token_expires_at,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )

    @app.get("/v1/interests", response_model=list[InterestResponse])
    async def list_interests(service: ServiceAccount = Depends(service_auth)):
        async with session_factory() as session:
            interests = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                    )
                ).all()
            )
            working = await filter_working_interests(session, interests)
        return working

    @app.get("/v1/interests/retained-status", response_model=RetainedInterestStatusResponse)
    async def list_retained_interest_status(
        bot_account_id: uuid.UUID,
        broadcaster_user_ids: str,
        service: ServiceAccount = Depends(service_auth),
    ):
        requested_ids = sorted(
            {
                str(value).strip()
                for value in broadcaster_user_ids.split(",")
                if str(value).strip()
            }
        )
        if not requested_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="broadcaster_user_ids is required",
            )

        requested_id_set = set(requested_ids)
        async with session_factory() as session:
            interests = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                    )
                ).all()
            )
            filtered_interests = [
                interest
                for interest in interests
                if interest.bot_account_id == bot_account_id
                and interest.broadcaster_user_id in requested_id_set
            ]
            working_interests = await filter_working_interests(session, filtered_interests)
            channel_states = list(
                (
                    await session.scalars(
                        select(ChannelState).where(ChannelState.bot_account_id == bot_account_id)
                    )
                ).all()
            )

        working_by_broadcaster: dict[str, list[ServiceInterest]] = {}
        for interest in working_interests:
            working_by_broadcaster.setdefault(interest.broadcaster_user_id, []).append(interest)

        all_by_broadcaster: dict[str, list[ServiceInterest]] = {}
        for interest in filtered_interests:
            all_by_broadcaster.setdefault(interest.broadcaster_user_id, []).append(interest)

        channel_state_by_broadcaster = {
            state.broadcaster_user_id: state
            for state in channel_states
            if state.broadcaster_user_id in requested_id_set
        }

        items: list[RetainedInterestStatusItem] = []
        for broadcaster_user_id in requested_ids:
            all_rows = all_by_broadcaster.get(broadcaster_user_id, [])
            working_rows = working_by_broadcaster.get(broadcaster_user_id, [])
            if not all_rows and not working_rows:
                continue
            heartbeat_values = [row.last_heartbeat_at for row in all_rows if row.last_heartbeat_at is not None]
            channel_state = channel_state_by_broadcaster.get(broadcaster_user_id)
            items.append(
                RetainedInterestStatusItem(
                    bot_account_id=bot_account_id,
                    broadcaster_user_id=broadcaster_user_id,
                    total_interest_count=len(all_rows),
                    working_interest_count=len(working_rows),
                    retained_event_types=sorted({row.event_type for row in working_rows}),
                    has_channel_state=channel_state is not None,
                    channel_is_live=(channel_state.is_live if channel_state is not None else None),
                    last_heartbeat_at=max(heartbeat_values) if heartbeat_values else None,
                )
            )

        return RetainedInterestStatusResponse(
            bot_account_id=bot_account_id,
            requested_broadcaster_user_ids=requested_ids,
            matched_broadcaster_count=len(items),
            items=items,
        )

    @app.get("/v1/subscriptions", response_model=ServiceSubscriptionsResponse)
    async def list_service_subscriptions(service: ServiceAccount = Depends(service_auth)):
        async with session_factory() as session:
            interests = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                    )
                ).all()
            )
            interests = await filter_working_interests(session, interests)
        items = [
            ServiceSubscriptionItem(
                interest_id=interest.id,
                bot_account_id=interest.bot_account_id,
                event_type=interest.event_type,
                broadcaster_user_id=interest.broadcaster_user_id,
                raid_direction=interest.raid_direction or None,
                authorization_source=normalize_persisted_authorization_source(
                    interest.event_type,
                    interest.authorization_source,
                ),
                local_transport=interest.transport,
                webhook_url=interest.webhook_url,
                created_at=interest.created_at,
                updated_at=interest.updated_at,
            )
            for interest in interests
        ]
        return ServiceSubscriptionsResponse(total=len(items), items=items)

    @app.get(
        "/v1/subscriptions/transports",
        response_model=ServiceSubscriptionTransportSummaryResponse,
    )
    async def list_service_subscription_transports(service: ServiceAccount = Depends(service_auth)):
        async with session_factory() as session:
            interests = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                    )
                ).all()
            )
            interests = await filter_working_interests(session, interests)
        by_transport: dict[str, int] = {"websocket": 0, "webhook": 0}
        by_event_type: dict[str, dict[str, int]] = {}
        for interest in interests:
            transport = interest.transport if interest.transport in {"websocket", "webhook"} else "websocket"
            by_transport[transport] += 1
            row = by_event_type.setdefault(interest.event_type, {"websocket": 0, "webhook": 0})
            row[transport] += 1
        rows = [
            ServiceSubscriptionTransportRow(
                event_type=event_type,
                websocket=counts["websocket"],
                webhook=counts["webhook"],
            )
            for event_type, counts in sorted(by_event_type.items())
        ]
        return ServiceSubscriptionTransportSummaryResponse(
            total_subscriptions=len(interests),
            by_transport={
                "websocket": by_transport["websocket"],
                "webhook": by_transport["webhook"],
            },
            by_event_type=rows,
        )

    @app.get(
        "/v1/eventsub/subscriptions/active",
        response_model=ActiveTwitchSubscriptionsResponse,
    )
    async def list_active_twitch_subscriptions_for_service(
        refresh: bool = False,
        broadcaster_user_id: str | None = None,
        service: ServiceAccount = Depends(service_auth),
    ):
        if refresh:
            snapshot, cached_at, from_cache = await eventsub_manager.get_active_subscriptions_snapshot(
                force_refresh=True
            )
        else:
            snapshot, cached_at = await eventsub_manager.get_db_active_subscriptions_snapshot()
            from_cache = True
        broadcaster_filter = str(broadcaster_user_id or "").strip()
        if broadcaster_filter:
            snapshot = [
                row for row in snapshot
                if str(row.get("broadcaster_user_id", "")).strip() == broadcaster_filter
            ]
        async with session_factory() as session:
            allowed_ids = await service_allowed_bot_ids(session, service.id)
            interests = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                    )
                ).all()
            )
            interests = await filter_working_interests(session, interests)
        interest_ids_by_key: dict[tuple[str, str, str, str, str], list[uuid.UUID]] = {}
        for interest in interests:
            if broadcaster_filter and interest.broadcaster_user_id != broadcaster_filter:
                continue
            key = (
                str(interest.bot_account_id),
                interest.event_type,
                interest.broadcaster_user_id,
                normalize_persisted_authorization_source(interest.event_type, interest.authorization_source),
                interest.raid_direction or "",
            )
            bucket = interest_ids_by_key.setdefault(key, [])
            bucket.append(interest.id)

        items: list[ActiveTwitchSubscriptionItem] = []
        for row in snapshot:
            bot_account_id = row.get("bot_account_id", "")
            status_value = str(row.get("status", "unknown"))
            if not (
                status_value.startswith("enabled")
                or status_value in {
                    "webhook_callback_verification_pending",
                    "websocket_callback_verification_pending",
                }
            ):
                continue
            try:
                bot_uuid = uuid.UUID(str(bot_account_id))
            except Exception:
                continue
            if allowed_ids and bot_uuid not in allowed_ids:
                continue
            key = (
                str(bot_uuid),
                str(row.get("event_type", "")),
                str(row.get("broadcaster_user_id", "")),
                normalize_persisted_authorization_source(
                    str(row.get("event_type", "")),
                    str(row.get("authorization_source", DEFAULT_AUTHORIZATION_SOURCE)),
                ),
                str(row.get("raid_direction", "") or ""),
            )
            matched_interest_ids = interest_ids_by_key.get(key, [])
            if not matched_interest_ids:
                continue
            items.append(
                ActiveTwitchSubscriptionItem(
                    twitch_subscription_id=str(row.get("twitch_subscription_id", "")),
                    status=status_value,
                    event_type=str(row.get("event_type", "")),
                    broadcaster_user_id=str(row.get("broadcaster_user_id", "")),
                    authorization_source=normalize_persisted_authorization_source(
                        str(row.get("event_type", "")),
                        str(row.get("authorization_source", DEFAULT_AUTHORIZATION_SOURCE)),
                    ),
                    raid_direction=str(row.get("raid_direction", "") or "") or None,
                    upstream_transport=str(row.get("upstream_transport", "websocket")),
                    bot_account_id=bot_uuid,
                    matched_interest_ids=matched_interest_ids,
                    session_id=row.get("session_id"),
                    connected_at=row.get("connected_at"),
                    disconnected_at=row.get("disconnected_at"),
                )
            )

        return ActiveTwitchSubscriptionsResponse(
            source="cache" if from_cache else "twitch_live",
            cached_at=cached_at,
            total_from_twitch=len(snapshot),
            matched_for_service=len(items),
            items=items,
        )

    @app.post(
        "/v1/eventsub/subscriptions/resubscribe",
        response_model=ResubscribeBroadcasterResponse,
    )
    async def resubscribe_broadcaster_eventsub(
        req: ResubscribeBroadcasterRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        async with session_factory() as session:
            allowed_ids = await service_allowed_bot_ids(session, service.id)
            if req.bot_account_id is not None:
                await ensure_service_can_access_bot(session, service.id, req.bot_account_id)

        result = await eventsub_manager.resubscribe_broadcaster(
            broadcaster_user_id=str(req.broadcaster_user_id),
            service_account_id=service.id,
            allowed_bot_ids=allowed_ids,
            bot_account_id=req.bot_account_id,
            force=bool(req.force),
        )
        return ResubscribeBroadcasterResponse(
            broadcaster_user_id=str(result.get("broadcaster_user_id", "")),
            bot_account_id=(
                uuid.UUID(str(result["bot_account_id"]))
                if result.get("bot_account_id")
                else None
            ),
            force=bool(result.get("force")),
            matched_interest_count=int(result.get("matched_interest_count", 0) or 0),
            ensured_interest_count=int(result.get("ensured_interest_count", 0) or 0),
            removed_subscription_count=int(result.get("removed_subscription_count", 0) or 0),
            event_types=sorted(
                {
                    str(event_type).strip()
                    for event_type in (result.get("event_types") or [])
                    if str(event_type).strip()
                }
            ),
        )

    async def _start_broadcaster_authorization_for_scopes(
        *,
        service: ServiceAccount,
        bot_account_id: uuid.UUID,
        redirect_url: str | None,
        requested_scopes: list[str],
        scope_mode: str = "recommended",
    ) -> StartBroadcasterAuthorizationResponse:
        async with session_factory() as session:
            from app.models import BotAccount

            bot = await session.get(BotAccount, bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            if not bot.enabled:
                raise HTTPException(status_code=409, detail="Bot is disabled")
            await ensure_service_can_access_bot(session, service.id, bot_account_id)

            state = secrets.token_urlsafe(24)
            scopes_csv = ",".join(requested_scopes)
            session.add(
                BroadcasterAuthorizationRequest(
                    state=state,
                    service_account_id=service.id,
                    bot_account_id=bot_account_id,
                    requested_scopes_csv=scopes_csv,
                    redirect_url=redirect_url,
                    status="pending",
                )
            )
            await session.commit()

        scopes_str = " ".join(requested_scopes)
        authorize_url = twitch_client.build_authorize_url_with_scopes(
            state=state,
            scopes=scopes_str,
            force_verify=True,
        )
        return StartBroadcasterAuthorizationResponse(
            state=state,
            authorize_url=authorize_url,
            requested_scopes=requested_scopes,
            scope_mode=scope_mode,
            expires_in_seconds=600,
        )

    @app.post(
        "/v1/broadcaster-authorizations/start",
        response_model=StartBroadcasterAuthorizationResponse,
    )
    async def start_broadcaster_authorization(
        req: StartBroadcasterAuthorizationRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        requested_event_types = _validate_event_types(req.event_types)
        normalized_custom_scopes = _normalize_scopes(req.custom_scopes)
        if req.scope_mode == "custom" and not normalized_custom_scopes and not req.include_base_scope:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="custom scope_mode requires custom_scopes or include_base_scope=true",
            )
        requested_scopes = _resolve_scope_set(
            scope_mode=req.scope_mode,
            requested_event_types=requested_event_types,
            custom_scopes=normalized_custom_scopes,
            include_base_scope=req.include_base_scope,
        )

        return await _start_broadcaster_authorization_for_scopes(
            service=service,
            bot_account_id=req.bot_account_id,
            redirect_url=str(req.redirect_url) if req.redirect_url else None,
            requested_scopes=requested_scopes,
            scope_mode=req.scope_mode,
        )

    @app.post(
        "/v1/broadcaster-authorizations/start-minimal",
        response_model=StartBroadcasterAuthorizationResponse,
    )
    async def start_broadcaster_authorization_minimal(
        req: StartMinimalBroadcasterAuthorizationRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        requested_scopes = sorted(set(broadcaster_auth_scopes))
        return await _start_broadcaster_authorization_for_scopes(
            service=service,
            bot_account_id=req.bot_account_id,
            redirect_url=str(req.redirect_url) if req.redirect_url else None,
            requested_scopes=requested_scopes,
            scope_mode="minimal",
        )

    @app.post(
        "/v1/eventsub/scopes/resolve",
        response_model=ResolveEventSubScopesResponse,
    )
    async def resolve_eventsub_scopes(
        req: ResolveEventSubScopesRequest,
        _: ServiceAccount = Depends(service_auth),
    ):
        requested_event_types = _validate_event_types(req.event_types)
        normalized_custom_scopes = _normalize_scopes(req.custom_scopes)
        if req.scope_mode == "custom" and not normalized_custom_scopes and not req.include_base_scope:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="custom scope_mode requires custom_scopes or include_base_scope=true",
            )
        resolved_scopes = _resolve_scope_set(
            scope_mode=req.scope_mode,
            requested_event_types=requested_event_types,
            custom_scopes=normalized_custom_scopes,
            include_base_scope=req.include_base_scope,
        )
        requirements = [
            EventSubScopeRequirement(
                event_type=event_type,
                required_scope_any_of_groups=[sorted(group) for group in required_scope_any_of_groups(event_type)],
                recommended_scopes=sorted(recommended_broadcaster_scopes(event_type)),
                recommended_bot_scopes=sorted(recommended_bot_scopes(event_type)),
            )
            for event_type in requested_event_types
        ]
        return ResolveEventSubScopesResponse(
            scope_mode=req.scope_mode,
            include_base_scope=req.include_base_scope,
            requested_event_types=requested_event_types,
            resolved_scopes=resolved_scopes,
            requirements=requirements,
        )

    @app.get(
        "/v1/broadcaster-authorizations",
        response_model=list[BroadcasterAuthorizationResponse],
    )
    async def list_broadcaster_authorizations(service: ServiceAccount = Depends(service_auth)):
        async with session_factory() as session:
            items = list(
                (
                    await session.scalars(
                        select(BroadcasterAuthorization).where(
                            BroadcasterAuthorization.service_account_id == service.id
                        )
                    )
                ).all()
            )
        return [
            BroadcasterAuthorizationResponse(
                id=item.id,
                service_account_id=item.service_account_id,
                bot_account_id=item.bot_account_id,
                broadcaster_user_id=item.broadcaster_user_id,
                broadcaster_login=item.broadcaster_login,
                scopes=split_csv(item.scopes_csv),
                authorized_at=item.authorized_at,
                updated_at=item.updated_at,
            )
            for item in items
        ]

    @app.get("/v1/eventsub/subscription-types", response_model=EventSubCatalogResponse)
    async def list_eventsub_subscription_types(_: ServiceAccount = Depends(service_auth)):
        webhook_preferred: list[EventSubCatalogItem] = []
        websocket_preferred: list[EventSubCatalogItem] = []
        all_items: list[EventSubCatalogItem] = []

        for entry in EVENTSUB_CATALOG:
            best_transport, reason = best_transport_for_service(
                event_type=entry.event_type,
                webhook_available=bool(
                    eventsub_manager.webhook_callback_url and eventsub_manager.webhook_secret
                ),
            )
            item = EventSubCatalogItem(
                title=entry.title,
                event_type=entry.event_type,
                version=entry.version,
                description=entry.description,
                status=entry.status,
                twitch_transports=supported_twitch_transports(entry.event_type),
                best_transport=best_transport,
                best_transport_reason=reason,
            )
            all_items.append(item)
            if best_transport == "webhook":
                webhook_preferred.append(item)
            else:
                websocket_preferred.append(item)

        return EventSubCatalogResponse(
            source_url=SOURCE_URL,
            source_snapshot_date=SOURCE_SNAPSHOT_DATE,
            total_items=len(all_items),
            total_unique_event_types=len({row.event_type for row in all_items}),
            webhook_preferred=webhook_preferred,
            websocket_preferred=websocket_preferred,
            all_items=all_items,
        )

    @app.post("/v1/interests", response_model=InterestResponse)
    async def create_interest(
        req: CreateInterestRequest,
        service: ServiceAccount = Depends(service_auth),
    ):
        action_id = str(uuid.uuid4())
        await record_service_trace(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.interest.create",
            target="/v1/interests",
            payload={
                "_action_id": action_id,
                "_action_status": "local_only",
                "bot_account_id": str(req.bot_account_id),
                "event_type": req.event_type,
                "broadcaster_user_id": req.broadcaster_user_id,
                "authorization_source": req.authorization_source,
                "transport": req.transport,
                "webhook_url": str(req.webhook_url) if req.webhook_url else None,
            },
        )
        event_type = req.event_type.strip().lower()
        raid_direction = _normalize_raid_direction(event_type, req.raid_direction)
        raw_broadcaster = normalize_broadcaster_id_or_login(req.broadcaster_user_id)
        if not raw_broadcaster:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="broadcaster_user_id is required",
            )
        if raw_broadcaster.isdigit():
            broadcaster_user_id = raw_broadcaster
        else:
            login = raw_broadcaster.lower()
            try:
                token = await twitch_client.app_access_token()
                users = await twitch_client.get_users_by_query(token, logins=[login])
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Failed resolving broadcaster login: {exc}") from exc
            if not users:
                raise HTTPException(status_code=404, detail="Broadcaster login not found")
            broadcaster_user_id = str(users[0].get("id", "")).strip()
            if not broadcaster_user_id:
                raise HTTPException(status_code=502, detail="Twitch user lookup returned empty id")
        webhook_url = str(req.webhook_url) if req.webhook_url else None

        if req.transport == "webhook" and not req.webhook_url:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="webhook_url is required for webhook transport",
            )
        if req.transport == "webhook" and webhook_url:
            await validate_webhook_target_url(webhook_url)
        if event_type not in KNOWN_EVENT_TYPES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Unsupported event_type '{req.event_type}'. "
                    "See GET /v1/eventsub/subscription-types."
                ),
            )
        async with session_factory() as session:
            bot = await session.get(BotAccount, req.bot_account_id)
            if not bot:
                raise HTTPException(status_code=404, detail="Bot not found")
            await ensure_service_can_access_bot(session, service.id, req.bot_account_id)
            preferred_transport = eventsub_manager._transport_for_event(
                event_type,
                req.authorization_source,
            )
            resolved_authorization_source = await _resolve_interest_authorization_source(
                session=session,
                service=service,
                bot=bot,
                event_type=event_type,
                broadcaster_user_id=broadcaster_user_id,
                requested_source=req.authorization_source,
                upstream_transport=preferred_transport,
            )
            upstream_transport = eventsub_manager._transport_for_event(
                event_type,
                resolved_authorization_source,
            )

            if raw_broadcaster != broadcaster_user_id:
                legacy_id = raw_broadcaster
                legacy_interests = list(
                    (
                        await session.scalars(
                            select(ServiceInterest).where(
                                ServiceInterest.service_account_id == service.id,
                                ServiceInterest.bot_account_id == req.bot_account_id,
                                ServiceInterest.broadcaster_user_id == legacy_id,
                            )
                        )
                    ).all()
                )
                for legacy in legacy_interests:
                    dupe = await session.scalar(
                        select(ServiceInterest).where(
                            ServiceInterest.service_account_id == legacy.service_account_id,
                            ServiceInterest.bot_account_id == legacy.bot_account_id,
                            ServiceInterest.event_type == legacy.event_type,
                            ServiceInterest.broadcaster_user_id == broadcaster_user_id,
                            ServiceInterest.authorization_source == legacy.authorization_source,
                            ServiceInterest.transport == legacy.transport,
                            ServiceInterest.webhook_url == legacy.webhook_url,
                            ServiceInterest.raid_direction == legacy.raid_direction,
                        )
                    )
                    if dupe:
                        await session.delete(legacy)
                    else:
                        legacy.broadcaster_user_id = broadcaster_user_id

                legacy_state = await session.scalar(
                    select(ChannelState).where(
                        ChannelState.bot_account_id == req.bot_account_id,
                        ChannelState.broadcaster_user_id == legacy_id,
                    )
                )
                if legacy_state:
                    dupe_state = await session.scalar(
                        select(ChannelState).where(
                            ChannelState.bot_account_id == req.bot_account_id,
                            ChannelState.broadcaster_user_id == broadcaster_user_id,
                        )
                    )
                    if dupe_state:
                        await session.delete(legacy_state)
                    else:
                        legacy_state.broadcaster_user_id = broadcaster_user_id
                await session.commit()

            existing = await session.scalar(
                select(ServiceInterest).where(
                    ServiceInterest.service_account_id == service.id,
                    ServiceInterest.bot_account_id == req.bot_account_id,
                    ServiceInterest.event_type == event_type,
                    ServiceInterest.broadcaster_user_id == broadcaster_user_id,
                    ServiceInterest.authorization_source == resolved_authorization_source,
                    ServiceInterest.transport == req.transport,
                    ServiceInterest.webhook_url == webhook_url,
                    ServiceInterest.raid_direction == raid_direction,
                )
            )
            created_interest = False
            if existing:
                interest = existing
                now = datetime.now(UTC)
                interest.updated_at = now
                interest.last_heartbeat_at = now
                interest.stale_marked_at = None
                interest.delete_after = None
                await session.commit()
                await session.refresh(interest)
            else:
                now = datetime.now(UTC)
                interest = ServiceInterest(
                    service_account_id=service.id,
                    bot_account_id=req.bot_account_id,
                    event_type=event_type,
                    broadcaster_user_id=broadcaster_user_id,
                    authorization_source=resolved_authorization_source,
                    transport=req.transport,
                    webhook_url=webhook_url,
                    raid_direction=raid_direction,
                    last_heartbeat_at=now,
                )
                session.add(interest)
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    interest = await session.scalar(
                        select(ServiceInterest).where(
                            ServiceInterest.service_account_id == service.id,
                            ServiceInterest.bot_account_id == req.bot_account_id,
                            ServiceInterest.event_type == event_type,
                            ServiceInterest.broadcaster_user_id == broadcaster_user_id,
                            ServiceInterest.authorization_source == resolved_authorization_source,
                            ServiceInterest.transport == req.transport,
                            ServiceInterest.webhook_url == webhook_url,
                            ServiceInterest.raid_direction == raid_direction,
                        )
                    )
                    if interest is None:
                        raise HTTPException(status_code=409, detail="Interest already exists") from None
                else:
                    await session.refresh(interest)
                    created_interest = True

        key = await interest_registry.add(interest)
        if not created_interest:
            logger.info(
                "Service interest refreshed: service=%s name=%s bot=%s broadcaster=%s event=%s auth_source=%s downstream=%s upstream=%s target=%s",
                service.id,
                service.name,
                req.bot_account_id,
                broadcaster_user_id,
                event_type,
                resolved_authorization_source,
                req.transport,
                upstream_transport,
                webhook_url or "/ws/events",
            )
            return interest
        logger.info(
            "Service interest created: service=%s name=%s bot=%s broadcaster=%s event=%s auth_source=%s downstream=%s upstream=%s target=%s",
            service.id,
            service.name,
            req.bot_account_id,
            broadcaster_user_id,
            event_type,
            resolved_authorization_source,
            req.transport,
            upstream_transport,
            webhook_url or "/ws/events",
        )
        try:
            await eventsub_manager.on_interest_added(key)
        except Exception as exc:
            logger.warning(
                "Interest created but upstream subscription ensure failed for %s/%s/%s: %s",
                key.bot_account_id,
                key.event_type,
                key.broadcaster_user_id,
                exc,
            )
            await eventsub_manager.reject_interests_for_key(
                key=key,
                reason=str(exc),
            )
            raise HTTPException(status_code=502, detail=f"Upstream subscription rejected: {exc}") from exc
        for default_interest in await ensure_default_stream_interests(
            service=service,
            bot_account_id=req.bot_account_id,
            broadcaster_user_id=broadcaster_user_id,
        ):
            default_key = await interest_registry.add(default_interest)
            try:
                await eventsub_manager.on_interest_added(default_key)
            except Exception as exc:
                logger.warning(
                    "Default interest created but upstream subscription ensure failed for %s/%s/%s: %s",
                    default_key.bot_account_id,
                    default_key.event_type,
                    default_key.broadcaster_user_id,
                    exc,
                )
                await eventsub_manager.reject_interests_for_key(
                    key=default_key,
                    reason=str(exc),
                )
        return interest

    @app.delete("/v1/interests/{interest_id}")
    async def delete_interest(interest_id: uuid.UUID, service: ServiceAccount = Depends(service_auth)):
        action_id = str(uuid.uuid4())
        await record_service_trace(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.interest.delete",
            target=f"/v1/interests/{interest_id}",
            payload={
                "_action_id": action_id,
                "_action_status": "local_only",
                "interest_id": str(interest_id),
            },
        )
        async with session_factory() as session:
            interest = await session.get(ServiceInterest, interest_id)
            if not interest or interest.service_account_id != service.id:
                raise HTTPException(status_code=404, detail="Interest not found")
            logger.info(
                "Service interest deleted: service=%s name=%s bot=%s broadcaster=%s event=%s downstream=%s target=%s",
                service.id,
                service.name,
                interest.bot_account_id,
                interest.broadcaster_user_id,
                interest.event_type,
                interest.transport,
                interest.webhook_url or "/ws/events",
            )
            await session.delete(interest)
            await session.commit()
        key, still_used = await interest_registry.remove(interest)
        await eventsub_manager.on_interest_removed(key, still_used)
        return {"deleted": True}

    @app.post("/v1/interests/{interest_id}/heartbeat")
    async def heartbeat_interest(interest_id: uuid.UUID, service: ServiceAccount = Depends(service_auth)):
        action_id = str(uuid.uuid4())
        await record_service_trace(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.interest.heartbeat",
            target=f"/v1/interests/{interest_id}/heartbeat",
            payload={
                "_action_id": action_id,
                "_action_status": "local_only",
                "interest_id": str(interest_id),
            },
        )
        async with session_factory() as session:
            interest = await session.get(ServiceInterest, interest_id)
            if not interest or interest.service_account_id != service.id:
                raise HTTPException(status_code=404, detail="Interest not found")
            touch_targets = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(
                            ServiceInterest.service_account_id == service.id,
                            ServiceInterest.bot_account_id == interest.bot_account_id,
                            ServiceInterest.broadcaster_user_id == interest.broadcaster_user_id,
                        )
                    )
                ).all()
            )
            now = datetime.now(UTC)
            for target in touch_targets:
                target.updated_at = now
                target.last_heartbeat_at = now
                target.stale_marked_at = None
                target.delete_after = None
            await session.commit()
        logger.info(
            "Interest heartbeat refreshed: service=%s name=%s touched=%d broadcaster=%s",
            service.id,
            service.name,
            len(touch_targets),
            interest.broadcaster_user_id,
        )
        return {"ok": True, "touched": len(touch_targets)}

    @app.post("/v1/interests/heartbeat")
    async def heartbeat_all_interests(service: ServiceAccount = Depends(service_auth)):
        action_id = str(uuid.uuid4())
        await record_service_trace(
            service_account_id=service.id,
            direction="incoming",
            local_transport="service_api",
            event_type="service.interests.heartbeat_all",
            target="/v1/interests/heartbeat",
            payload={"_action_id": action_id, "_action_status": "local_only"},
        )
        async with session_factory() as session:
            touch_targets = list(
                (
                    await session.scalars(
                        select(ServiceInterest).where(ServiceInterest.service_account_id == service.id)
                    )
                ).all()
            )
            now = datetime.now(UTC)
            for target in touch_targets:
                target.updated_at = now
                target.last_heartbeat_at = now
                target.stale_marked_at = None
                target.delete_after = None
            await session.commit()
        logger.info(
            "Interest heartbeat refreshed for all service interests: service=%s name=%s touched=%d",
            service.id,
            service.name,
            len(touch_targets),
        )
        return {"ok": True, "touched": len(touch_targets)}
