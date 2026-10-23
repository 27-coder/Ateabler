import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import {CONFIG_KEY, OUTBOX_KEY} from "../core.js";

function event() {
  const listeners = [];
  return {listeners, addListener: listener => listeners.push(listener)};
}
const data = {};
const alarms = new Map();
const menus = [];
const calls = [];
globalThis.chrome = {
  runtime: {id: "test-extension", getURL: path => `chrome-extension://test-extension/${path}`, onMessage: event(), onInstalled: event(), onStartup: event(), openOptionsPage: async () => {}},
  storage: {local: {async get(key) { return {[key]: structuredClone(data[key])}; }, async set(values) { Object.assign(data, structuredClone(values)); }, async setAccessLevel(values) { assert.equal(values.accessLevel, "TRUSTED_CONTEXTS"); }}},
  alarms: {onAlarm: event(), async get(name) { return alarms.get(name); }, async create(name, options) { alarms.set(name, options); }},
  action: {async setBadgeText() {}, async setBadgeBackgroundColor() {}, async setTitle() {}},
  contextMenus: {onClicked: event(), async removeAll() { menus.length = 0; }, create(value) { menus.push(value); }},
  tabs: {async query() { return [{id: 7, url: "https://example.test/page", title: "Current page"}]; }},
  scripting: {async executeScript(request) { assert.equal(request.target.tabId, 7); assert.deepEqual(request.args, [5000]); return [{result: {items: [{url: "https://example.test/file"}], limitReached: false}}]; }}
};
globalThis.fetch = async (url, request) => {
  calls.push({url, request});
  if (url.endsWith("/api/capture-target")) return Response.json({bucket_id: "actual-bucket-id", name: "Actual bucket"});
  assert.equal(url, "http://127.0.0.1:8765/api/capture");
  const payload = JSON.parse(request.body);
  return Response.json({bucket_id: "actual-bucket-id", accepted: payload.items.length, duplicates: 0});
};
const {enumerateLinks} = await import("../background.js");
function rpc(type, values = {}, page = "popup.html") {
  return new Promise(resolve => chrome.runtime.onMessage.listeners[0]({type, ...values}, {id: chrome.runtime.id, url: chrome.runtime.getURL(page)}, resolve));
}

test("worker initializes durable retry alarm and rejects token access outside Settings", async () => {
  const response = await rpc("status");
  assert.equal(response.ok, true);
  assert.equal(response.data.configured, false);
  assert.equal(alarms.get("ateable-capture-retry").periodInMinutes, 1);
  assert.equal((await rpc("getConfig")).ok, false);
  assert.equal((await rpc("getConfig", {}, "options.html")).ok, true);
});

test("settings verify actual bucket; captures send metadata only to local controller", async () => {
  const saved = await rpc("saveConfig", {config: {controllerUrl: "http://127.0.0.1:8765", token: "capture-token", destination: "My label"}}, "options.html");
  assert.equal(saved.ok, true);
  assert.equal(saved.data.bucketId, "actual-bucket-id");
  assert.equal(data[CONFIG_KEY].bucketName, "Actual bucket");
  assert.match(menus[0].title, /My label/);
  assert.equal((await rpc("capturePage")).ok, true);
  assert.equal((await rpc("collectLinks")).ok, true);
  assert.equal(data[OUTBOX_KEY].batches.length, 0);
  assert.equal(calls.filter(call => call.request.method === "POST").length, 2);
  assert.ok(calls.every(call => call.url.startsWith("http://127.0.0.1:8765/api/capture")));
  const status = await rpc("status");
  assert.equal(JSON.stringify(status).includes("capture-token"), false);
});

test("untrusted sender and public-endpoint settings never invoke fetch", async () => {
  const before = calls.length;
  const rejected = await rpc("saveConfig", {config: {controllerUrl: "http://public.test", token: "capture-token"}}, "options.html");
  assert.equal(rejected.ok, false);
  const external = await new Promise(resolve => chrome.runtime.onMessage.listeners[0]({type: "capturePage"}, {id: "external", url: "https://example.test"}, resolve));
  assert.equal(external.ok, false);
  assert.equal(calls.length, before);
});

test("DOM enumeration skips unsafe URLs and caps link metadata", () => {
  globalThis.location = {href: "https://example.test/index"};
  globalThis.document = {querySelectorAll: selector => {
    assert.equal(selector, "a[href], area[href]");
    return [
      {href: "data:text/plain,secret", textContent: "unsafe"},
      {href: "https://user:pass@example.test/private", textContent: "credential"},
      {href: "https://example.test/a", textContent: "A"},
      {href: "https://example.test/a", textContent: "duplicate"},
      {href: "https://example.test/b", title: "B title", textContent: "B"},
      {href: "https://example.test/c", textContent: "C"}
    ];
  }};
  const result = enumerateLinks(2);
  assert.equal(result.items.length, 2);
  assert.equal(result.limitReached, true);
  assert.equal(result.items[1].title, "B title");
  assert.equal(result.items[0].source_page, "https://example.test/index");
  delete globalThis.document;
  delete globalThis.location;
});

test("manifest has no broad host, history, downloads, remote code, or automatic page access", async () => {
  const manifest = JSON.parse(await fs.readFile(new URL("../manifest.json", import.meta.url), "utf8"));
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.background.type, "module");
  assert.deepEqual(manifest.host_permissions, ["http://127.0.0.1/*", "http://localhost/*", "http://[::1]/*"]);
  for (const permission of ["history", "downloads", "tabs", "webRequest", "cookies", "debugger"]) assert.equal(manifest.permissions.includes(permission), false);
  assert.equal(manifest.content_scripts, undefined);
  assert.equal(manifest.externally_connectable, undefined);
  assert.ok(manifest.content_security_policy.extension_pages.includes("script-src 'self'"));
});
