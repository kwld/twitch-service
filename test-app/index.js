import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import dotenv from "dotenv";
import express from "express";
import WebSocket from "ws";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

dotenv.config({ path: path.join(__dirname, ".env") });

const config = {
  port: Number.parseInt(process.env.TEST_APP_PORT ?? "9090", 10),
  serviceBaseUrl: (process.env.SERVICE_BASE_URL ?? "http://localhost:8080").replace(/\/+$/, ""),
  serviceClientId: process.env.SERVICE_CLIENT_ID ?? "",
  serviceClientSecret: process.env.SERVICE_CLIENT_SECRET ?? "",
  webhookPublicUrl: process.env.TEST_WEBHOOK_PUBLIC_URL ?? "",
};

function ensureConfig() {
  if (!config.serviceClientId || !config.serviceClientSecret) {
    throw new Error("SERVICE_CLIENT_ID and SERVICE_CLIENT_SECRET are required in test-app/.env");
  }
}

function serviceHeaders() {
  return {
    "X-Client-Id": config.serviceClientId,
    "X-Client-Secret": config.serviceClientSecret,
    "Content-Type": "application/json",
  };
}

async function serviceFetch(pathname, options = {}) {
  const response = await fetch(`${config.serviceBaseUrl}${pathname}`, {
    method: options.method ?? "GET",
    headers: {
      ...serviceHeaders(),
      ...(options.headers ?? {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }

  if (!response.ok) {
    const err = new Error(`Service returned ${response.status}`);
    err.status = response.status;
    err.data = data;
    throw err;
  }
  return data;
}

function toWsUrl(baseUrl) {
  if (baseUrl.startsWith("https://")) {
    return `wss://${baseUrl.slice("https://".length)}`;
  }
  if (baseUrl.startsWith("http://")) {
    return `ws://${baseUrl.slice("http://".length)}`;
  }
  return baseUrl;
}

const app = express();
app.use(express.json({ limit: "1mb" }));
app.use(express.static(path.join(__dirname, "public")));

const sseClients = new Set();
const recentEvents = [];
const RECENT_EVENT_LIMIT = 200;
let serviceWs = null;
let wsState = "disconnected";
let shouldKeepWsConnected = false;

function pushEvent(kind, payload) {
  const entry = {
    id: `${Date.now()}-${Math.random().toString(16).slice(2, 10)}`,
    kind,
    at: new Date().toISOString(),
    payload,
  };
  recentEvents.unshift(entry);
  if (recentEvents.length > RECENT_EVENT_LIMIT) {
    recentEvents.length = RECENT_EVENT_LIMIT;
  }

  const line = `data: ${JSON.stringify(entry)}\n\n`;
  for (const res of sseClients) {
    res.write(line);
  }
}

function connectServiceWs() {
  if (serviceWs) {
    return;
  }
  wsState = "connecting";
  const wsUrl = new URL(`${toWsUrl(config.serviceBaseUrl)}/ws/events`);
  wsUrl.searchParams.set("client_id", config.serviceClientId);
  wsUrl.searchParams.set("client_secret", config.serviceClientSecret);

  serviceWs = new WebSocket(wsUrl.toString());
  serviceWs.on("open", () => {
    wsState = "connected";
    pushEvent("system", { message: "Connected to service websocket" });
  });
  serviceWs.on("message", (raw) => {
    try {
      const parsed = JSON.parse(String(raw));
      pushEvent("service_ws", parsed);
    } catch {
      pushEvent("service_ws", { raw: String(raw) });
    }
  });
  serviceWs.on("close", (code, reason) => {
    wsState = "disconnected";
    pushEvent("system", { message: "Service websocket closed", code, reason: String(reason) });
    serviceWs = null;
    if (shouldKeepWsConnected) {
      pushEvent("system", { message: "Reconnecting service websocket in 1s" });
      setTimeout(() => {
        if (shouldKeepWsConnected) {
          connectServiceWs();
        }
      }, 1000);
    }
  });
  serviceWs.on("error", (error) => {
    wsState = "error";
    pushEvent("system", { message: "Service websocket error", error: error.message });
  });
}

function disconnectServiceWs() {
  shouldKeepWsConnected = false;
  if (!serviceWs) {
    wsState = "disconnected";
    return;
  }
  serviceWs.close(1000, "client_disconnect");
  serviceWs = null;
  wsState = "disconnected";
}

function sendError(res, error) {
  if (error?.status) {
    res.status(error.status).json({
      error: "service_error",
      detail: error.data ?? error.message,
    });
    return;
  }
  res.status(500).json({ error: "internal_error", detail: error?.message ?? "unknown_error" });
}

app.get("/api/info", (_req, res) => {
  res.json({
    service_base_url: config.serviceBaseUrl,
    webhook_public_url: config.webhookPublicUrl,
    ws_state: wsState,
  });
});

app.get("/api/health", async (_req, res) => {
  try {
    const response = await fetch(`${config.serviceBaseUrl}/health`);
    const data = await response.json();
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.get("/api/bots", async (_req, res) => {
  try {
    const data = await serviceFetch("/v1/bots/accessible");
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.get("/api/catalog", async (_req, res) => {
  try {
    const data = await serviceFetch("/v1/eventsub/subscription-types");
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.get("/api/users/resolve", async (req, res) => {
  try {
    const login = String(req.query.login ?? "").trim().toLowerCase();
    const botAccountId = String(req.query.bot_account_id ?? "").trim();
    if (!login) {
      return res.status(400).json({ error: "validation_error", detail: "login is required" });
    }
    if (!botAccountId) {
      return res.status(400).json({ error: "validation_error", detail: "bot_account_id is required" });
    }
    const search = new URLSearchParams();
    search.set("bot_account_id", botAccountId);
    search.set("logins", login);
    const data = await serviceFetch(`/v1/twitch/profiles?${search.toString()}`);
    const user = (data?.data ?? [])[0];
    if (!user) {
      return res.status(404).json({ error: "not_found", detail: "Twitch user not found" });
    }
    return res.json({
      user_id: String(user.id),
      login: String(user.login),
      display_name: String(user.display_name ?? user.login ?? ""),
      profile_image_url: user.profile_image_url ?? null,
      offline_image_url: user.offline_image_url ?? null,
      description: user.description ?? "",
    });
  } catch (error) {
    sendError(res, error);
  }
});

app.get("/api/users/profile", async (req, res) => {
  try {
    const botAccountId = String(req.query.bot_account_id ?? "").trim();
    const broadcasterUserId = String(req.query.broadcaster_user_id ?? "").trim();
    const login = String(req.query.login ?? "").trim().toLowerCase();
    if (!botAccountId) {
      return res.status(400).json({ error: "validation_error", detail: "bot_account_id is required" });
    }
    if (!broadcasterUserId && !login) {
      return res
        .status(400)
        .json({ error: "validation_error", detail: "broadcaster_user_id or login is required" });
    }
    const search = new URLSearchParams();
    search.set("bot_account_id", botAccountId);
    if (broadcasterUserId) {
      search.set("user_ids", broadcasterUserId);
    } else {
      search.set("logins", login);
    }
    const data = await serviceFetch(`/v1/twitch/profiles?${search.toString()}`);
    const user = (data?.data ?? [])[0];
    if (!user) {
      return res.status(404).json({ error: "not_found", detail: "Twitch user not found" });
    }
    return res.json({
      user_id: String(user.id),
      login: String(user.login ?? ""),
      display_name: String(user.display_name ?? user.login ?? ""),
      profile_image_url: user.profile_image_url ?? null,
      offline_image_url: user.offline_image_url ?? null,
      description: user.description ?? "",
      view_count: user.view_count ?? null,
      created_at: user.created_at ?? null,
    });
  } catch (error) {
    sendError(res, error);
  }
});

app.get("/api/interests", async (_req, res) => {
  try {
    const data = await serviceFetch("/v1/interests");
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.post("/api/interests", async (req, res) => {
  try {
    const data = await serviceFetch("/v1/interests", {
      method: "POST",
      body: req.body,
    });
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.delete("/api/interests/:id", async (req, res) => {
  try {
    const data = await serviceFetch(`/v1/interests/${encodeURIComponent(req.params.id)}`, {
      method: "DELETE",
    });
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.post("/api/interests/:id/heartbeat", async (req, res) => {
  try {
    const data = await serviceFetch(`/v1/interests/${encodeURIComponent(req.params.id)}/heartbeat`, {
      method: "POST",
    });
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.get("/api/broadcaster-authorizations", async (_req, res) => {
  try {
    const data = await serviceFetch("/v1/broadcaster-authorizations");
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.post("/api/broadcaster-authorizations/start", async (req, res) => {
  try {
    const data = await serviceFetch("/v1/broadcaster-authorizations/start", {
      method: "POST",
      body: req.body,
    });
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.post("/api/chat/send", async (req, res) => {
  try {
    const data = await serviceFetch("/v1/twitch/chat/messages", {
      method: "POST",
      body: req.body,
    });
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.post("/api/clips", async (req, res) => {
  try {
    const data = await serviceFetch("/v1/twitch/clips", {
      method: "POST",
      body: req.body,
    });
    res.json(data);
  } catch (error) {
    sendError(res, error);
  }
});

app.post("/api/events/connect", (_req, res) => {
  shouldKeepWsConnected = true;
  connectServiceWs();
  res.json({ ok: true, ws_state: wsState });
});

app.post("/api/events/disconnect", (_req, res) => {
  disconnectServiceWs();
  res.json({ ok: true, ws_state: wsState });
});

app.get("/api/events/status", (_req, res) => {
  res.json({ ws_state: wsState, recent_count: recentEvents.length });
});

app.get("/api/events/recent", (_req, res) => {
  res.json({ data: recentEvents });
});

app.get("/api/events/stream", (req, res) => {
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.flushHeaders();
  sseClients.add(res);
  res.write(`data: ${JSON.stringify({ kind: "system", payload: { message: "stream_connected" } })}\n\n`);

  req.on("close", () => {
    sseClients.delete(res);
  });
});

app.post("/service-webhook", (req, res) => {
  pushEvent("webhook", req.body);
  res.status(204).end();
});

app.get("*", (_req, res) => {
  res.sendFile(path.join(__dirname, "public", "index.html"));
});

try {
  ensureConfig();
  app.listen(config.port, () => {
    console.log(`test-app frontend running at http://localhost:${config.port}`);
    console.log(`service base: ${config.serviceBaseUrl}`);
    if (config.webhookPublicUrl) {
      console.log(`webhook public url: ${config.webhookPublicUrl}`);
    }
  });
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
