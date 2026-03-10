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

async function loadOptions() {
  const stored = await extApi.storage.local.get(["bridgePort", "bridgePollSeconds"]);
  const value = Number(stored.bridgePort || DEFAULT_PORT);
  const pollSeconds = clampPollSeconds(stored.bridgePollSeconds || DEFAULT_POLL_SECONDS);
  const input = document.getElementById("bridgePort");
  const pollInput = document.getElementById("bridgePollSeconds");
  input.value = Number.isInteger(value) ? value : DEFAULT_PORT;
  pollInput.value = pollSeconds;
}

async function saveOptions() {
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
  window.setTimeout(() => {
    status.textContent = "";
  }, 1200);
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
  window.setTimeout(() => {
    status.textContent = "";
  }, 1800);
}

document.getElementById("saveBtn").addEventListener("click", saveOptions);
document.getElementById("reconnectBtn").addEventListener("click", reconnectBridge);
loadOptions();
