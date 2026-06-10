"use strict";

const LB_URL = "http://127.0.0.1:5000/api/browser-tab";

// Dedupe rapid-fire navigations: only send after tab has been stable for 500ms
const pendingUpdates = {};

// Track the currently foregrounded tab for dwell-time calculations
// Shape: { tabId, url, title, activatedAt }
let activeTab = null;

function send(payload) {
  fetch(LB_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch(() => {});
}

function scheduleUpdate(tabId, payload) {
  clearTimeout(pendingUpdates[tabId]);
  pendingUpdates[tabId] = setTimeout(() => {
    delete pendingUpdates[tabId];
    send(payload);
  }, 500);
}

// Send a dwell event for a tab that is losing focus or being closed.
// Ignores trivially short dwells (< 500 ms) to filter noise.
function sendDwell(tab, endTime) {
  if (!tab || !tab.url) return;
  const duration_ms = endTime - tab.activatedAt;
  if (duration_ms < 500) return;
  send({
    event_type: "dwell",
    title: tab.title || "",
    url: tab.url,
    tab_id: String(tab.tabId),
    duration_ms,
    is_foreground: 1,
  });
}

// User switched to a different tab
browser.tabs.onActivated.addListener(({ tabId }) => {
  const now = Date.now();

  // Close out dwell for the tab that just lost focus
  if (activeTab) {
    sendDwell(activeTab, now);
  }

  browser.tabs.get(tabId).then((tab) => {
    if (!tab.url || tab.url.startsWith("about:") || tab.url.startsWith("moz-extension:")) {
      activeTab = null;
      return;
    }
    // activatedAt uses `now` (the moment activation fired), not the async resolution time
    activeTab = { tabId, url: tab.url, title: tab.title || "", activatedAt: now };
    send({
      event_type: "activated",
      title: tab.title || "",
      url: tab.url,
      tab_id: String(tabId),
      is_foreground: 1,
    });
  }).catch(() => { activeTab = null; });
});

// Tab navigated to a new URL
browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  if (!tab.url || tab.url.startsWith("about:") || tab.url.startsWith("moz-extension:")) return;

  const isForeground = (activeTab && activeTab.tabId === tabId) ? 1 : 0;

  if (isForeground) {
    // Foreground tab navigated to a new page — end the dwell for the old URL
    const now = Date.now();
    sendDwell(activeTab, now);
    activeTab = { tabId, url: tab.url, title: tab.title || "", activatedAt: now };
  }

  scheduleUpdate(tabId, {
    event_type: "navigated",
    title: tab.title || "",
    url: tab.url,
    tab_id: String(tabId),
    is_foreground: isForeground,
  });
});

// New tab opened
browser.tabs.onCreated.addListener((tab) => {
  send({
    event_type: "created",
    title: tab.title || "",
    url: tab.url || "",
    tab_id: String(tab.id),
    is_foreground: 0,
  });
});

// Tab closed — flush dwell if it was the active tab
browser.tabs.onRemoved.addListener((tabId) => {
  if (activeTab && activeTab.tabId === tabId) {
    sendDwell(activeTab, Date.now());
    activeTab = null;
  }
  send({
    event_type: "closed",
    title: "",
    url: "",
    tab_id: String(tabId),
  });
});
