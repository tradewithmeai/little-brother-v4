"use strict";

const LB_URL = "http://127.0.0.1:5000/api/browser-tab";
const RETRY_MS = 5000;

// Dedupe rapid-fire updates: only send after the tab has been stable for 500ms
const pendingUpdates = {};

function send(payload) {
  fetch(LB_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch(() => {
    // LB not running — silent fail, will pick up next event
  });
}

function scheduleUpdate(tabId, payload) {
  clearTimeout(pendingUpdates[tabId]);
  pendingUpdates[tabId] = setTimeout(() => {
    delete pendingUpdates[tabId];
    send(payload);
  }, 500);
}

// Tab navigated to a new URL or title changed
browser.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  if (!tab.url || tab.url.startsWith("about:") || tab.url.startsWith("moz-extension:")) return;
  scheduleUpdate(tabId, {
    event_type: "navigated",
    title: tab.title || "",
    url: tab.url,
    tab_id: String(tabId),
  });
});

// User switched to a different tab
browser.tabs.onActivated.addListener(({ tabId }) => {
  browser.tabs.get(tabId).then((tab) => {
    if (!tab.url || tab.url.startsWith("about:") || tab.url.startsWith("moz-extension:")) return;
    send({
      event_type: "activated",
      title: tab.title || "",
      url: tab.url,
      tab_id: String(tabId),
    });
  }).catch(() => {});
});

// New tab opened
browser.tabs.onCreated.addListener((tab) => {
  send({
    event_type: "created",
    title: tab.title || "",
    url: tab.url || "",
    tab_id: String(tab.id),
  });
});

// Tab closed
browser.tabs.onRemoved.addListener((tabId, removeInfo) => {
  send({
    event_type: "closed",
    title: "",
    url: "",
    tab_id: String(tabId),
  });
});
