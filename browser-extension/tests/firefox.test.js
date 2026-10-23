import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import {CONFIG_KEY, OUTBOX_KEY} from "../core.js";

function event() { const listeners = []; return {listeners, addListener: listener => listeners.push(listener)}; }
const data = {};
const alarms = new Map();
const menus = [];
const calls = [];
const badges = [];
let captureCode = 200;
globalThis.browser = {
  runtime: {id: "ateable-capture@ateable.local", getURL: path => `moz-extension://test-uuid/${path}`, onMessage: event(), onInstalled: event(), onStartup: event(), openOptionsPage: async () => {}},
  storage: {onChanged: event(), local: {async get(key) { return {[key]: structuredClone(data[key])}; }, async set(values) { Object.assign(data, structuredClone(values)); }}},
  alarms: {onAlarm: event(), async get(name) { return alarms.get(name); }, async create(name, options) { alarms.set(name, options); }},
  action: {async setBadgeText(value) { badges.push(value.text); }, async setBadgeBackgroundColor() {}, async setTitle() {}},
  contextMenus: {onClicked: event(), async removeAll() { menus.length = 0; }, create(value) { menus.push(value); return value.id; }},
  tabs: {async query() { return [{id: 9, url: "https://example.test/page", title: "Firefox page"}]; }},
  scripting: {async executeScript(request) { assert.equal(request.target.tabId, 9); assert.deepEqual(request.args, [5000]); return [{result: {items: [{url: "https://example.test/firefox-link"}], limitReached: false}}]; }}
};
// Firefox also exposes chrome.*, but code must use its Promise-native browser API.
globalThis.chrome = {runtime: {}, storage: {local: {get() { assert.fail("Firefox chrome callback alias must not be used"); }}}};
globalThis.fetch = async (url, request) => {
  calls.push({url, request});
  assert.ok(url.startsWith("http://127.0.0.1:8765/api/capture"));
  if (url.endsWith("/api/capture-target")) return Response.json({bucket_id: "firefox-bucket", name: "Firefox bucket"});
  if (captureCode !== 200) return new Response("", {status: captureCode});
  const payload = JSON.parse(request.body);
  return Response.json({bucket_id: "firefox-bucket", accepted: payload.items.length, duplicates: 0});
};
await import("../background.js");
function rpc(type, values = {}, page = "popup.html") {
  const result = browser.runtime.onMessage.listeners[0]({type, ...values}, {id: browser.runtime.id, url: browser.runtime.getURL(page)});
  assert.equal(typeof result?.then, "function", "Firefox messages return native Promises");
  return result;
}
browser.runtime.sendMessage = message => rpc(message.type, message);
async function eventually(predicate) {
  for (let attempt = 0; attempt < 50; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.fail("Expected background event completion");
}

test("Firefox event page starts without unsupported storage.setAccessLevel and registers listeners", async () => {
  const response = await rpc("status");
  assert.equal(response.ok, true);
  assert.equal(alarms.get("ateable-capture-retry").periodInMinutes, 1);
  assert.equal(browser.contextMenus.onClicked.listeners.length, 1);
  assert.equal(browser.alarms.onAlarm.listeners.length, 1);
  assert.equal(browser.runtime.onStartup.listeners.length, 1);
  assert.equal(browser.runtime.onInstalled.listeners.length, 1);
  assert.ok(badges.includes("SET"));
  assert.equal((await rpc("getConfig")).ok, false);
});

test("Firefox verifies target, creates menus and supports popup Promise requests and scripting", async () => {
  const saved = await rpc("saveConfig", {config: {controllerUrl: "http://127.0.0.1:8765", token: "firefox-scoped-token", destination: "Later"}}, "options.html");
  assert.equal(saved.ok, true);
  assert.equal(data[CONFIG_KEY].bucketId, "firefox-bucket");
  assert.equal(menus.length, 6);
  assert.match(menus[0].title, /Later/);
  const {request} = await import("../ui.js");
  assert.equal((await request("status")).configured, true);
  assert.equal((await rpc("capturePage")).ok, true);
  assert.equal((await rpc("collectLinks")).ok, true);
  assert.equal(data[OUTBOX_KEY].batches.length, 0);
  assert.equal(JSON.stringify(await rpc("status")).includes("firefox-scoped-token"), false);
});

test("Firefox right-click capture remains durable offline and startup/alarm retry drains it", async () => {
  captureCode = 503;
  browser.contextMenus.onClicked.listeners[0]({menuItemId: "ateable-link", linkUrl: "https://example.test/offline", pageUrl: "https://example.test/page"}, {id: 9, title: "Page"});
  await eventually(() => data[OUTBOX_KEY].batches[0]?.attempts === 1);
  const key = data[OUTBOX_KEY].batches[0].idempotencyKey;
  captureCode = 200;
  data[OUTBOX_KEY].batches[0].nextAttemptAt = 0;
  browser.runtime.onStartup.listeners[0]();
  await eventually(() => data[OUTBOX_KEY].batches.length === 0);
  const bodies = calls.filter(call => call.request.method === "POST").map(call => JSON.parse(call.request.body));
  assert.equal(bodies.at(-1).idempotency_key, key);
  captureCode = 503;
  await rpc("capturePage");
  assert.equal(data[OUTBOX_KEY].batches.length, 1);
  captureCode = 200;
  data[OUTBOX_KEY].batches[0].nextAttemptAt = 0;
  browser.alarms.onAlarm.listeners[0]({name: "ateable-capture-retry"});
  await eventually(() => data[OUTBOX_KEY].batches.length === 0);
});

test("Firefox revoked token pauses and same-target recovery keeps the batch key", async () => {
  captureCode = 401;
  await rpc("capturePage");
  const batch = structuredClone(data[OUTBOX_KEY].batches[0]);
  assert.equal(batch.pausedReason, "token");
  const before = calls.length;
  await rpc("retry");
  assert.equal(calls.length, before);
  assert.ok(badges.includes("!"));
  captureCode = 200;
  const response = await rpc("recover");
  assert.equal(response.ok, true);
  assert.equal(data[OUTBOX_KEY].batches.length, 0);
  const payload = JSON.parse(calls.at(-1).request.body);
  assert.equal(payload.idempotency_key, batch.idempotencyKey);
});

test("Firefox manifest discloses metadata and token, uses modules and only loopback hosts", async () => {
  const firefox = JSON.parse(await fs.readFile(new URL("../manifest.firefox.json", import.meta.url), "utf8"));
  const chrome = JSON.parse(await fs.readFile(new URL("../manifest.json", import.meta.url), "utf8"));
  assert.deepEqual(firefox.background, {scripts: ["background.js"], type: "module"});
  assert.equal(firefox.minimum_chrome_version, undefined);
  assert.equal(firefox.browser_specific_settings.gecko.strict_min_version, "140.0");
  assert.deepEqual(firefox.browser_specific_settings.gecko.data_collection_permissions.required, ["authenticationInfo", "browsingActivity", "websiteContent"]);
  assert.deepEqual(firefox.host_permissions, chrome.host_permissions);
  assert.deepEqual(firefox.permissions, chrome.permissions);
  assert.equal(firefox.content_scripts, undefined);
  assert.equal(firefox.externally_connectable, undefined);
});

test("adapter selects namespace, guards unsupported storage API, and normalizes both message styles", async () => {
  const {getExtensionApi, messageListener, restrictStorage} = await import("../extension-api.js");
  const chromeOnly = {runtime: {}, storage: {local: {}}};
  assert.equal(getExtensionApi({chrome: chromeOnly}).api, chromeOnly);
  assert.equal(getExtensionApi({browser, chrome: chromeOnly}).api, browser);
  await restrictStorage(browser);
  let restricted = false;
  await restrictStorage({storage: {local: {async setAccessLevel(value) { restricted = value.accessLevel === "TRUSTED_CONTEXTS"; }}}});
  assert.equal(restricted, true);
  const handler = async message => { if (message.fail) throw new Error("expected rejection"); return message.value; };
  const firefoxListener = messageListener(handler, true);
  assert.deepEqual(await firefoxListener({value: 7}, {}), {ok: true, data: 7});
  assert.deepEqual(await firefoxListener({fail: true}, {}), {ok: false, error: "expected rejection"});
  const chromeListener = messageListener(handler, false);
  const answer = await new Promise(resolve => assert.equal(chromeListener({value: 8}, {}, resolve), true));
  assert.deepEqual(answer, {ok: true, data: 8});
});
