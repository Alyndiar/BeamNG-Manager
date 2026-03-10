const extApi = typeof browser !== "undefined" ? browser : chrome;
const DEFAULT_PORT = 49441;
const DEFAULT_POLL_SECONDS = 10;
const MIN_POLL_SECONDS = 4;
const MAX_POLL_SECONDS = 60;

function clampPollSeconds(value) {
  let seconds = Number(value || DEFAULT_POLL_SECONDS);
  if (!Number.isFinite(seconds)) {
    seconds = DEFAULT_POLL_SECONDS;
  }
  seconds = Math.round(seconds);
  return Math.max(MIN_POLL_SECONDS, Math.min(MAX_POLL_SECONDS, seconds));
}

function formatLastCommand(payload) {
  if (!payload || typeof payload !== "object") {
    return "Last command: none";
  }
  const id = Number(payload.id || 0);
  const outcome = String(payload.outcome || "unknown");
  const url = String(payload.url || "").trim();
  const atValue = Number(payload.at || 0);
  const stamp = Number.isFinite(atValue) && atValue > 0 ? new Date(atValue).toLocaleTimeString() : "";
  const idText = id > 0 ? `#${id}` : "#?";
  const urlText = url.length > 80 ? `${url.slice(0, 77)}...` : url;
  if (urlText) {
    return `Last command ${idText}: ${outcome} @ ${stamp}\n${urlText}`;
  }
  return `Last command ${idText}: ${outcome} @ ${stamp}`;
}

function renderLastCommand(payload) {
  const el = document.getElementById("lastCommand");
  el.textContent = formatLastCommand(payload);
}

async function loadPort() {
  const stored = await extApi.storage.local.get(["bridgePort", "bridgePollSeconds", "bridgeLastCommand"]);
  const value = Number(stored.bridgePort || DEFAULT_PORT);
  const pollSeconds = clampPollSeconds(stored.bridgePollSeconds || DEFAULT_POLL_SECONDS);
  const input = document.getElementById("bridgePort");
  const pollInput = document.getElementById("bridgePollSeconds");
  input.value = Number.isInteger(value) ? value : DEFAULT_PORT;
  pollInput.value = pollSeconds;
  renderLastCommand(stored.bridgeLastCommand);
}

async function savePort() {
  const input = document.getElementById("bridgePort");
  const pollInput = document.getElementById("bridgePollSeconds");
  let value = Number(input.value || DEFAULT_PORT);
  if (!Number.isInteger(value)) {
    value = DEFAULT_PORT;
  }
  value = Math.max(1024, Math.min(65535, value));
  const pollSeconds = clampPollSeconds(pollInput.value || DEFAULT_POLL_SECONDS);
  input.value = value;
  pollInput.value = pollSeconds;
  await extApi.storage.local.set({ bridgePort: value, bridgePollSeconds: pollSeconds });
  const status = document.getElementById("status");
  status.textContent = `Saved port ${value}; poll ${pollSeconds}s.`;
  setTimeout(() => {
    status.textContent = "";
  }, 1200);
}

async function openOptions() {
  if (extApi.runtime.openOptionsPage) {
    await extApi.runtime.openOptionsPage();
  }
}

async function reconnectBridge() {
  const status = document.getElementById("status");
  status.textContent = "Reconnecting...";
  try {
    const response = await extApi.runtime.sendMessage({ type: "beamng_manager_reconnect" });
    if (response && response.ok && response.online) {
      status.textContent = `Bridge online on port ${response.port}.`;
    } else if (response && response.ok) {
      status.textContent = "Reconnect done, bridge offline.";
    } else {
      status.textContent = "Reconnect failed.";
    }
  } catch (_err) {
    status.textContent = "Reconnect failed.";
  }
  setTimeout(() => {
    status.textContent = "";
  }, 1800);
}

document.getElementById("saveBtn").addEventListener("click", savePort);
document.getElementById("reconnectBtn").addEventListener("click", reconnectBridge);
document.getElementById("optionsBtn").addEventListener("click", openOptions);
if (extApi.storage && extApi.storage.onChanged) {
  extApi.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local" || !changes.bridgeLastCommand) {
      return;
    }
    const next = changes.bridgeLastCommand.newValue;
    renderLastCommand(next);
  });
}
loadPort();
