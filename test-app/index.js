import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import dotenv from "dotenv";
import WebSocket from "ws";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

dotenv.config({ path: path.join(__dirname, ".env") });

const CACHE_FILE = path.join(__dirname, ".service-account.json");

function env(name, fallback = "") {
  return process.env[name] ?? fallback;
}

function boolEnv(name, fallback = false) {
  const raw = env(name, fallback ? "true" : "false").toLowerCase();
  return raw === "1" || raw === "true" || raw === "yes" || raw === "y";
}

function intEnv(name, fallback) {
  const parsed = Number.parseInt(env(name, String(fallback)), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function requireValue(name, value) {
  if (!value) {
    throw new Error(`Missing required env: ${name}`);
  }
}

const config = {
  baseUrl: env("SERVICE_BASE_URL", "http://localhost:8080").replace(/\/+$/, ""),
  adminApiKey: env("ADMIN_API_KEY"),
  serviceClientId: env("SERVICE_CLIENT_ID"),
  serviceClientSecret: env("SERVICE_CLIENT_SECRET"),
  serviceAccountName: env("SERVICE_ACCOUNT_NAME", "test-app"),
  testBotId: env("TEST_BOT_ID"),
  testBotName: env("TEST_BOT_NAME"),
  broadcasterIds: env("TEST_BROADCASTER_IDS")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean),
  chatMessage: env("TEST_CHAT_MESSAGE", "[test-app] hello from service integration test"),
  chatAuthMode: env("TEST_CHAT_AUTH_MODE", "auto"),
  listenSeconds: intEnv("LISTEN_SECONDS", 60),
  heartbeatIntervalSeconds: intEnv("HEARTBEAT_INTERVAL_SECONDS", 20),
  broadcasterAuthWaitSeconds: intEnv("BROADCASTER_AUTH_WAIT_SECONDS", 180),
  keepInterests: boolEnv("KEEP_INTERESTS", false),
  webhookPublicUrl: env("TEST_WEBHOOK_PUBLIC_URL"),
  webhookListenPort: intEnv("TEST_WEBHOOK_LISTEN_PORT", 9090),
};

function adminHeaders() {
  return {
    "X-Admin-Key": config.adminApiKey,
    "Content-Type": "application/json",
  };
}

function serviceHeaders(creds) {
  return {
    "X-Client-Id": creds.client_id,
    "X-Client-Secret": creds.client_secret,
    "Content-Type": "application/json",
  };
}

async function httpJson(url, opts = {}) {
  const response = await fetch(url, opts);
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} ${url}: ${typeof data === "string" ? data : JSON.stringify(data)}`);
  }
  return data;
}

async function loadCachedServiceAccount() {
  try {
    const raw = await fs.readFile(CACHE_FILE, "utf8");
    const parsed = JSON.parse(raw);
    if (parsed?.client_id && parsed?.client_secret) {
      return parsed;
    }
  } catch {
    // ignore
  }
  return null;
}

async function saveCachedServiceAccount(creds) {
  const payload = {
    client_id: creds.client_id,
    client_secret: creds.client_secret,
    saved_at: new Date().toISOString(),
  };
  await fs.writeFile(CACHE_FILE, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
}

async function healthCheck() {
  const data = await httpJson(`${config.baseUrl}/health`);
  console.log("[health]", data);
}

async function listBotsAdmin() {
  const bots = await httpJson(`${config.baseUrl}/v1/bots`, {
    headers: adminHeaders(),
  });
  console.log(`[admin] bots=${bots.length}`);
  for (const bot of bots) {
    console.log(`  - ${bot.name} id=${bot.id} twitch=${bot.twitch_login}/${bot.twitch_user_id} enabled=${bot.enabled}`);
  }
  return bots;
}

async function listServiceAccountsAdmin() {
  return httpJson(`${config.baseUrl}/v1/admin/service-accounts`, {
    headers: adminHeaders(),
  });
}

async function createServiceAccountAdmin(name) {
  const url = new URL(`${config.baseUrl}/v1/admin/service-accounts`);
  url.searchParams.set("name", name);
  return httpJson(url.toString(), {
    method: "POST",
    headers: adminHeaders(),
  });
}

async function regenerateServiceSecretAdmin(clientId) {
  return httpJson(`${config.baseUrl}/v1/admin/service-accounts/${encodeURIComponent(clientId)}/regenerate`, {
    method: "POST",
    headers: adminHeaders(),
  });
}

async function validateServiceCredentials(creds) {
  await httpJson(`${config.baseUrl}/v1/interests`, {
    headers: serviceHeaders(creds),
  });
}

async function ensureServiceCredentials() {
  if (config.serviceClientId && config.serviceClientSecret) {
    const creds = { client_id: config.serviceClientId, client_secret: config.serviceClientSecret };
    await validateServiceCredentials(creds);
    console.log("[service] using SERVICE_CLIENT_ID/SERVICE_CLIENT_SECRET from env");
    return creds;
  }

  requireValue("ADMIN_API_KEY", config.adminApiKey);

  const cached = await loadCachedServiceAccount();
  if (cached) {
    try {
      await validateServiceCredentials(cached);
      console.log("[service] using cached credentials from .service-account.json");
      return cached;
    } catch {
      console.log("[service] cached credentials invalid; will recover via admin API");
    }
  }

  const accounts = await listServiceAccountsAdmin();
  const existing = accounts.find((x) => x.name === config.serviceAccountName);
  if (!existing) {
    const created = await createServiceAccountAdmin(config.serviceAccountName);
    const creds = { client_id: created.client_id, client_secret: created.client_secret };
    await saveCachedServiceAccount(creds);
    console.log(`[admin] created service account '${config.serviceAccountName}'`);
    return creds;
  }

  const regenerated = await regenerateServiceSecretAdmin(existing.client_id);
  const creds = { client_id: regenerated.client_id, client_secret: regenerated.client_secret };
  await saveCachedServiceAccount(creds);
  console.log(`[admin] regenerated secret for service '${config.serviceAccountName}'`);
  return creds;
}

function selectBot(bots) {
  if (!bots.length) {
    throw new Error("No bots found in service.");
  }
  if (config.testBotId) {
    const byId = bots.find((b) => b.id === config.testBotId);
    if (!byId) {
      throw new Error(`TEST_BOT_ID not found: ${config.testBotId}`);
    }
    return byId;
  }
  if (config.testBotName) {
    const byName = bots.find((b) => b.name === config.testBotName || b.twitch_login === config.testBotName);
    if (!byName) {
      throw new Error(`TEST_BOT_NAME not found: ${config.testBotName}`);
    }
    return byName;
  }
  const enabled = bots.find((b) => b.enabled);
  if (!enabled) {
    throw new Error("No enabled bot accounts found.");
  }
  return enabled;
}

async function listCatalog(creds) {
  const catalog = await httpJson(`${config.baseUrl}/v1/eventsub/subscription-types`, {
    headers: serviceHeaders(creds),
  });
  console.log(
    `[service] catalog total=${catalog.total_items} unique=${catalog.total_unique_event_types} source=${catalog.source_snapshot_date}`,
  );
  return catalog;
}

async function listAccessibleBots(creds) {
  const payload = await httpJson(`${config.baseUrl}/v1/bots/accessible`, {
    headers: serviceHeaders(creds),
  });
  console.log(`[service] accessible bots mode=${payload.access_mode} count=${payload.bots.length}`);
  for (const bot of payload.bots) {
    console.log(`  - ${bot.name} id=${bot.id} twitch=${bot.twitch_login}/${bot.twitch_user_id}`);
  }
  return payload;
}

async function listBroadcasterAuthorizations(creds) {
  const rows = await httpJson(`${config.baseUrl}/v1/broadcaster-authorizations`, {
    headers: serviceHeaders(creds),
  });
  console.log(`[service] broadcaster authorizations=${rows.length}`);
  for (const row of rows) {
    console.log(`  - broadcaster=${row.broadcaster_login}/${row.broadcaster_user_id} bot=${row.bot_account_id}`);
  }
  return rows;
}

async function startBroadcasterAuthorization(creds, botId) {
  const payload = await httpJson(`${config.baseUrl}/v1/broadcaster-authorizations/start`, {
    method: "POST",
    headers: serviceHeaders(creds),
    body: JSON.stringify({ bot_account_id: botId }),
  });
  console.log("[service] broadcaster authorization started");
  console.log(`  state: ${payload.state}`);
  console.log(`  requested_scopes: ${payload.requested_scopes.join(",")}`);
  console.log(`  authorize_url: ${payload.authorize_url}`);
  return payload;
}

async function listInterests(creds) {
  return httpJson(`${config.baseUrl}/v1/interests`, {
    headers: serviceHeaders(creds),
  });
}

async function createInterest(creds, payload) {
  return httpJson(`${config.baseUrl}/v1/interests`, {
    method: "POST",
    headers: serviceHeaders(creds),
    body: JSON.stringify(payload),
  });
}

async function heartbeatInterest(creds, interestId) {
  return httpJson(`${config.baseUrl}/v1/interests/${interestId}/heartbeat`, {
    method: "POST",
    headers: serviceHeaders(creds),
  });
}

async function deleteInterest(creds, interestId) {
  return httpJson(`${config.baseUrl}/v1/interests/${interestId}`, {
    method: "DELETE",
    headers: serviceHeaders(creds),
  });
}

async function fetchProfiles(creds, botId, broadcasterIds) {
  const url = new URL(`${config.baseUrl}/v1/twitch/profiles`);
  url.searchParams.set("bot_account_id", botId);
  url.searchParams.set("user_ids", broadcasterIds.join(","));
  return httpJson(url.toString(), { headers: serviceHeaders(creds) });
}

async function fetchStreamsStatus(creds, botId, broadcasterIds) {
  const url = new URL(`${config.baseUrl}/v1/twitch/streams/status`);
  url.searchParams.set("bot_account_id", botId);
  url.searchParams.set("broadcaster_user_ids", broadcasterIds.join(","));
  return httpJson(url.toString(), { headers: serviceHeaders(creds) });
}

async function fetchInterestedStreamsStatus(creds) {
  return httpJson(`${config.baseUrl}/v1/twitch/streams/status/interested`, {
    headers: serviceHeaders(creds),
  });
}

async function sendChatMessage(creds, payload) {
  return httpJson(`${config.baseUrl}/v1/twitch/chat/messages`, {
    method: "POST",
    headers: serviceHeaders(creds),
    body: JSON.stringify(payload),
  });
}

function connectEventsWs(creds, onEvent) {
  const wsBase = config.baseUrl.replace(/^http/, "ws");
  const wsUrl = new URL(`${wsBase}/ws/events`);
  wsUrl.searchParams.set("client_id", creds.client_id);
  wsUrl.searchParams.set("client_secret", creds.client_secret);

  const ws = new WebSocket(wsUrl.toString());
  const pingTimer = setInterval(() => {
    if (ws.readyState === ws.OPEN) {
      ws.send("keepalive");
    }
  }, 15000);

  ws.on("open", () => {
    console.log("[ws] connected");
  });
  ws.on("message", (raw) => {
    try {
      const data = JSON.parse(String(raw));
      const eventType = data.subscription_type || data.type || "unknown";
      console.log(`[ws:event] ${eventType} id=${data.id ?? "n/a"}`);
      if (eventType === "channel.chat.message") {
        const chatter = data?.event?.chatter_user_login || data?.event?.chatter_user_name || "unknown";
        const text = data?.event?.message?.text || "";
        console.log(`[chat] ${chatter}: ${text}`);
      }
      onEvent?.(data);
    } catch (err) {
      console.log("[ws] non-json message", err);
    }
  });
  ws.on("close", (code, reason) => {
    clearInterval(pingTimer);
    console.log(`[ws] closed code=${code} reason=${String(reason)}`);
  });
  ws.on("error", (err) => {
    console.log("[ws] error", err.message);
  });

  return ws;
}

function maybeStartWebhookListener() {
  if (!config.webhookPublicUrl) {
    return null;
  }

  const server = http.createServer(async (req, res) => {
    if (req.method !== "POST" || req.url !== "/service-webhook") {
      res.statusCode = 404;
      res.end("not found");
      return;
    }

    const chunks = [];
    for await (const chunk of req) {
      chunks.push(chunk);
    }
    const raw = Buffer.concat(chunks).toString("utf8");
    try {
      const payload = JSON.parse(raw);
      const eventType = payload.subscription_type || payload.type || "unknown";
      console.log(`[webhook:event] ${eventType} id=${payload.id ?? "n/a"}`);
    } catch {
      console.log("[webhook:event] invalid json body");
    }
    res.statusCode = 204;
    res.end();
  });

  server.listen(config.webhookListenPort, "0.0.0.0", () => {
    console.log(`[webhook] listening on :${config.webhookListenPort}/service-webhook`);
    console.log(`[webhook] public url expected at: ${config.webhookPublicUrl}`);
  });

  return server;
}

async function runFull() {
  requireValue("ADMIN_API_KEY", config.adminApiKey);
  if (!config.broadcasterIds.length) {
    throw new Error("TEST_BROADCASTER_IDS must include at least one broadcaster user id.");
  }

  await healthCheck();

  const bots = await listBotsAdmin();
  const bot = selectBot(bots);
  console.log(`[admin] selected bot: ${bot.name} (${bot.id})`);

  const creds = await ensureServiceCredentials();
  await validateServiceCredentials(creds);
  console.log(`[service] authenticated as client_id=${creds.client_id}`);
  await listAccessibleBots(creds);

  await listCatalog(creds);

  const authRows = await listBroadcasterAuthorizations(creds);
  const authForSelectedBot = authRows.filter((x) => x.bot_account_id === bot.id);
  const authIds = new Set(authForSelectedBot.map((x) => x.broadcaster_user_id));
  const missingAuth = config.broadcasterIds.filter((id) => !authIds.has(id));
  if (missingAuth.length) {
    console.log(`[service] missing broadcaster channel authorization for bot on: ${missingAuth.join(", ")}`);
    console.log("[service] starting OAuth flow; complete this in browser as the streamer.");
    await startBroadcasterAuthorization(creds, bot.id);
    console.log(`[service] waiting up to ${config.broadcasterAuthWaitSeconds}s for authorization records...`);
    const until = Date.now() + config.broadcasterAuthWaitSeconds * 1000;
    while (Date.now() < until) {
      await new Promise((r) => setTimeout(r, 5000));
      const rows = await listBroadcasterAuthorizations(creds);
      const rowsForBot = rows.filter((x) => x.bot_account_id === bot.id);
      const rowIds = new Set(rowsForBot.map((x) => x.broadcaster_user_id));
      const stillMissing = config.broadcasterIds.filter((id) => !rowIds.has(id));
      if (!stillMissing.length) {
        console.log("[service] broadcaster authorizations present for configured channels.");
        break;
      }
    }
  } else {
    console.log("[service] broadcaster authorizations already present for configured channels.");
  }

  const webhookServer = maybeStartWebhookListener();
  const ws = connectEventsWs(creds);

  const createdInterestIds = [];
  try {
    for (const broadcasterId of config.broadcasterIds) {
      const chatInterest = await createInterest(creds, {
        bot_account_id: bot.id,
        event_type: "channel.chat.message",
        broadcaster_user_id: broadcasterId,
        transport: "websocket",
      });
      createdInterestIds.push(chatInterest.id);
      console.log(`[interest] chat websocket created/reused id=${chatInterest.id} broadcaster=${broadcasterId}`);
    }

    const onlineInterest = await createInterest(creds, {
      bot_account_id: bot.id,
      event_type: "channel.online",
      broadcaster_user_id: config.broadcasterIds[0],
      transport: "websocket",
    });
    createdInterestIds.push(onlineInterest.id);
    console.log(`[interest] online websocket created/reused id=${onlineInterest.id}`);

    if (config.webhookPublicUrl) {
      const webhookInterest = await createInterest(creds, {
        bot_account_id: bot.id,
        event_type: "channel.offline",
        broadcaster_user_id: config.broadcasterIds[0],
        transport: "webhook",
        webhook_url: config.webhookPublicUrl,
      });
      createdInterestIds.push(webhookInterest.id);
      console.log(`[interest] offline webhook created/reused id=${webhookInterest.id}`);
    } else {
      console.log("[interest] TEST_WEBHOOK_PUBLIC_URL not set; webhook transport test skipped.");
    }

    const allInterests = await listInterests(creds);
    console.log(`[service] total interests currently visible to this service: ${allInterests.length}`);

    for (const interestId of createdInterestIds) {
      const hb = await heartbeatInterest(creds, interestId);
      console.log(`[interest] heartbeat id=${interestId} touched=${hb.touched}`);
    }

    const profiles = await fetchProfiles(creds, bot.id, config.broadcasterIds);
    console.log(`[twitch] profiles returned=${profiles.data?.length ?? 0}`);

    const streams = await fetchStreamsStatus(creds, bot.id, config.broadcasterIds);
    console.log(`[twitch] streams status rows=${streams.data?.length ?? 0}`);

    const interested = await fetchInterestedStreamsStatus(creds);
    console.log(`[twitch] interested stream rows=${interested.data?.length ?? 0}`);

    const chatSend = await sendChatMessage(creds, {
      bot_account_id: bot.id,
      broadcaster_user_id: config.broadcasterIds[0],
      message: `${config.chatMessage} @ ${new Date().toISOString()}`,
      auth_mode: config.chatAuthMode,
    });
    console.log(
      `[chat] sent=${chatSend.is_sent} mode=${chatSend.auth_mode_used} badgeEligible=${chatSend.bot_badge_eligible} messageId=${chatSend.message_id}`,
    );
    if (chatSend.drop_reason_code || chatSend.drop_reason_message) {
      console.log(`[chat] dropReason=${chatSend.drop_reason_code ?? ""} ${chatSend.drop_reason_message ?? ""}`);
    }

    console.log(`[listen] waiting ${config.listenSeconds}s for websocket/webhook events...`);
    const startedAt = Date.now();
    while (Date.now() - startedAt < config.listenSeconds * 1000) {
      await new Promise((r) => setTimeout(r, config.heartbeatIntervalSeconds * 1000));
      for (const interestId of createdInterestIds) {
        await heartbeatInterest(creds, interestId);
      }
      console.log("[listen] heartbeat tick");
    }
  } finally {
    if (!config.keepInterests) {
      for (const interestId of createdInterestIds) {
        try {
          await deleteInterest(creds, interestId);
          console.log(`[interest] deleted id=${interestId}`);
        } catch (err) {
          console.log(`[interest] delete failed id=${interestId}: ${err.message}`);
        }
      }
    } else {
      console.log("[interest] KEEP_INTERESTS=true, skipping cleanup");
    }
    if (ws && ws.readyState === ws.OPEN) {
      ws.close(1000, "done");
    }
    if (webhookServer) {
      await new Promise((resolve) => webhookServer.close(resolve));
    }
  }
}

async function main() {
  const cmd = process.argv[2] || "full";
  if (cmd !== "full") {
    throw new Error(`Unknown command: ${cmd}. Use: full`);
  }
  await runFull();
}

main().catch((err) => {
  console.error("[fatal]", err.message);
  process.exitCode = 1;
});
