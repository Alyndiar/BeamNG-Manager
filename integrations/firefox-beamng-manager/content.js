(() => {
  const extApi = typeof browser !== "undefined" ? browser : chrome;

  const STYLE_ID = "beamng-manager-installed-style";
  const MARKER_ATTR = "data-beamng-manager-install-kind";
  const CARD_ATTR = "data-beamng-manager-install-kind-card";
  const PAGE_BADGE_ID = "beamng-manager-installed-page-badge";
  const LIST_CARD_SELECTOR = ".resourceListItem, .structItem--resource, .structItem, .resourceRow";
  const TITLE_TAG_PATTERN = /\s*\|\s*(Installed on this PC|Subscribed|Manually Installed)\s*$/i;

  let subscribedTokens = new Set();
  let manualTokens = new Set();
  let subscribedTagIds = new Set();
  let manualTagIds = new Set();
  let active = false;
  let observer = null;
  let resourceBadgeRetryTimer = null;

  const statusLabel = (kind) =>
    kind === "subscribed" ? "Subscribed" : kind === "manual" ? "Manually Installed" : "";

  function isResourceDetailPage() {
    const parts = (window.location.pathname || "")
      .split("/")
      .filter(Boolean)
      .map((p) => p.toLowerCase());
    if (parts.length < 2 || parts[0] !== "resources") {
      return false;
    }
    const second = parts[1] || "";
    if (!second) {
      return false;
    }
    if (new Set(["authors", "categories", "reviews"]).has(second)) {
      return false;
    }
    return true;
  }

  function ensureStyle() {
    let style = document.getElementById(STYLE_ID);
    if (style) {
      return;
    }
    style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
a[${MARKER_ATTR}="subscribed"] { box-shadow: inset 0 0 0 2px rgba(255, 216, 77, 0.92) !important; border-radius: 4px !important; }
a[${MARKER_ATTR}="manual"] { box-shadow: inset 0 0 0 2px rgba(225, 68, 68, 0.92) !important; border-radius: 4px !important; }
[${CARD_ATTR}="subscribed"] { position: relative !important; box-shadow: inset 0 0 0 1px rgba(255, 216, 77, 0.75) !important; border-radius: 6px !important; }
[${CARD_ATTR}="manual"] { position: relative !important; box-shadow: inset 0 0 0 1px rgba(225, 68, 68, 0.8) !important; border-radius: 6px !important; }
#${PAGE_BADGE_ID} { display: inline-block !important; margin: 8px 0 10px 0 !important; padding: 4px 10px !important; border-radius: 999px !important; font-size: 12px !important; font-weight: 700 !important; }
#${PAGE_BADGE_ID}[data-kind="subscribed"] { background: rgba(255, 216, 77, 0.2) !important; border: 1px solid rgba(255, 216, 77, 0.7) !important; color: #ffd84d !important; }
#${PAGE_BADGE_ID}[data-kind="manual"] { background: rgba(225, 68, 68, 0.22) !important; border: 1px solid rgba(225, 68, 68, 0.78) !important; color: #ff8c8c !important; }
`;
    (document.head || document.documentElement).appendChild(style);
  }

  function extractDirectResourceToken(value) {
    const raw = String(value || "").trim();
    if (!raw) {
      return "";
    }
    let parsed;
    try {
      parsed = new URL(raw, document.baseURI || window.location.href);
    } catch (_err) {
      return "";
    }
    const parts = parsed.pathname.split("/").filter(Boolean);
    const idx = parts.findIndex((part) => part.toLowerCase() === "resources");
    if (idx < 0 || idx + 1 >= parts.length || idx + 2 !== parts.length) {
      return "";
    }
    const token = String(parts[idx + 1] || "").trim().toLowerCase();
    if (!token || token === "download") {
      return "";
    }
    if (new Set(["authors", "categories", "reviews"]).has(token)) {
      return "";
    }
    return token;
  }

  function kindFromToken(token) {
    const value = String(token || "").trim().toLowerCase();
    if (!value) {
      return "";
    }
    if (subscribedTokens.has(value)) {
      return "subscribed";
    }
    if (manualTokens.has(value)) {
      return "manual";
    }
    const maybeId = value.match(/\.([0-9]+)$/);
    if (maybeId) {
      const id = maybeId[1];
      if (subscribedTokens.has(id)) {
        return "subscribed";
      }
      if (manualTokens.has(id)) {
        return "manual";
      }
    }
    return "";
  }

  function installedKind(href) {
    const raw = String(href || "").trim();
    if (!raw) {
      return "";
    }
    const protocolMatch = raw.match(/^beamng:v1\/(?:subscriptionMod|showMod)\/([A-Za-z0-9_]+)/i);
    if (protocolMatch && protocolMatch[1]) {
      const tag = protocolMatch[1].toLowerCase();
      if (subscribedTagIds.has(tag)) {
        return "subscribed";
      }
      if (manualTagIds.has(tag)) {
        return "manual";
      }
    }
    const token = extractDirectResourceToken(raw);
    if (!token) {
      return "";
    }
    return kindFromToken(token);
  }

  function isListBadgeEligibleAnchor(anchor) {
    if (!(anchor instanceof HTMLAnchorElement)) {
      return false;
    }
    if (anchor.classList.contains("resourceIcon")) {
      return true;
    }
    if (
      anchor.closest("h3.title, .resourceTitle, .structItem-title, .resourceBody h3, .resourceBody .title")
    ) {
      return true;
    }
    return false;
  }

  function clearAnchor(anchor) {
    anchor.removeAttribute(MARKER_ATTR);
    anchor.title = String(anchor.title || "").replace(TITLE_TAG_PATTERN, "").trim();
  }

  function syncAnchor(anchor) {
    if (!(anchor instanceof HTMLAnchorElement)) {
      return;
    }
    if (!active || isResourceDetailPage() || !isListBadgeEligibleAnchor(anchor)) {
      clearAnchor(anchor);
      return;
    }
    const kind = installedKind(anchor.getAttribute("href"));
    if (!kind) {
      clearAnchor(anchor);
      return;
    }
    anchor.setAttribute(MARKER_ATTR, kind);
    const cleanedTitle = String(anchor.title || "").replace(TITLE_TAG_PATTERN, "").trim();
    const label = statusLabel(kind);
    anchor.title = cleanedTitle ? `${cleanedTitle} | ${label}` : label;
  }

  function syncCard(card) {
    if (!(card instanceof Element)) {
      return;
    }
    if (!active || isResourceDetailPage()) {
      card.removeAttribute(CARD_ATTR);
      return;
    }
    const links = card.querySelectorAll("a[href]");
    let kind = "";
    for (const anchor of Array.from(links)) {
      const current = installedKind(anchor.getAttribute("href"));
      if (current === "subscribed") {
        kind = "subscribed";
        break;
      }
      if (current === "manual") {
        kind = "manual";
      }
    }
    if (kind) {
      card.setAttribute(CARD_ATTR, kind);
      return;
    }
    card.removeAttribute(CARD_ATTR);
  }

  function clearResourceBadgeRetryTimer() {
    if (resourceBadgeRetryTimer !== null) {
      window.clearTimeout(resourceBadgeRetryTimer);
      resourceBadgeRetryTimer = null;
    }
  }

  function syncPageBadge() {
    const existing = document.getElementById(PAGE_BADGE_ID);
    if (existing) {
      existing.remove();
    }
    if (!active) {
      return;
    }
    const kind = installedKind(window.location.href);
    if (!kind) {
      return;
    }
    const title = document.querySelector("h1, .p-title-value, .resourceTitle, .PageTitle");
    if (!title || !title.parentElement) {
      return;
    }
    const badge = document.createElement("span");
    badge.id = PAGE_BADGE_ID;
    badge.setAttribute("data-kind", kind);
    badge.textContent = statusLabel(kind);
    badge.style.marginLeft = "8px";
    badge.style.verticalAlign = "middle";
    badge.style.display = "inline-block";
    badge.style.padding = "2px 8px";
    badge.style.borderRadius = "999px";
    badge.style.fontSize = "12px";
    badge.style.fontWeight = "700";
    if (kind === "subscribed") {
      badge.style.border = "1px solid rgba(255, 216, 77, 0.7)";
      badge.style.color = "#ffd84d";
    } else if (kind === "manual") {
      badge.style.border = "1px solid rgba(225, 68, 68, 0.78)";
      badge.style.color = "#ff8c8c";
    }
    title.appendChild(badge);
  }

  function stopObserver() {
    if (!observer) {
      return;
    }
    observer.disconnect();
    observer = null;
  }

  function syncResourcePageBadgeOnly(attempt = 0) {
    syncPageBadge();
    if (document.getElementById(PAGE_BADGE_ID)) {
      return;
    }
    if (!active || !isResourceDetailPage()) {
      return;
    }
    if (attempt >= 10) {
      return;
    }
    clearResourceBadgeRetryTimer();
    resourceBadgeRetryTimer = window.setTimeout(() => {
      resourceBadgeRetryTimer = null;
      syncResourcePageBadgeOnly(attempt + 1);
    }, 300);
  }

  function scan(scope) {
    const root = scope || document;
    if (!root.querySelectorAll) {
      return;
    }
    root.querySelectorAll("a[href]").forEach((anchor) => syncAnchor(anchor));
    root.querySelectorAll(LIST_CARD_SELECTOR).forEach((card) => syncCard(card));
    syncPageBadge();
  }

  function ensureObserver() {
    if (observer) {
      return;
    }
    observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of Array.from(mutation.addedNodes || [])) {
          if (!(node instanceof Element)) {
            continue;
          }
          if (node.matches && node.matches("a[href]")) {
            syncAnchor(node);
          }
          scan(node);
        }
      }
    });
    observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
  }

  function setMarkers(payload) {
    subscribedTokens = new Set((payload.subscribed_tokens || []).map((v) => String(v).trim().toLowerCase()).filter(Boolean));
    manualTokens = new Set((payload.manual_tokens || []).map((v) => String(v).trim().toLowerCase()).filter(Boolean));
    subscribedTagIds = new Set((payload.subscribed_tag_ids || []).map((v) => String(v).trim().toLowerCase()).filter(Boolean));
    manualTagIds = new Set((payload.manual_tag_ids || []).map((v) => String(v).trim().toLowerCase()).filter(Boolean));
    active = true;
    if (isResourceDetailPage()) {
      stopObserver();
      clearResourceBadgeRetryTimer();
      syncResourcePageBadgeOnly();
      return;
    }
    ensureStyle();
    ensureObserver();
    scan(document);
  }

  function goOffline() {
    active = false;
    clearResourceBadgeRetryTimer();
    if (isResourceDetailPage()) {
      stopObserver();
      syncPageBadge();
      return;
    }
    scan(document);
  }

  extApi.runtime.onMessage.addListener((message) => {
    if (!message || !message.type) {
      return;
    }
    if (message.type === "beamng_manager_markers") {
      setMarkers(message.payload || {});
      return;
    }
    if (message.type === "beamng_manager_offline") {
      goOffline();
    }
  });

  extApi.runtime
    .sendMessage({ type: "beamng_manager_request_state" })
    .then((message) => {
      if (!message || !message.type) {
        return;
      }
      if (message.type === "beamng_manager_markers") {
        setMarkers(message.payload || {});
        return;
      }
      goOffline();
    })
    .catch(() => {
      goOffline();
    });
})();
