(function () {
  const app = document.getElementById("app");
  if (!app) return;

  const statusEndpoint = app.dataset.statusEndpoint || "/status";
  const statusWsPath = app.dataset.statusWs || "/ws/status";

  const el = {
    pill: document.getElementById("connection-pill"),
    generatedAt: document.getElementById("generated-at"),
    tabs: document.getElementById("status-tabs"),
    tabPanels: document.querySelectorAll("[data-tab-panel]"),
    summaryCards: document.getElementById("summary-cards"),
    startupMeta: document.getElementById("startup-meta"),
    startupPhases: document.getElementById("startup-phases"),
    botsMeta: document.getElementById("bots-meta"),
    botsTable: document.getElementById("bots-table"),
    servicesMeta: document.getElementById("services-meta"),
    servicesTable: document.getElementById("services-table"),
    eventsubMeta: document.getElementById("eventsub-meta"),
    eventsubCounters: document.getElementById("eventsub-counters"),
    eventsubFilterEvent: document.getElementById("eventsub-filter-text"),
    eventsubFilterBroadcaster: document.getElementById("eventsub-filter-broadcaster"),
    eventsubFilterCost: document.getElementById("eventsub-filter-cost"),
    eventsubFilterService: document.getElementById("eventsub-filter-service"),
    eventsubFilterBot: document.getElementById("eventsub-filter-bot"),
    eventsubFilterTransport: document.getElementById("eventsub-filter-transport"),
    eventsubFilterStatus: document.getElementById("eventsub-filter-status"),
    eventsubFilterSession: document.getElementById("eventsub-filter-session"),
    eventsubPageSize: document.getElementById("eventsub-page-size"),
    eventsubPagination: document.getElementById("eventsub-pagination"),
    eventsubTable: document.getElementById("eventsub-table"),
    broadcasterMeta: document.getElementById("broadcaster-meta"),
    broadcasterTable: document.getElementById("broadcaster-table"),
    broadcasterFilterBot: document.getElementById("broadcaster-filter-bot"),
    logsMeta: document.getElementById("logs-meta"),
    logsList: document.getElementById("logs-list"),
    logsFilterBot: document.getElementById("logs-filter-bot"),
    eventsMeta: document.getElementById("events-meta"),
    eventsPagination: document.getElementById("events-pagination"),
    eventsList: document.getElementById("events-list"),
    eventsPauseToggle: document.getElementById("events-pause-toggle"),
    eventsFilterText: document.getElementById("events-filter-text"),
    eventsFilterDirection: document.getElementById("events-filter-direction"),
    eventsFilterOrigin: document.getElementById("events-filter-origin"),
    eventsFilterService: document.getElementById("events-filter-service"),
    eventsFilterBot: document.getElementById("events-filter-bot"),
    eventsPageSize: document.getElementById("events-page-size"),
    eventsModal: document.getElementById("events-modal"),
    eventsModalBackdrop: document.getElementById("events-modal-backdrop"),
    eventsModalClose: document.getElementById("events-modal-close"),
    eventsModalLabel: document.getElementById("events-modal-label"),
    eventsModalId: document.getElementById("events-modal-id"),
    eventsModalList: document.getElementById("events-modal-list"),
    deliveriesMeta: document.getElementById("deliveries-meta"),
    deliveriesFilterText: document.getElementById("deliveries-filter-text"),
    deliveriesFilterTransport: document.getElementById("deliveries-filter-transport"),
    deliveriesFilterOutcome: document.getElementById("deliveries-filter-outcome"),
    deliveriesFilterService: document.getElementById("deliveries-filter-service"),
    deliveriesPageSize: document.getElementById("deliveries-page-size"),
    deliveriesPagination: document.getElementById("deliveries-pagination"),
    deliveriesList: document.getElementById("deliveries-list"),
  };

  let reconnectDelay = 1000;
  let reconnectTimer = null;
  let socket = null;
  let currentBroadcasters = [];
  let allBroadcasters = [];
  let eventsPaused = false;
  let allEvents = [];
  let allLogs = [];
  let allBots = [];
  let allEventSubRows = [];
  let allDeliveries = [];
  let activeTab = "overview";
  let eventsPage = 1;
  let eventsubPage = 1;
  let deliveriesPage = 1;
  let pendingSnapshot = null;
  let selectionResumeTimer = null;
  let openEventKeys = new Set();
  let openDeliveryKeys = new Set();

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

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#39;",
    }[ch]));
  }

  function setActiveTab(name) {
    activeTab = name;
    Array.from(el.tabs.querySelectorAll("[data-tab-target]")).forEach((button) => {
      button.classList.toggle("is-active", button.dataset.tabTarget === name);
    });
    Array.from(el.tabPanels).forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.tabPanel === name);
    });
  }

  function eventRowKey(row) {
    return [
      row.timestamp,
      row.service_name,
      row.direction,
      row.event_type,
      row.target,
      row.broadcaster_user_id_masked,
    ].join("|");
  }

  function deliveryRowKey(row) {
    return [
      row.timestamp,
      row.service_name,
      row.event_type,
      row.transport,
      row.target,
      row.outcome,
      row.broadcaster_user_id_masked,
    ].join("|");
  }

  function eventOrigin(row) {
    const transport = String(row.transport || "").toLowerCase();
    const target = String(row.target || "").toLowerCase();
    if (transport.startsWith("twitch_") || target === "twitch:eventsub") {
      return "twitch";
    }
    if (transport === "webhook") {
      return "webhook";
    }
    if (transport === "websocket" || target === "/ws/events") {
      return "websocket";
    }
    return "service";
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

  function renderBots(rows) {
    const items = Array.isArray(rows) ? rows : [];
    allBots = items.slice();
    el.botsMeta.textContent = `${items.length} bot accounts`;
    el.botsTable.innerHTML = items.map((row) => `
      <tr>
        <td>
          <strong>${row.bot_name_masked || row.bot_name}</strong>
          <div class="muted mono">${row.twitch_login_masked} • ${row.bot_account_id}</div>
        </td>
        <td>${row.enabled ? '<span class="badge badge-good">enabled</span>' : '<span class="badge badge-bad">disabled</span>'}</td>
        <td>${row.channel_count || 0}</td>
        <td>${row.subscription_count || 0}</td>
        <td>${row.enabled_subscription_count || 0}</td>
        <td><span class="badge badge-warn">${row.eventsub_cost_total || 0}/${row.eventsub_cost_max || 0}</span></td>
      </tr>
    `).join("") || `<tr><td colspan="6" class="muted">No bot accounts found.</td></tr>`;
    const broadcasterSelected = el.broadcasterFilterBot.value;
    const eventSelected = el.eventsFilterBot.value;
    const logSelected = el.logsFilterBot.value;
    const options = ['<option value="">All bot accounts</option>', ...items.map((row) => `<option value="${escapeHtml(row.bot_account_id)}">${escapeHtml(row.bot_name_masked || row.bot_name)}</option>`)];
    el.broadcasterFilterBot.innerHTML = options.join("");
    el.eventsFilterBot.innerHTML = options.join("");
    el.logsFilterBot.innerHTML = options.join("");
    if (items.some((row) => row.bot_account_id === broadcasterSelected)) el.broadcasterFilterBot.value = broadcasterSelected;
    if (items.some((row) => row.bot_account_id === eventSelected)) el.eventsFilterBot.value = eventSelected;
    if (items.some((row) => row.bot_account_id === logSelected)) el.logsFilterBot.value = logSelected;
    const eventsubSelected = el.eventsubFilterBot.value;
    el.eventsubFilterBot.innerHTML = options.join("");
    if (items.some((row) => row.bot_account_id === eventsubSelected)) el.eventsubFilterBot.value = eventsubSelected;
  }

  function renderEventSub(eventsub) {
    const transportRows = eventsub.active_snapshot_by_transport || [];
    const statusRows = eventsub.active_snapshot_by_status || [];
    const rows = Array.isArray(eventsub.active_snapshot_rows) ? eventsub.active_snapshot_rows : [];
    allEventSubRows = rows.slice();
    el.eventsubMeta.textContent = `registry=${eventsub.registry_key_count || 0} | active_subs=${eventsub.active_snapshot_total || 0} | total_cost=${eventsub.active_snapshot_cost_total || 0}/${eventsub.active_snapshot_max_total_cost || 0} | source=${eventsub.active_snapshot_source || "unknown"} | ws_listeners=${eventsub.active_service_ws_connections || 0}`;
    el.eventsubCounters.innerHTML = [
      `<article class="summary-card neutral"><div class="label">Active Subs</div><div class="value">${eventsub.active_snapshot_total || 0}</div></article>`,
      `<article class="summary-card warn"><div class="label">Cost</div><div class="value">${eventsub.active_snapshot_cost_total || 0}/${eventsub.active_snapshot_max_total_cost || 0}</div></article>`,
      ...transportRows.map((row) => `<article class="summary-card neutral"><div class="label">Transport ${row.label}</div><div class="value">${row.count}</div></article>`),
      ...statusRows.map((row) => `<article class="summary-card neutral"><div class="label">Status ${row.label}</div><div class="value">${row.count}</div></article>`),
    ].join("");
    renderEventSubServiceFilter(rows);
    refreshEventSub();
  }

  function renderBroadcasters(rows) {
    allBroadcasters = Array.isArray(rows) ? rows.slice() : [];
    const selectedBot = el.broadcasterFilterBot.value || "";
    currentBroadcasters = allBroadcasters.filter((row) => !selectedBot || row.bot_account_id === selectedBot);
    el.broadcasterMeta.textContent = `${currentBroadcasters.length} masked channels`;
    el.broadcasterTable.innerHTML = currentBroadcasters.map((row, idx) => `
      <tr>
        <td><strong>${row.broadcaster_label}</strong><div class="muted mono">${row.broadcaster_user_id_masked}</div></td>
        <td><strong>${row.bot_name_masked}</strong><div class="muted mono">${row.bot_account_id_masked}</div></td>
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
    `).join("") || `<tr><td colspan="9" class="muted">No broadcaster state rows.</td></tr>`;
  }

  function renderLogs(logs) {
    allLogs = Array.isArray(logs) ? logs.slice() : [];
    const selectedBot = el.logsFilterBot.value || "";
    const rows = allLogs.filter((row) => !selectedBot || row.bot_account_id === selectedBot);
    el.logsMeta.textContent = `${rows.length} buffered lines`;
    el.logsList.innerHTML = rows.slice().reverse().map((row) => `
      <div class="log-row log-${String(row.level || "INFO").toLowerCase()}">
        <div class="log-top">
          <strong>${row.level}</strong>
          <span class="muted">${fmtDate(row.timestamp)}</span>
        </div>
        <div class="muted mono">${row.logger} • ${row.bot_name_masked || "unknown"} • ${row.bot_account_id_masked || "n/a"}</div>
        <div class="message">${row.message}</div>
      </div>
    `).join("") || `<div class="log-row"><div class="muted">No logs buffered yet.</div></div>`;
  }

  function renderEventServiceFilter(rows) {
    const selected = el.eventsFilterService.value;
    const names = Array.from(new Set((rows || []).map((row) => row.service_name).filter(Boolean))).sort();
    el.eventsFilterService.innerHTML = ['<option value="">All services</option>', ...names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)].join("");
    if (names.includes(selected)) {
      el.eventsFilterService.value = selected;
    }
  }

  function renderEventSubServiceFilter(rows) {
    const selected = el.eventsubFilterService.value;
    const names = Array.from(new Set((rows || []).flatMap((row) => Array.isArray(row.service_names) ? row.service_names : []).filter(Boolean))).sort();
    el.eventsubFilterService.innerHTML = ['<option value="">All services</option>', ...names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)].join("");
    if (names.includes(selected)) {
      el.eventsubFilterService.value = selected;
    }
  }

  function renderEventSubSelectFilter(element, values, selected, allLabel) {
    element.innerHTML = [`<option value="">${allLabel}</option>`, ...values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)].join("");
    if (values.includes(selected)) {
      element.value = selected;
    }
  }

  function getFilteredEventSubRows() {
    const eventText = (el.eventsubFilterEvent.value || "").trim().toLowerCase();
    const broadcasterText = (el.eventsubFilterBroadcaster.value || "").trim().toLowerCase();
    const costText = (el.eventsubFilterCost.value || "").trim().toLowerCase();
    const service = el.eventsubFilterService.value || "";
    const bot = el.eventsubFilterBot.value || "";
    const transport = el.eventsubFilterTransport.value || "";
    const status = el.eventsubFilterStatus.value || "";
    const session = el.eventsubFilterSession.value || "";
    return allEventSubRows.filter((row) => {
      if (service && !(Array.isArray(row.service_names) && row.service_names.includes(service))) return false;
      if (bot && row.bot_account_id !== bot) return false;
      if (transport && row.transport !== transport) return false;
      if (status && row.status !== status) return false;
      if (session === "has-session" && (!row.session_id_masked || row.session_id_masked === "no-session")) return false;
      if (session === "no-session" && row.session_id_masked && row.session_id_masked !== "no-session") return false;
      if (eventText && !String(row.event_type || "").toLowerCase().includes(eventText)) return false;
      if (broadcasterText) {
        const haystack = [row.broadcaster_masked, row.broadcaster_user_id_masked].join(" ").toLowerCase();
        if (!haystack.includes(broadcasterText)) return false;
      }
      if (costText && !String(row.cost || "").toLowerCase().includes(costText)) return false;
      return true;
    });
  }

  function renderGenericPagination(target, totalItems, totalPages, currentPage, prefix) {
    if (totalItems <= 0) {
      target.innerHTML = `<span class="muted">No matching rows</span>`;
      return;
    }
    target.innerHTML = `
      <div class="muted">Showing page ${currentPage} / ${totalPages} • ${totalItems} matched</div>
      <div class="pagination-actions">
        <button class="ghost-button" type="button" data-${prefix}-nav="prev" ${currentPage <= 1 ? "disabled" : ""}>Prev</button>
        <button class="ghost-button" type="button" data-${prefix}-nav="next" ${currentPage >= totalPages ? "disabled" : ""}>Next</button>
      </div>
    `;
  }

  function renderEventSubRows(rows) {
    const items = Array.isArray(rows) ? rows : [];
    const pageSize = Math.max(1, Number(el.eventsubPageSize.value || 25));
    const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
    eventsubPage = Math.min(totalPages, Math.max(1, eventsubPage));
    const start = (eventsubPage - 1) * pageSize;
    const pageRows = items.slice(start, start + pageSize);
    renderGenericPagination(el.eventsubPagination, items.length, totalPages, eventsubPage, "eventsub-page");
    el.eventsubTable.innerHTML = pageRows.map((row) => `
      <tr>
        <td><strong>${row.event_type}</strong><div class="muted mono">${row.subscription_id}</div></td>
        <td><strong>${row.broadcaster_masked}</strong><div class="muted mono">${row.broadcaster_user_id_masked}</div></td>
        <td><strong>${row.bot_name_masked || "unknown"}</strong><div class="muted mono">${row.bot_account_id_masked || "n/a"}</div></td>
        <td><strong>${row.service_count || 0}</strong><div class="muted">${escapeHtml(row.service_names_display || "none")}</div></td>
        <td><span class="badge badge-info">${row.transport || "unknown"}</span></td>
        <td><span class="badge ${String(row.status || "").startsWith("enabled") ? "badge-good" : "badge-warn"}">${row.status || "unknown"}</span></td>
        <td><span class="badge badge-warn">${row.cost || 0}</span></td>
        <td><span class="muted mono">${row.session_id_masked || "no-session"}</span></td>
      </tr>
    `).join("") || `<tr><td colspan="8" class="muted">No EventSub snapshot rows.</td></tr>`;
  }

  function refreshEventSub() {
    renderEventSubSelectFilter(
      el.eventsubFilterTransport,
      Array.from(new Set(allEventSubRows.map((row) => row.transport).filter(Boolean))).sort(),
      el.eventsubFilterTransport.value,
      "All transports",
    );
    renderEventSubSelectFilter(
      el.eventsubFilterStatus,
      Array.from(new Set(allEventSubRows.map((row) => row.status).filter(Boolean))).sort(),
      el.eventsubFilterStatus.value,
      "All statuses",
    );
    renderEventSubRows(getFilteredEventSubRows());
  }

  function renderDeliveryServiceFilter(rows) {
    const selected = el.deliveriesFilterService.value;
    const names = Array.from(new Set((rows || []).map((row) => row.service_name).filter(Boolean))).sort();
    el.deliveriesFilterService.innerHTML = ['<option value="">All services</option>', ...names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`)].join("");
    if (names.includes(selected)) {
      el.deliveriesFilterService.value = selected;
    }
  }

  function getFilteredDeliveries() {
    const text = (el.deliveriesFilterText.value || "").trim().toLowerCase();
    const transport = el.deliveriesFilterTransport.value || "";
    const outcome = el.deliveriesFilterOutcome.value || "";
    const service = el.deliveriesFilterService.value || "";
    return allDeliveries.filter((row) => {
      if (transport && row.transport !== transport) return false;
      if (outcome && row.outcome !== outcome) return false;
      if (service && row.service_name !== service) return false;
      if (!text) return true;
      const haystack = [
        row.event_type,
        row.service_name,
        row.broadcaster_label,
        row.target,
        row.error,
        row.transport,
        row.outcome,
      ].join(" ").toLowerCase();
      return haystack.includes(text);
    });
  }

  function renderDeliveries(rows) {
    const currentOpen = new Set(openDeliveryKeys);
    Array.from(el.deliveriesList.querySelectorAll("details[data-delivery-key][open]")).forEach((node) => {
      currentOpen.add(node.dataset.deliveryKey);
    });
    openDeliveryKeys = currentOpen;

    const items = Array.isArray(rows) ? rows : [];
    const pageSize = Math.max(1, Number(el.deliveriesPageSize.value || 25));
    const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
    deliveriesPage = Math.min(totalPages, Math.max(1, deliveriesPage));
    const start = (deliveriesPage - 1) * pageSize;
    const pageRows = items.slice(start, start + pageSize);
    el.deliveriesMeta.textContent = `${items.length} delivery attempts`;
    renderGenericPagination(el.deliveriesPagination, items.length, totalPages, deliveriesPage, "deliveries-page");
    el.deliveriesList.innerHTML = pageRows.map((row) => `
      <details class="event-row" data-delivery-key="${escapeHtml(deliveryRowKey(row))}" ${openDeliveryKeys.has(deliveryRowKey(row)) ? "open" : ""}>
        <summary class="event-summary">
          <div class="event-main">
            <span class="badge badge-${row.outcome === "delivered" ? "good" : row.outcome === "failed" ? "bad" : "warn"}">${row.outcome}</span>
            <strong>${row.event_type}</strong>
            <span class="muted">${row.broadcaster_label}</span>
            <span class="badge badge-info">${row.transport}</span>
            <span class="badge badge-warn">${row.duration_ms}ms</span>
          </div>
          <div class="event-side">
            <span class="muted">${row.service_name}</span>
            <span class="muted">${fmtDate(row.timestamp)}</span>
          </div>
        </summary>
        <div class="event-meta">
          <div class="muted mono">${row.target} • listeners=${row.listener_count} • sent=${row.delivered_count} • failed=${row.failed_count} • status=${row.status_code || "-"}</div>
          <div class="muted mono">${row.broadcaster_user_id_masked} • ${row.bot_name_masked || "unknown"} • ${row.bot_account_id_masked || "n/a"}</div>
        </div>
        ${row.error ? `<div class="message delivery-error">${escapeHtml(row.error)}</div>` : ""}
        <pre class="event-body">${row.body_pretty}</pre>
      </details>
    `).join("") || `<div class="log-row"><div class="muted">No delivery attempts recorded yet.</div></div>`;
  }

  function refreshDeliveries() {
    renderDeliveryServiceFilter(allDeliveries);
    renderDeliveries(getFilteredDeliveries());
  }

  function getFilteredEvents() {
    const text = (el.eventsFilterText.value || "").trim().toLowerCase();
    const direction = el.eventsFilterDirection.value || "";
    const origin = el.eventsFilterOrigin.value || "";
    const service = el.eventsFilterService.value || "";
    const bot = el.eventsFilterBot.value || "";
    return allEvents.filter((row) => {
      if (direction && row.direction !== direction) return false;
      if (service && row.service_name !== service) return false;
       if (bot && row.bot_account_id !== bot) return false;
      if (origin) {
        const rowOrigin = eventOrigin(row);
        if (origin === "service" && (rowOrigin === "twitch")) return false;
        if (origin !== "service" && rowOrigin !== origin) return false;
      }
      if (!text) return true;
      const haystack = [
        row.event_type,
        row.service_name,
        row.broadcaster_label,
        row.bot_name,
        row.target,
        row.transport,
        eventOrigin(row),
      ].join(" ").toLowerCase();
      return haystack.includes(text);
    });
  }

  function renderEventPagination(totalItems, totalPages, currentPage) {
    if (totalItems <= 0) {
      el.eventsPagination.innerHTML = `<span class="muted">No matching events</span>`;
      return;
    }
    el.eventsPagination.innerHTML = `
      <div class="muted">Showing page ${currentPage} / ${totalPages} • ${totalItems} matched</div>
      <div class="pagination-actions">
        <button class="ghost-button" type="button" data-page-nav="prev" ${currentPage <= 1 ? "disabled" : ""}>Prev</button>
        <button class="ghost-button" type="button" data-page-nav="next" ${currentPage >= totalPages ? "disabled" : ""}>Next</button>
      </div>
    `;
  }

  function renderEvents(rows) {
    const currentOpen = new Set(openEventKeys);
    Array.from(el.eventsList.querySelectorAll("details[data-event-key][open]")).forEach((node) => {
      currentOpen.add(node.dataset.eventKey);
    });
    openEventKeys = currentOpen;

    const items = Array.isArray(rows) ? rows : [];
    const pageSize = Math.max(1, Number(el.eventsPageSize.value || 25));
    const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
    eventsPage = Math.min(totalPages, Math.max(1, eventsPage));
    const start = (eventsPage - 1) * pageSize;
    const pageRows = items.slice(start, start + pageSize);
    el.eventsMeta.textContent = `${items.length} matched events${eventsPaused ? " | paused" : ""}`;
    renderEventPagination(items.length, totalPages, eventsPage);
    el.eventsList.innerHTML = pageRows.map((row) => `
      <details class="event-row" data-event-key="${escapeHtml(eventRowKey(row))}" ${openEventKeys.has(eventRowKey(row)) ? "open" : ""}>
        <summary class="event-summary">
          <div class="event-main">
            <span class="badge ${row.direction === "incoming" ? "badge-good" : "badge-info"}">${row.direction}</span>
            <strong>${row.event_type}</strong>
            <span class="muted">${row.broadcaster_label}</span>
            <span class="badge badge-warn">${eventOrigin(row)}</span>
            <span class="badge badge-info">${row.bot_name_masked || row.bot_name || "unknown"}</span>
          </div>
          <div class="event-side">
            <span class="muted">${row.service_name}</span>
            <span class="muted">${fmtDate(row.timestamp)}</span>
          </div>
        </summary>
        <div class="event-meta">
          <div class="muted mono">${row.broadcaster_user_id_masked} • ${row.transport} • ${row.target}</div>
          <div class="muted mono">${row.service_account_id_masked} • ${row.bot_account_id_masked || "n/a"}</div>
        </div>
        <pre class="event-body">${row.body_pretty}</pre>
      </details>
    `).join("") || `<div class="log-row"><div class="muted">No traced events yet.</div></div>`;
  }

  function refreshEvents() {
    renderEventServiceFilter(allEvents);
    renderEvents(getFilteredEvents());
  }

  function hasActiveSelectionLock() {
    const selection = window.getSelection ? window.getSelection() : null;
    if (!selection) return false;
    return !selection.isCollapsed && String(selection).trim().length > 0;
  }

  function scheduleSelectionResume() {
    if (selectionResumeTimer) return;
    selectionResumeTimer = window.setInterval(() => {
      if (hasActiveSelectionLock()) return;
      window.clearInterval(selectionResumeTimer);
      selectionResumeTimer = null;
      if (pendingSnapshot) {
        const snapshot = pendingSnapshot;
        pendingSnapshot = null;
        render(snapshot);
      }
    }, 600);
  }

  function render(snapshot) {
    if (hasActiveSelectionLock()) {
      pendingSnapshot = snapshot;
      scheduleSelectionResume();
      return;
    }
    el.generatedAt.textContent = `snapshot ${fmtDate(snapshot.generated_at)}`;
    renderSummary(snapshot.summary_cards || []);
    renderPhases(snapshot.eventsub || {});
    renderBots(snapshot.bots || []);
    renderServices(snapshot.services || { rows: [] });
    renderEventSub(snapshot.eventsub || {});
    renderBroadcasters(snapshot.broadcasters || []);
    if (!eventsPaused) {
      allEvents = Array.isArray(snapshot.recent_events) ? snapshot.recent_events : [];
      refreshEvents();
    } else if (!el.eventsList.innerHTML) {
      refreshEvents();
    } else {
      el.eventsMeta.textContent = `${allEvents.length} buffered events | paused`;
    }
    allDeliveries = Array.isArray(snapshot.recent_deliveries) ? snapshot.recent_deliveries : [];
    refreshDeliveries();
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

  function handleEventFilterChange(resetPage) {
    if (resetPage) eventsPage = 1;
    refreshEvents();
  }

  el.tabs.addEventListener("click", (event) => {
    const button = event.target.closest("[data-tab-target]");
    if (!button) return;
    setActiveTab(button.dataset.tabTarget);
  });

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
      refreshEvents();
    } else {
      el.eventsMeta.textContent = `${allEvents.length} buffered events | paused`;
    }
  });

  el.eventsFilterText.addEventListener("input", () => handleEventFilterChange(true));
  el.eventsFilterDirection.addEventListener("change", () => handleEventFilterChange(true));
  el.eventsFilterOrigin.addEventListener("change", () => handleEventFilterChange(true));
  el.eventsFilterService.addEventListener("change", () => handleEventFilterChange(true));
  el.eventsFilterBot.addEventListener("change", () => handleEventFilterChange(true));
  el.eventsPageSize.addEventListener("change", () => handleEventFilterChange(true));
  el.eventsubFilterEvent.addEventListener("input", () => { eventsubPage = 1; refreshEventSub(); });
  el.eventsubFilterBroadcaster.addEventListener("input", () => { eventsubPage = 1; refreshEventSub(); });
  el.eventsubFilterCost.addEventListener("input", () => { eventsubPage = 1; refreshEventSub(); });
  el.eventsubFilterService.addEventListener("change", () => { eventsubPage = 1; refreshEventSub(); });
  el.eventsubFilterBot.addEventListener("change", () => { eventsubPage = 1; refreshEventSub(); });
  el.eventsubFilterTransport.addEventListener("change", () => { eventsubPage = 1; refreshEventSub(); });
  el.eventsubFilterStatus.addEventListener("change", () => { eventsubPage = 1; refreshEventSub(); });
  el.eventsubFilterSession.addEventListener("change", () => { eventsubPage = 1; refreshEventSub(); });
  el.eventsubPageSize.addEventListener("change", () => { eventsubPage = 1; refreshEventSub(); });
  el.deliveriesFilterText.addEventListener("input", () => { deliveriesPage = 1; refreshDeliveries(); });
  el.deliveriesFilterTransport.addEventListener("change", () => { deliveriesPage = 1; refreshDeliveries(); });
  el.deliveriesFilterOutcome.addEventListener("change", () => { deliveriesPage = 1; refreshDeliveries(); });
  el.deliveriesFilterService.addEventListener("change", () => { deliveriesPage = 1; refreshDeliveries(); });
  el.deliveriesPageSize.addEventListener("change", () => { deliveriesPage = 1; refreshDeliveries(); });
  el.broadcasterFilterBot.addEventListener("change", () => renderBroadcasters(allBroadcasters));
  el.logsFilterBot.addEventListener("change", () => renderLogs(allLogs));
  el.eventsPagination.addEventListener("click", (event) => {
    const button = event.target.closest("[data-page-nav]");
    if (!button) return;
    if (button.dataset.pageNav === "prev") eventsPage -= 1;
    if (button.dataset.pageNav === "next") eventsPage += 1;
    refreshEvents();
  });
  el.eventsubPagination.addEventListener("click", (event) => {
    const button = event.target.closest("[data-eventsub-page-nav]");
    if (!button) return;
    if (button.dataset.eventsubPageNav === "prev") eventsubPage -= 1;
    if (button.dataset.eventsubPageNav === "next") eventsubPage += 1;
    refreshEventSub();
  });
  el.deliveriesPagination.addEventListener("click", (event) => {
    const button = event.target.closest("[data-deliveries-page-nav]");
    if (!button) return;
    if (button.dataset.deliveriesPageNav === "prev") deliveriesPage -= 1;
    if (button.dataset.deliveriesPageNav === "next") deliveriesPage += 1;
    refreshDeliveries();
  });
  el.eventsList.addEventListener("toggle", (event) => {
    const details = event.target.closest("details[data-event-key]");
    if (!details) return;
    const key = details.dataset.eventKey;
    if (!key) return;
    if (details.open) openEventKeys.add(key);
    else openEventKeys.delete(key);
  }, true);
  el.deliveriesList.addEventListener("toggle", (event) => {
    const details = event.target.closest("details[data-delivery-key]");
    if (!details) return;
    const key = details.dataset.deliveryKey;
    if (!key) return;
    if (details.open) openDeliveryKeys.add(key);
    else openDeliveryKeys.delete(key);
  }, true);

  loadInitial().catch((err) => {
    console.error(err);
    setPill("bad", "Snapshot failed");
  }).finally(() => {
    connectWs();
  });
})();
