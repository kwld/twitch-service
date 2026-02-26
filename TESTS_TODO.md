# Backend Test Suite TODO

## 1. Test Infrastructure
- [x] Add `pytest` + `pytest-asyncio` + `httpx` test dependencies to project config
- [x] Add test config (`pytest.ini` or `pyproject` section) with async mode + markers
- [x] Create test package layout:
- [x] `tests/unit/`
- [x] `tests/integration/`
- [x] `tests/fixtures/`
- [x] Add reusable fixtures:
- [x] app fixture
- [x] async DB session fixture
- [x] temporary test DB fixture
- [x] authenticated service/admin header fixtures
- [x] mocked Twitch client fixture
- [x] Add helper factories for DB entities (bot/service/account/auth rows)
- [x] Add coverage tooling and report threshold gate

## 2. Core Security Tests
- [x] IP allowlist middleware behavior (`allow/deny`, forwarded IP, webhook bypass)
- [x] Twitch webhook signature validation (valid/invalid/missing headers)
- [x] EventSub message dedupe behavior (first accepted, replay ignored)
- [x] Webhook target URL validator SSRF protections:
- [x] scheme checks
- [x] userinfo rejection
- [x] private/loopback/link-local blocking
- [x] allowlist enforcement

## 3. Authentication & Authorization Tests
- [x] Admin auth success/failure paths
- [x] Service auth success/failure paths
- [x] WS token flow:
- [x] token issue
- [x] single-use consume
- [x] expiry rejection
- [x] Service-to-bot access policy:
- [x] unrestricted mode (no mappings)
- [x] restricted mode (allow listed only)
- [x] forbidden bot access (`403`)

## 4. Scope Matrix & OAuth Flow Tests
- [x] Full `eventsub_catalog` scope matrix sanity tests:
- [x] known event -> expected required groups
- [x] recommended scope selection from any-of groups
- [x] unknown event -> empty requirements
- [x] `/v1/eventsub/scopes/resolve`:
- [x] recommended mode
- [x] minimal mode
- [x] custom mode
- [x] invalid scope format
- [x] unknown scope rejection
- [x] unknown event type rejection
- [x] `/v1/broadcaster-authorizations/start`:
- [x] scope_mode recommended/minimal/custom behavior
- [x] include_base_scope behavior
- [x] custom mode without scopes failure
- [x] `/oauth/callback` scope enforcement:
- [x] succeeds when all requested scopes are granted
- [x] fails when requested scope missing
- [x] redirect_url success/failure query payload validation

## 5. Interest Lifecycle Tests
- [x] `POST /v1/interests` validation:
- [x] invalid event type
- [x] webhook transport requires URL
- [x] broadcaster ID/login/url normalization
- [x] Interest dedupe (same key returns existing row)
- [x] Concurrency race insert handling (integrity conflict -> reuse)
- [x] Auto-creation of default `stream.online/offline` interests
- [x] Upstream subscription failure path:
- [x] `interest.rejected` signaling
- [x] DB cleanup of rejected interests
- [x] Heartbeat endpoints:
- [x] single-interest heartbeat touches channel tuple
- [x] global heartbeat touches all service interests

## 6. EventSub Manager Behavior Tests
- [x] Startup reconciliation from DB + Twitch snapshot
- [x] Subscription ensure logic:
- [x] transport selection
- [x] version selection
- [x] required condition fields (`user_id` etc.)
- [x] required scope enforcement before create
- [x] Handling stale websocket subscriptions during reconcile
- [x] Notification fanout:
- [x] websocket delivery
- [x] webhook delivery
- [x] service trace recording
- [x] channel state updates (`stream.online/offline`)
- [x] Stale-interest pruning loop timing behavior

## 7. Route-Level Integration Tests
- [x] Health/system endpoints
- [x] Admin service-account CRUD endpoints
- [x] Accessible bots endpoint
- [x] Active Twitch subscription snapshot endpoint (`cache` vs `refresh=true`)
- [x] Twitch helper endpoints (`profiles`, `streams`) with mocked Twitch responses
- [x] Chat send + clip create endpoint behavior (happy/error paths)

## 8. WebSocket Route Tests
- [x] `/ws/events` reject missing/invalid token
- [x] `/ws/events` accept valid token and track connect/disconnect hooks
- [x] socket.io mismatch routes return expected status/messages
- [x] catch-all websocket mismatch route behavior

## 9. Regression Tests for Fixed Bugs
- [ ] DB pool regression guard (high-concurrency auth calls; no leaked sessions)
- [ ] Requested-scope callback validation regression
- [ ] Chat scope missing failure message consistency

## 10. Test Execution & Developer UX
- [ ] Add commands for:
- [ ] fast unit run
- [ ] integration run
- [ ] full test run with coverage
- [ ] Add docs section: how to run tests locally + in container
- [ ] Add CI workflow for test + coverage enforcement
- [ ] Add flaky-test safeguards (timeouts, retries where appropriate)

## 11. Quality Gate
- [ ] Minimum coverage target agreed and enforced
- [ ] All endpoints touched by at least one integration test
- [ ] Critical security/auth flows covered by deterministic tests
- [ ] Final test report + gap list (if any) documented
