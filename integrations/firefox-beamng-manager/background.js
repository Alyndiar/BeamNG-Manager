const extApi = typeof browser !== "undefined" ? browser : chrome;

const DEFAULT_PORT = 49441;
const PORT_SCAN_COUNT = 21;
const DEFAULT_POLL_SECONDS = 10;
const MIN_POLL_SECONDS = 4;
const MAX_POLL_SECONDS = 60;
const TAB_MATCH_PATTERNS = [
  "https://www.beamng.com/resources/*",
  "https://www.beamng.com/forums/*"
];

let currentPort = null;
let currentPayload = null;
let online = false;
let pollTimerId = null;
let pollingInFlight = false;

let sessionId = "";
let markersRev = -1;
let commandsRev = -1;

function clampPort(value) {
  let port = Number(value || DEFAULT_PORT);
  if (!Number.isInteger(port)) {
    port = DEFAULT_PORT;
  }
  return Math.max(1024, Math.min(65535, port));
}

function clampPollSeconds(value) {
  let seconds = Number(value || DEFAULT_POLL_SECONDS);
  if (!Number.isFinite(seconds)) {
    seconds = DEFAULT_POLL_SECONDS;
  }
  seconds = Math.round(seconds);
  return Math.max(MIN_POLL_SECONDS, Math.min(MAX_POLL_SECONDS, seconds));
}

function candidatePorts(basePort) {
  const out = [];
  const start = clampPort(basePort);
  for (let offset = 0; offset < PORT_SCAN_COUNT; offset += 1) {
    const port = start + offset;
    if (port >= 1024 && port <= 65535) {
      out.push(port);
    }
  }
  return out;
}

function resetBridgeSession() {
  sessionId = "";
  markersRev = -1;
  commandsRev = -1;
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  return response.json();
}

async function startSession(port) {
  const payload = await fetchJson(`http://127.0.0.1:${port}/session/start`);
  if (!payload || payload.ok !== true) {
    throw new Error("Invalid session payload");
  }
  return payload;
}

async function fetchChanges(port) {
  const url = new URL(`http://127.0.0.1:${port}/changes`);
  url.searchParams.set("session_id", String(sessionId || ""));
  url.searchParams.set("markers_rev", String(markersRev));
  url.searchParams.set("commands_rev", String(commandsRev));
  const payload = await fetchJson(url.toString());
  if (!payload || payload.ok !== true) {
    throw new Error("Invalid changes payload");
  }
  return payload;
}

async function fetchMarkers(port) {
  const url = new URL(`http://127.0.0.1:${port}/markers`);
  url.searchParams.set("session_id", String(sessionId || ""));
  const payload = await fetchJson(url.toString());
  if (!payload || payload.ok !== true) {
    throw new Error("Invalid markers payload");
  }
  return payload;
}

async function fetchNextCommand(port) {
  const url = new URL(`http://127.0.0.1:${port}/commands/next`);
  url.searchParams.set("session_id", String(sessionId || ""));
  const payload = await fetchJson(url.toString());
  if (!payload || payload.ok !== true) {
    throw new Error("Invalid commands payload");
  }
  return payload;
}

async function notifyTabs(message) {
  const tabs = await extApi.tabs.query({ url: TAB_MATCH_PATTERNS });
  await Promise.all(
    tabs.map((tab) =>
      extApi.tabs.sendMessage(tab.id, message).catch(() => undefined)
    )
  );
}

async function goOffline() {
  if (!online) {
    return;
  }
  online = false;
  currentPayload = null;
  currentPort = null;
  resetBridgeSession();
  await notifyTabs({ type: "beamng_manager_offline" });
}

async function storeLastCommandEvent(command, outcome, detail) {
  let commandId = 0;
  let commandUrl = "";
  if (command && typeof command === "object") {
    const rawId = Number(command.id);
    if (Number.isFinite(rawId) && rawId > 0) {
      commandId = Math.floor(rawId);
    }
    commandUrl = String(command.url || "").trim();
  }
  const payload = {
    id: commandId,
    url: commandUrl,
    outcome: String(outcome || "").trim() || "unknown",
    detail: String(detail || "").trim(),
    at: Date.now()
  };
  try {
    await extApi.storage.local.set({ bridgeLastCommand: payload });
  } catch (_err) {
    // Ignore storage failures.
  }
}

function parseOpenCommand(command) {
  if (!command || typeof command !== "object") {
    return null;
  }
  const id = Number(command.id);
  const url = String(command.url || "").trim();
  if (!url) {
    return null;
  }
  const lower = url.toLowerCase();
  if (!(lower.startsWith("https://") || lower.startsWith("http://"))) {
    return null;
  }
  return { id: Number.isFinite(id) && id > 0 ? id : 0, url };
}

async function openCommandUrl(command) {
  const parsed = parseOpenCommand(command);
  if (!parsed) {
    await storeLastCommandEvent(command, "ignored", "invalid command payload");
    return;
  }
  try {
    await extApi.tabs.create({ url: parsed.url, active: true });
    await storeLastCommandEvent(parsed, "opened_tab", "");
  } catch (_err) {
    try {
      if (extApi.windows && extApi.windows.create) {
        await extApi.windows.create({ url: parsed.url, focused: true });
        await storeLastCommandEvent(parsed, "opened_window", "");
        return;
      }
    } catch (_err2) {
      await storeLastCommandEvent(parsed, "failed", String(_err2));
      return;
    }
    await storeLastCommandEvent(parsed, "failed", String(_err));
  }
}

async function drainCommands(port) {
  for (let i = 0; i < 100; i += 1) {
    const payload = await fetchNextCommand(port);
    if (Number.isFinite(Number(payload.commands_rev))) {
      commandsRev = Number(payload.commands_rev);
    }
    const command = payload.command;
    if (!command) {
      return;
    }
    await openCommandUrl(command);
  }
}

async function pushOnlineMarkers(port, payload) {
  currentPort = Number(port);
  currentPayload = payload;
  online = true;
  await notifyTabs({
    type: "beamng_manager_markers",
    payload
  });
}

async function establishSessionAndRefresh(port) {
  const session = await startSession(port);
  sessionId = String(session.session_id || "").trim();
  markersRev = Number(session.markers_rev || 0);
  commandsRev = Number(session.commands_rev || 0);
  if (!sessionId) {
    throw new Error("Missing session_id");
  }
  const markersPayload = await fetchMarkers(port);
  markersRev = Number(markersPayload.markers_rev || markersRev);
  commandsRev = Number(markersPayload.commands_rev || commandsRev);
  await pushOnlineMarkers(port, markersPayload);
  await drainCommands(port);
}

async function syncOnPort(port) {
  const changedPort = currentPort !== null && Number(currentPort) !== Number(port);
  if (!sessionId || changedPort) {
    resetBridgeSession();
    await establishSessionAndRefresh(port);
    return;
  }

  const changes = await fetchChanges(port);
  const sessionChanged = Boolean(changes.session_changed) || String(changes.session_id || "") !== sessionId;
  if (sessionChanged) {
    resetBridgeSession();
    await establishSessionAndRefresh(port);
    return;
  }

  const nextMarkersRev = Number(changes.markers_rev || markersRev);
  const nextCommandsRev = Number(changes.commands_rev || commandsRev);
  markersRev = nextMarkersRev;

  if (changes.markers_changed || !currentPayload) {
    const markersPayload = await fetchMarkers(port);
    markersRev = Number(markersPayload.markers_rev || markersRev);
    commandsRev = Number(markersPayload.commands_rev || commandsRev);
    await pushOnlineMarkers(port, markersPayload);
  } else if (!online) {
    online = true;
  }

  const commandsPending = Boolean(changes.commands_pending);
  if (changes.commands_changed || commandsPending) {
    await drainCommands(port);
    return;
  }
  commandsRev = nextCommandsRev;
}

async function pollBridge() {
  if (pollingInFlight) {
    return;
  }
  pollingInFlight = true;
  try {
    const stored = await extApi.storage.local.get(["bridgePort", "bridgePollSeconds"]);
    const preferred = clampPort(stored.bridgePort || DEFAULT_PORT);
    const ports = candidatePorts(preferred);

    for (const port of ports) {
      try {
        await syncOnPort(port);
        return;
      } catch (_err) {
        // Continue to next candidate port.
      }
    }
    await goOffline();
  } finally {
    pollingInFlight = false;
  }
}

async function forceReconnect() {
  online = false;
  currentPayload = null;
  currentPort = null;
  resetBridgeSession();
  await notifyTabs({ type: "beamng_manager_offline" });
  await pollBridge();
  return { online, port: currentPort };
}

async function schedulePolling() {
  if (pollTimerId !== null) {
    clearInterval(pollTimerId);
    pollTimerId = null;
  }
  const stored = await extApi.storage.local.get("bridgePollSeconds");
  const pollSeconds = clampPollSeconds(stored.bridgePollSeconds || DEFAULT_POLL_SECONDS);
  pollTimerId = setInterval(pollBridge, pollSeconds * 1000);
}

extApi.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || !message.type) {
    return false;
  }
  if (message.type === "beamng_manager_reconnect") {
    (async () => {
      try {
        const state = await forceReconnect();
        sendResponse({ ok: true, online: Boolean(state.online), port: state.port });
      } catch (_err) {
        sendResponse({ ok: false, online: false });
      }
    })();
    return true;
  }
  if (message.type !== "beamng_manager_request_state") {
    return false;
  }
  (async () => {
    if (!online || !currentPayload) {
      await pollBridge();
    }
    if (online && currentPayload) {
      sendResponse({ type: "beamng_manager_markers", payload: currentPayload, port: currentPort });
      return;
    }
    sendResponse({ type: "beamng_manager_offline" });
  })();
  return true;
});

extApi.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete" || !tab || !tab.id || !tab.url) {
    return;
  }
  if (!TAB_MATCH_PATTERNS.some((pattern) => tab.url.startsWith(pattern.replace("*", "")))) {
    return;
  }
  if (online && currentPayload) {
    extApi.tabs.sendMessage(tab.id, { type: "beamng_manager_markers", payload: currentPayload }).catch(() => undefined);
    return;
  }
  extApi.tabs.sendMessage(tab.id, { type: "beamng_manager_offline" }).catch(() => undefined);
});

if (extApi.storage && extApi.storage.onChanged) {
  extApi.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local") {
      return;
    }
    if (changes.bridgePort || changes.bridgePollSeconds) {
      resetBridgeSession();
      pollBridge();
      schedulePolling();
    }
  });
}

schedulePolling();
pollBridge();
