# Targeted Hardening Plan

This plan focuses on high-impact reliability and security improvements without changing API contracts.

## Phase 1 (Implemented)
- Reuse one persistent HTTP client in `TwitchClient` instead of creating a new client per request.
- Add explicit `TwitchClient.close()` and call it during API shutdown.
- Centralize runtime token management:
  - short-lived WS token store,
  - EventSub message-id dedupe store.
- Centralize security helpers:
  - IP allowlist parsing/checking,
  - webhook target URL validation,
  - payload redaction utilities.
- Add Alembic migrations and remove runtime schema mutation from app startup.
- Remove stale websocket auth documentation that referenced unsupported `client_id/client_secret` query auth.

## Phase 2 (Next)
- Encrypt sensitive OAuth token columns at rest (application-level encryption key + rotation procedure).
- Add integration tests for:
  - ws-token auth flow,
  - webhook signature + dedupe behavior,
  - interest creation/rejection paths.

## Phase 3 (Next)
- Break down `EventSubManager` into smaller services:
  - reconciliation service,
  - fanout service,
  - audit/trace service.
- Add structured metrics for subscription churn, fanout failures, and Twitch API latency.
