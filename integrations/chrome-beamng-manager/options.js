const extApi = typeof browser !== "undefined" ? browser : chrome;
const DEFAULT_PORT = 49441;
const DEFAULT_POLL_SECONDS = 10;
const MIN_POLL_SECONDS = 4;
const MAX_POLL_SECONDS = 60;
const CURRENT_EXTENSION_VERSION = (() => {
  try {
    return String(extApi.runtime.getManifest().version || "").trim() || "unknown";
  } catch (_err) {
    return "unknown";
  }
})();

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
  await refreshVersionInfo(value);
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
  await refreshVersionInfo(value);
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
  await refreshVersionInfo();
}

function setVersionState(expected, current) {
  const currentNode = document.getElementById("currentVersion");
  const expectedNode = document.getElementById("expectedVersion");
  const stateNode = document.getElementById("versionState");

  const expectedText = String(expected || "").trim() || "unknown";
  const currentText = String(current || "").trim() || CURRENT_EXTENSION_VERSION;

  currentNode.textContent = currentText;
  expectedNode.textContent = expectedText;
  stateNode.textContent = "";
  stateNode.style.color = "";
  stateNode.style.fontWeight = "";

  if (expectedText !== "unknown" && currentText !== "unknown" && expectedText === currentText) {
    stateNode.textContent = " (match)";
    stateNode.style.color = "#2b7a2b";
    return;
  }
  if (expectedText !== "unknown" && currentText !== "unknown") {
    stateNode.textContent = " (mismatch)";
    stateNode.style.color = "#b31d1d";
    stateNode.style.fontWeight = "600";
    return;
  }
  stateNode.textContent = " (manager offline or unavailable)";
}

async function refreshVersionInfo(portOverride) {
  const currentNode = document.getElementById("currentVersion");
  currentNode.textContent = CURRENT_EXTENSION_VERSION;

  const input = document.getElementById("bridgePort");
  let port = Number(portOverride || input.value || DEFAULT_PORT);
  if (!Number.isInteger(port)) {
    port = DEFAULT_PORT;
  }
  port = Math.max(1024, Math.min(65535, port));

  try {
    const url = new URL(`http://127.0.0.1:${port}/extension/version`);
    if (CURRENT_EXTENSION_VERSION !== "unknown") {
      url.searchParams.set("extension_version", CURRENT_EXTENSION_VERSION);
    }
    const response = await fetch(url.toString(), { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    if (!payload || payload.ok !== true) {
      throw new Error("Invalid payload");
    }
    setVersionState(payload.expected_extension_version, CURRENT_EXTENSION_VERSION);
    return;
  } catch (_err) {
    // Fall through to offline/unavailable state.
  }
  setVersionState("", CURRENT_EXTENSION_VERSION);
}

document.getElementById("saveBtn").addEventListener("click", saveOptions);
document.getElementById("reconnectBtn").addEventListener("click", reconnectBridge);
loadOptions();
