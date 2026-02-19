from __future__ import annotations

import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.eventsub_catalog import (
    EVENTSUB_CATALOG,
    KNOWN_EVENT_TYPES,
    SOURCE_SNAPSHOT_DATE,
    SOURCE_URL,
    best_transport_for_service,
    recommended_broadcaster_scopes,
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
    InterestResponse,
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
    @app.post("/v1/ws-token")
    async def create_service_ws_token(service: ServiceAccount = Depends(service_auth)):
        ws_token, expires_in_seconds = await issue_ws_token(service.id)
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
        service: ServiceAccount = Depends(service_auth),
    ):
        snapshot, cached_at, from_cache = await eventsub_manager.get_active_subscriptions_snapshot(
            force_refresh=refresh
        )
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
        interest_ids_by_key: dict[tuple[str, str, str], list[uuid.UUID]] = {}
        for interest in interests:
            key = (
                str(interest.bot_account_id),
                interest.event_type,
                interest.broadcaster_user_id,
            )
            bucket = interest_ids_by_key.setdefault(key, [])
            bucket.append(interest.id)

        items: list[ActiveTwitchSubscriptionItem] = []
        for row in snapshot:
            bot_account_id = row.get("bot_account_id", "")
            status_value = str(row.get("status", "unknown"))
            if not status_value.startswith("enabled"):
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

    async def _start_broadcaster_authorization_for_scopes(
        *,
        service: ServiceAccount,
        bot_account_id: uuid.UUID,
        redirect_url: str | None,
        requested_scopes: list[str],
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
        requested_event_types = [str(x).strip().lower() for x in (req.event_types or []) if str(x).strip()]
        invalid_types = [x for x in requested_event_types if x not in KNOWN_EVENT_TYPES]
        if invalid_types:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Unsupported event_types for broadcaster authorization: "
                    + ", ".join(sorted(set(invalid_types)))
                ),
            )
        requested_scope_set = set(broadcaster_auth_scopes)
        for event_type in requested_event_types:
            requested_scope_set.update(recommended_broadcaster_scopes(event_type))
        requested_scopes = sorted(requested_scope_set)

        return await _start_broadcaster_authorization_for_scopes(
            service=service,
            bot_account_id=req.bot_account_id,
            redirect_url=str(req.redirect_url) if req.redirect_url else None,
            requested_scopes=requested_scopes,
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
        event_type = req.event_type.strip().lower()
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
                            ServiceInterest.transport == legacy.transport,
                            ServiceInterest.webhook_url == legacy.webhook_url,
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
                    ServiceInterest.transport == req.transport,
                    ServiceInterest.webhook_url == webhook_url,
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
                    transport=req.transport,
                    webhook_url=webhook_url,
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
                            ServiceInterest.transport == req.transport,
                            ServiceInterest.webhook_url == webhook_url,
                        )
                    )
                    if interest is None:
                        raise HTTPException(status_code=409, detail="Interest already exists") from None
                else:
                    await session.refresh(interest)
                    created_interest = True

        key = await interest_registry.add(interest)
        if not created_interest:
            return interest
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
        async with session_factory() as session:
            interest = await session.get(ServiceInterest, interest_id)
            if not interest or interest.service_account_id != service.id:
                raise HTTPException(status_code=404, detail="Interest not found")
            await session.delete(interest)
            await session.commit()
        key, still_used = await interest_registry.remove(interest)
        await eventsub_manager.on_interest_removed(key, still_used)
        return {"deleted": True}

    @app.post("/v1/interests/{interest_id}/heartbeat")
    async def heartbeat_interest(interest_id: uuid.UUID, service: ServiceAccount = Depends(service_auth)):
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
        return {"ok": True, "touched": len(touch_targets)}

    @app.post("/v1/interests/heartbeat")
    async def heartbeat_all_interests(service: ServiceAccount = Depends(service_auth)):
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
        return {"ok": True, "touched": len(touch_targets)}
