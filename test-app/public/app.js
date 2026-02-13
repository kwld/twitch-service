const statusLine = document.getElementById("statusLine");
const botSelect = document.getElementById("botSelect");
const broadcasterInput = document.getElementById("broadcasterInput");
const broadcasterLoginInput = document.getElementById("broadcasterLoginInput");
const broadcasterAvatar = document.getElementById("broadcasterAvatar");
const broadcasterName = document.getElementById("broadcasterName");
const broadcasterMeta = document.getElementById("broadcasterMeta");
const broadcasterBio = document.getElementById("broadcasterBio");
const grantUrl = document.getElementById("grantUrl");
const eventTypeSelect = document.getElementById("eventTypeSelect");
const transportSelect = document.getElementById("transportSelect");
const webhookUrlInput = document.getElementById("webhookUrlInput");
const interestList = document.getElementById("interestList");
const eventsLog = document.getElementById("eventsLog");
const authModeSelect = document.getElementById("authModeSelect");
const messageInput = document.getElementById("messageInput");
const sendResult = document.getElementById("sendResult");
const clipTitleInput = document.getElementById("clipTitleInput");
const clipDurationInput = document.getElementById("clipDurationInput");
const clipDelaySelect = document.getElementById("clipDelaySelect");
const clipResult = document.getElementById("clipResult");

let eventSource = null;
let accessibleBots = [];

function clearBroadcasterProfile() {
  broadcasterAvatar.removeAttribute("src");
  broadcasterAvatar.style.display = "none";
  broadcasterName.textContent = "No broadcaster profile loaded";
  broadcasterMeta.textContent = "";
  broadcasterBio.textContent = "";
}

function renderBroadcasterProfile(profile) {
  if (profile.profile_image_url) {
    broadcasterAvatar.src = profile.profile_image_url;
    broadcasterAvatar.style.display = "block";
  } else {
    broadcasterAvatar.removeAttribute("src");
    broadcasterAvatar.style.display = "none";
  }
  broadcasterName.textContent = `${profile.display_name} (@${profile.login})`;
  const created = profile.created_at ? ` created=${new Date(profile.created_at).toISOString().slice(0, 10)}` : "";
  const views = profile.view_count != null ? ` views=${profile.view_count}` : "";
  broadcasterMeta.textContent = `id=${profile.user_id}${views}${created}`;
  broadcasterBio.textContent = profile.description || "";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    method: options.method ?? "GET",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await response.text();
  let data;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!response.ok) {
    throw new Error(typeof data === "string" ? data : JSON.stringify(data));
  }
  return data;
}

function appendLog(line) {
  const at = new Date().toISOString();
  eventsLog.textContent = `${at} ${line}\n${eventsLog.textContent}`.slice(0, 20000);
}

function formatEventPayload(payload) {
  const MAX_LOG_CHARS = 4000;
  if (payload == null) {
    return "null";
  }
  if (typeof payload === "string") {
    return payload.length > MAX_LOG_CHARS
      ? `${payload.slice(0, MAX_LOG_CHARS)} ... [truncated]`
      : payload;
  }
  try {
    const formatted = JSON.stringify(payload, null, 2);
    return formatted.length > MAX_LOG_CHARS
      ? `${formatted.slice(0, MAX_LOG_CHARS)} ... [truncated]`
      : formatted;
  } catch {
    return String(payload);
  }
}

function selectedBotId() {
  return botSelect.value;
}

function requireBroadcaster() {
  const value = broadcasterInput.value.trim();
  if (!value) {
    throw new Error("Broadcaster user id is required.");
  }
  return value;
}

async function refreshStatus() {
  const [info, health, eventStatus] = await Promise.all([
    api("/api/info"),
    api("/api/health"),
    api("/api/events/status"),
  ]);
  statusLine.textContent = `service=${info.service_base_url} health=${health.ok} ws=${eventStatus.ws_state}`;
  if (info.webhook_public_url && !webhookUrlInput.value.trim()) {
    webhookUrlInput.value = info.webhook_public_url;
  }
}

async function refreshBots() {
  const payload = await api("/api/bots");
  accessibleBots = payload.bots || [];
  botSelect.innerHTML = "";
  for (const bot of accessibleBots) {
    const opt = document.createElement("option");
    opt.value = bot.id;
    opt.textContent = `${bot.name} (${bot.twitch_login}/${bot.twitch_user_id})`;
    botSelect.appendChild(opt);
  }
  appendLog(`loaded accessible bots: mode=${payload.access_mode} count=${accessibleBots.length}`);
}

async function refreshInterests() {
  const interests = await api("/api/interests");
  interestList.innerHTML = "";
  for (const item of interests) {
    const li = document.createElement("li");
    li.innerHTML = `<div><strong>${item.event_type}</strong> bot=${item.bot_account_id} broadcaster=${item.broadcaster_user_id} transport=${item.transport}</div>`;

    const actions = document.createElement("div");
    actions.className = "row";

    const hbBtn = document.createElement("button");
    hbBtn.type = "button";
    hbBtn.textContent = "Heartbeat";
    hbBtn.onclick = async () => {
      await api(`/api/interests/${item.id}/heartbeat`, { method: "POST" });
      appendLog(`heartbeat ${item.id}`);
    };
    actions.appendChild(hbBtn);

    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.textContent = "Delete";
    delBtn.onclick = async () => {
      await api(`/api/interests/${item.id}`, { method: "DELETE" });
      appendLog(`deleted interest ${item.id}`);
      await refreshInterests();
    };
    actions.appendChild(delBtn);
    li.appendChild(actions);
    interestList.appendChild(li);
  }
}

async function startGrant() {
  const botId = selectedBotId();
  if (!botId) {
    throw new Error("Select a bot first.");
  }
  const redirectBase = `${window.location.origin}${window.location.pathname}`;
  const payload = await api("/api/broadcaster-authorizations/start", {
    method: "POST",
    body: {
      bot_account_id: botId,
      redirect_url: redirectBase,
    },
  });
  grantUrl.innerHTML = `Authorize streamer here: <a href="${payload.authorize_url}" target="_blank" rel="noopener">${payload.authorize_url}</a>`;
  window.open(payload.authorize_url, "_blank", "noopener");
}

async function resolveBroadcaster() {
  const botId = selectedBotId();
  if (!botId) {
    throw new Error("Select a bot first.");
  }
  const login = broadcasterLoginInput.value.trim().toLowerCase();
  if (!login) {
    throw new Error("Broadcaster username is required.");
  }
  const search = new URLSearchParams();
  search.set("login", login);
  search.set("bot_account_id", botId);
  const payload = await api(`/api/users/resolve?${search.toString()}`);
  broadcasterInput.value = payload.user_id;
  broadcasterLoginInput.value = payload.login;
  renderBroadcasterProfile(payload);
  appendLog(`resolved @${payload.login} -> ${payload.user_id}`);
}

async function fetchBroadcasterProfile() {
  const botId = selectedBotId();
  if (!botId) {
    throw new Error("Select a bot first.");
  }
  const broadcasterUserId = broadcasterInput.value.trim();
  const login = broadcasterLoginInput.value.trim().toLowerCase();
  if (!broadcasterUserId && !login) {
    throw new Error("Provide broadcaster user id or username.");
  }
  const search = new URLSearchParams();
  search.set("bot_account_id", botId);
  if (broadcasterUserId) {
    search.set("broadcaster_user_id", broadcasterUserId);
  } else {
    search.set("login", login);
  }
  const payload = await api(`/api/users/profile?${search.toString()}`);
  if (!broadcasterUserId && payload.user_id) {
    broadcasterInput.value = payload.user_id;
  }
  if (payload.login) {
    broadcasterLoginInput.value = payload.login;
  }
  renderBroadcasterProfile(payload);
  appendLog(`loaded profile @${payload.login} (${payload.user_id})`);
}

async function refreshGrants() {
  const botId = selectedBotId();
  const grants = await api("/api/broadcaster-authorizations");
  const own = grants.filter((x) => x.bot_account_id === botId);
  appendLog(`grants for selected bot: ${own.length}`);
}

async function createInterest() {
  const botId = selectedBotId();
  const broadcaster = requireBroadcaster();
  if (!botId) {
    throw new Error("Select a bot first.");
  }
  const transport = transportSelect.value;
  const payload = {
    bot_account_id: botId,
    event_type: eventTypeSelect.value,
    broadcaster_user_id: broadcaster,
    transport,
  };
  if (transport === "webhook") {
    const url = webhookUrlInput.value.trim();
    if (!url) {
      throw new Error("Webhook URL is required for webhook transport.");
    }
    payload.webhook_url = url;
  }
  const result = await api("/api/interests", { method: "POST", body: payload });
  appendLog(`created/reused interest ${result.id} (${result.event_type})`);
  await refreshInterests();
}

async function sendMessage() {
  const botId = selectedBotId();
  const broadcaster = requireBroadcaster();
  if (!botId) {
    throw new Error("Select a bot first.");
  }
  const payload = await api("/api/chat/send", {
    method: "POST",
    body: {
      bot_account_id: botId,
      broadcaster_user_id: broadcaster,
      message: messageInput.value,
      auth_mode: authModeSelect.value,
    },
  });
  sendResult.textContent = JSON.stringify(payload, null, 2);
  appendLog(`chat send result: sent=${payload.is_sent} mode=${payload.auth_mode_used}`);
}

async function createClip() {
  const botId = selectedBotId();
  const broadcaster = requireBroadcaster();
  if (!botId) {
    throw new Error("Select a bot first.");
  }
  const title = clipTitleInput.value.trim();
  if (!title) {
    throw new Error("Clip title is required.");
  }
  const duration = Number.parseFloat(clipDurationInput.value);
  if (Number.isNaN(duration) || duration < 5 || duration > 60) {
    throw new Error("Clip duration must be between 5 and 60 seconds.");
  }
  const hasDelay = clipDelaySelect.value === "true";
  const payload = await api("/api/clips", {
    method: "POST",
    body: {
      bot_account_id: botId,
      broadcaster_user_id: broadcaster,
      title,
      duration,
      has_delay: hasDelay,
    },
  });
  clipResult.textContent = JSON.stringify(payload, null, 2);
  appendLog(`clip create result: status=${payload.status} clip_id=${payload.clip_id}`);
}

function startSse() {
  if (eventSource) {
    return;
  }
  eventSource = new EventSource("/api/events/stream");
  eventSource.onmessage = (evt) => {
    try {
      const data = JSON.parse(evt.data);
      const eventType = data?.payload?.subscription_type || data?.payload?.type || "unknown";
      const payloadText = formatEventPayload(data?.payload);
      appendLog(`[${data.kind}] type=${eventType} payload=${payloadText}`);
    } catch {
      appendLog(`[sse] ${evt.data}`);
    }
  };
  eventSource.onerror = () => {
    appendLog("event stream error");
  };
}

async function connectEvents() {
  await api("/api/events/connect", { method: "POST" });
  appendLog("requested service websocket connect");
  await refreshStatus();
}

async function disconnectEvents() {
  await api("/api/events/disconnect", { method: "POST" });
  appendLog("requested service websocket disconnect");
  await refreshStatus();
}

function wireButtons() {
  document.getElementById("refreshStatusBtn").onclick = () => withError(refreshStatus);
  document.getElementById("refreshBotsBtn").onclick = () => withError(refreshBots);
  document.getElementById("resolveBroadcasterBtn").onclick = () => withError(resolveBroadcaster);
  document.getElementById("fetchProfileBtn").onclick = () => withError(fetchBroadcasterProfile);
  document.getElementById("startGrantBtn").onclick = () => withError(startGrant);
  document.getElementById("refreshGrantsBtn").onclick = () => withError(refreshGrants);
  document.getElementById("createInterestBtn").onclick = () => withError(createInterest);
  document.getElementById("reloadInterestsBtn").onclick = () => withError(refreshInterests);
  document.getElementById("connectEventsBtn").onclick = () => withError(connectEvents);
  document.getElementById("disconnectEventsBtn").onclick = () => withError(disconnectEvents);
  document.getElementById("clearEventsBtn").onclick = () => {
    eventsLog.textContent = "";
  };
  document.getElementById("sendMessageBtn").onclick = () => withError(sendMessage);
  document.getElementById("createClipBtn").onclick = () => withError(createClip);
  broadcasterInput.oninput = clearBroadcasterProfile;
  broadcasterLoginInput.oninput = clearBroadcasterProfile;
}

async function withError(fn) {
  try {
    await fn();
  } catch (error) {
    appendLog(`ERROR: ${error.message}`);
  }
}

async function main() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("ok") === "true") {
    const broadcasterId = params.get("broadcaster_user_id");
    const broadcasterLogin = params.get("broadcaster_login");
    if (broadcasterId) {
      broadcasterInput.value = broadcasterId;
    }
    if (broadcasterLogin) {
      broadcasterLoginInput.value = broadcasterLogin;
    }
    appendLog(`grant callback success for ${broadcasterLogin ?? "unknown"} (${broadcasterId ?? "n/a"})`);
    history.replaceState({}, "", window.location.pathname);
  } else if (params.get("ok") === "false") {
    appendLog(`grant callback failed: ${params.get("error") ?? "unknown"} ${params.get("message") ?? ""}`);
    history.replaceState({}, "", window.location.pathname);
  }

  wireButtons();
  clearBroadcasterProfile();
  startSse();
  await withError(refreshStatus);
  await withError(refreshBots);
  await withError(refreshInterests);
}

main();
