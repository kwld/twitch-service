(function () {
  const app = document.getElementById("app");
  if (!app) return;

  const statusEndpoint = app.dataset.statusEndpoint || "/status";
  const statusWsPath = app.dataset.statusWs || "/ws/status";

  const el = {
    pill: document.getElementById("connection-pill"),
    generatedAt: document.getElementById("generated-at"),
    summaryCards: document.getElementById("summary-cards"),
    startupMeta: document.getElementById("startup-meta"),
    startupPhases: document.getElementById("startup-phases"),
    servicesMeta: document.getElementById("services-meta"),
    servicesTable: document.getElementById("services-table"),
    eventsubMeta: document.getElementById("eventsub-meta"),
    eventsubGroups: document.getElementById("eventsub-groups"),
    broadcasterMeta: document.getElementById("broadcaster-meta"),
    broadcasterTable: document.getElementById("broadcaster-table"),
    logsMeta: document.getElementById("logs-meta"),
    logsList: document.getElementById("logs-list"),
    eventsMeta: document.getElementById("events-meta"),
    eventsList: document.getElementById("events-list"),
    eventsPauseToggle: document.getElementById("events-pause-toggle"),
    eventsModal: document.getElementById("events-modal"),
    eventsModalBackdrop: document.getElementById("events-modal-backdrop"),
    eventsModalClose: document.getElementById("events-modal-close"),
    eventsModalLabel: document.getElementById("events-modal-label"),
    eventsModalId: document.getElementById("events-modal-id"),
    eventsModalList: document.getElementById("events-modal-list"),
  };

  let reconnectDelay = 1000;
  let reconnectTimer = null;
  let socket = null;
  let currentBroadcasters = [];
  let eventsPaused = false;
  let pausedEvents = [];

  function fmtDate(value) {
    if (!value) return "-";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleString();
  }

  function setPill(kind, label) {
    el.pill.className = `pill pill-${kind}`;
    el.pill.textContent = label;
  }

  function renderSummary(cards) {
    el.summaryCards.innerHTML = (cards || []).map((card) => `
      <article class="summary-card ${card.tone || "neutral"}">
        <div class="label">${card.label}</div>
        <div class="value">${card.value}</div>
      </article>
    `).join("");
  }

  function renderPhases(eventsub) {
    const phases = Array.isArray(eventsub.phase_history) ? eventsub.phase_history : [];
    const progressTotal = Math.max(phases.length, 1);
    const progressDone = phases.length;
    const progressPct = Math.max(8, Math.min(100, Math.round((progressDone / progressTotal) * 100)));
    el.startupMeta.textContent = `state=${eventsub.startup_state || "unknown"} | session_welcome=${eventsub.session_welcome_count || 0} | reconnects=${eventsub.connect_cycle_count || 0}`;
    el.startupPhases.innerHTML = phases.map((phase, idx) => `
      <div class="phase-row">
        <div class="phase-row-top">
          <strong>${idx + 1}. ${phase.label}</strong>
          <span class="badge badge-info">${phase.elapsed_ms}ms</span>
        </div>
        <div class="muted">${fmtDate(phase.completed_at)}</div>
        <div class="bar"><span style="width:${Math.min(100, Math.max(10, phase.elapsed_ms / 8))}%"></span></div>
      </div>
    `).join("") || `<div class="phase-row"><div class="muted">No startup phases recorded yet.</div><div class="bar"><span style="width:${progressPct}%"></span></div></div>`;
  }

  function renderServices(services) {
    const rows = services.rows || [];
    el.servicesMeta.textContent = `${rows.length} services`;
    el.servicesTable.innerHTML = rows.map((row) => `
      <tr>
        <td>
          <strong>${row.name}</strong>
          <div class="muted mono">${row.client_id_masked}</div>
        </td>
        <td>${row.enabled ? '<span class="badge badge-good">enabled</span>' : '<span class="badge badge-bad">disabled</span>'}</td>
        <td>${row.is_connected ? '<span class="badge badge-good">connected</span>' : '<span class="badge badge-warn">idle</span>'}<div class="muted">${row.active_ws_connections}</div></td>
        <td>${row.interests_total}</td>
        <td>${row.working_interests}</td>
        <td>${row.total_events_sent}</td>
        <td>${row.last_activity_human}</td>
      </tr>
    `).join("") || `<tr><td colspan="7" class="muted">No service accounts found.</td></tr>`;
  }

  function renderEventSub(eventsub) {
    const transportRows = eventsub.active_snapshot_by_transport || [];
    const statusRows = eventsub.active_snapshot_by_status || [];
    const sampleRows = eventsub.active_snapshot_sample || [];
    el.eventsubMeta.textContent = `registry=${eventsub.registry_key_count || 0} | active_subs=${eventsub.active_snapshot_total || 0} | ws_listeners=${eventsub.active_service_ws_connections || 0}`;
    el.eventsubGroups.innerHTML = [
      ...transportRows.map((row) => `<div class="compact-item"><div class="compact-top"><strong>Transport ${row.label}</strong><span class="badge badge-info">${row.count}</span></div></div>`),
      ...statusRows.map((row) => `<div class="compact-item"><div class="compact-top"><strong>Status ${row.label}</strong><span class="badge badge-warn">${row.count}</span></div></div>`),
      ...sampleRows.slice(0, 8).map((row) => `
        <div class="compact-item">
          <div class="compact-top">
            <strong>${row.event_type}</strong>
            <span class="badge badge-info">${row.transport}</span>
          </div>
          <div class="muted mono">${row.subscription_id} • ${row.broadcaster_masked} • ${row.session_id_masked || "no-session"}</div>
        </div>
      `)
    ].join("") || `<div class="compact-item"><div class="muted">No EventSub snapshot rows.</div></div>`;
  }

  function renderBroadcasters(rows) {
    currentBroadcasters = Array.isArray(rows) ? rows.slice() : [];
    el.broadcasterMeta.textContent = `${currentBroadcasters.length} masked channels`;
    el.broadcasterTable.innerHTML = currentBroadcasters.map((row, idx) => `
      <tr>
        <td><strong>${row.broadcaster_label}</strong><div class="muted mono">${row.broadcaster_user_id_masked}</div></td>
        <td>${row.is_live ? '<span class="badge badge-good">live</span>' : '<span class="badge badge-info">idle</span>'}</td>
        <td><strong>${row.messages_received || 0}</strong></td>
        <td><strong>${row.messages_sent || 0}</strong></td>
        <td>
          <div class="eventsub-cell">
            <span class="badge badge-info">${row.eventsub_count || 0}</span>
            <button class="ghost-button" type="button" data-broadcaster-index="${idx}">View</button>
          </div>
        </td>
        <td>${row.title_masked}</td>
        <td>${row.game_name}</td>
        <td>${row.last_checked_human}</td>
      </tr>
    `).join("") || `<tr><td colspan="8" class="muted">No broadcaster state rows.</td></tr>`;
  }

  function renderLogs(logs) {
    const rows = logs || [];
    el.logsMeta.textContent = `${rows.length} buffered lines`;
    el.logsList.innerHTML = rows.slice().reverse().map((row) => `
      <div class="log-row">
        <div class="log-top">
          <strong>${row.level}</strong>
          <span class="muted">${fmtDate(row.timestamp)}</span>
        </div>
        <div class="muted mono">${row.logger}</div>
        <div class="message">${row.message}</div>
      </div>
    `).join("") || `<div class="log-row"><div class="muted">No logs buffered yet.</div></div>`;
  }

  function renderEvents(rows) {
    const items = Array.isArray(rows) ? rows : [];
    el.eventsMeta.textContent = `${items.length} buffered events${eventsPaused ? " | paused" : ""}`;
    el.eventsList.innerHTML = items.map((row) => `
      <details class="event-row">
        <summary class="event-summary">
          <div class="event-main">
            <span class="badge ${row.direction === "incoming" ? "badge-good" : "badge-info"}">${row.direction}</span>
            <strong>${row.event_type}</strong>
            <span class="muted">${row.broadcaster_label}</span>
          </div>
          <div class="event-side">
            <span class="muted">${row.service_name}</span>
            <span class="muted">${fmtDate(row.timestamp)}</span>
          </div>
        </summary>
        <div class="event-meta">
          <div class="muted mono">${row.broadcaster_user_id_masked} • ${row.transport} • ${row.target}</div>
          <div class="muted mono">${row.service_account_id_masked}</div>
        </div>
        <pre class="event-body">${row.body_pretty}</pre>
      </details>
    `).join("") || `<div class="log-row"><div class="muted">No traced events yet.</div></div>`;
  }

  function render(snapshot) {
    el.generatedAt.textContent = `snapshot ${fmtDate(snapshot.generated_at)}`;
    renderSummary(snapshot.summary_cards || []);
    renderPhases(snapshot.eventsub || {});
    renderServices(snapshot.services || { rows: [] });
    renderEventSub(snapshot.eventsub || {});
    renderBroadcasters(snapshot.broadcasters || []);
    if (!eventsPaused) {
      pausedEvents = Array.isArray(snapshot.recent_events) ? snapshot.recent_events : [];
      renderEvents(pausedEvents);
    } else if (!el.eventsList.innerHTML) {
      renderEvents(pausedEvents);
    } else {
      el.eventsMeta.textContent = `${pausedEvents.length} buffered events | paused`;
    }
    renderLogs(snapshot.logs || []);
  }

  function closeEventsModal() {
    el.eventsModal.classList.add("hidden");
    el.eventsModal.setAttribute("aria-hidden", "true");
  }

  function openEventsModal(row) {
    const names = Array.isArray(row && row.eventsub_names) ? row.eventsub_names : [];
    el.eventsModalLabel.textContent = row && row.broadcaster_label ? row.broadcaster_label : "chan:unknown";
    el.eventsModalId.textContent = row && row.broadcaster_user_id_masked ? row.broadcaster_user_id_masked : "n/a";
    el.eventsModalList.innerHTML = names.map((name) => `
      <div class="compact-item">
        <div class="compact-top">
          <strong>${name}</strong>
          <span class="badge badge-info">attached</span>
        </div>
      </div>
    `).join("") || `<div class="compact-item"><div class="muted">No attached EventSub names.</div></div>`;
    el.eventsModal.classList.remove("hidden");
    el.eventsModal.setAttribute("aria-hidden", "false");
  }

  async function loadInitial() {
    setPill("wait", "Loading snapshot");
    const res = await fetch(statusEndpoint, { method: "POST" });
    if (!res.ok) throw new Error(`Initial status fetch failed: ${res.status}`);
    const data = await res.json();
    render(data);
    setPill("good", "Live");
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    setPill("warn", `Reconnecting in ${Math.round(reconnectDelay / 1000)}s`);
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connectWs();
    }, reconnectDelay);
    reconnectDelay = Math.min(15000, Math.round(reconnectDelay * 1.7));
  }

  function connectWs() {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${window.location.host}${statusWsPath}`);
    setPill("wait", "Connecting live feed");

    socket.addEventListener("open", () => {
      reconnectDelay = 1000;
      setPill("good", "Live");
    });

    socket.addEventListener("message", (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === "status_snapshot" && payload.payload) {
          render(payload.payload);
        }
      } catch (_err) {
      }
    });

    socket.addEventListener("close", () => {
      socket = null;
      scheduleReconnect();
    });

    socket.addEventListener("error", () => {
      try { socket.close(); } catch (_err) {}
    });
  }

  el.broadcasterTable.addEventListener("click", (event) => {
    const button = event.target.closest("[data-broadcaster-index]");
    if (!button) return;
    const idx = Number(button.dataset.broadcasterIndex);
    if (!Number.isInteger(idx) || idx < 0 || idx >= currentBroadcasters.length) return;
    openEventsModal(currentBroadcasters[idx]);
  });

  el.eventsModalClose.addEventListener("click", closeEventsModal);
  el.eventsModalBackdrop.addEventListener("click", closeEventsModal);
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el.eventsModal.classList.contains("hidden")) {
      closeEventsModal();
    }
  });

  el.eventsPauseToggle.addEventListener("click", () => {
    eventsPaused = !eventsPaused;
    el.eventsPauseToggle.textContent = eventsPaused ? "Resume" : "Pause";
    if (!eventsPaused) {
      renderEvents(pausedEvents);
    } else {
      el.eventsMeta.textContent = `${pausedEvents.length} buffered events | paused`;
    }
  });

  loadInitial().catch((err) => {
    console.error(err);
    setPill("bad", "Snapshot failed");
  }).finally(() => {
    connectWs();
  });
})();
