import {CONFIG_KEY, DEFAULT_CONTROLLER, MAX_URLS, Outbox, configured, normalizeUrl, publicStatus, requestTarget, selectedUrls, validateController, validateToken} from "./core.js";
import {extensionApi as api, messageListener, restrictStorage} from "./extension-api.js";

const ALARM = "ateable-capture-retry";
const MENU = {root: "ateable-root", link: "ateable-link", media: "ateable-media", selected: "ateable-selected", page: "ateable-page", collect: "ateable-collect"};
const outbox = new Outbox({storage: api.storage.local, fetchImpl: (...args) => fetch(...args), onChange: updateBadge});
let ready;

async function getConfig() {
  return (await api.storage.local.get(CONFIG_KEY))[CONFIG_KEY] || {controllerUrl: DEFAULT_CONTROLLER, token: "", destination: ""};
}

async function status() { return publicStatus(await outbox.snapshot(), await getConfig()); }

async function updateBadge(state) {
  const config = await getConfig();
  const current = publicStatus(state, config);
  const error = current.paused > 0 || current.lastNotice?.type === "error";
  await api.action.setBadgeText({text: !current.configured ? "SET" : error ? "!" : current.count > 999 ? "999+" : current.count ? String(current.count) : ""});
  await api.action.setBadgeBackgroundColor({color: !current.configured || error ? "#a13532" : "#14685a"});
  await api.action.setTitle({title: `Ate it on bucket — ${current.configured ? config.destination || config.bucketName : "set up a bucket"}${current.count ? ` · ${current.count} queued` : ""}${current.paused ? ` · ${current.paused} need attention` : ""}`});
}

async function rebuildMenus() {
  const config = await getConfig();
  await api.contextMenus.removeAll();
  const contexts = ["page", "link", "image", "video", "audio", "selection"];
  api.contextMenus.create({id: MENU.root, title: `Ate it on bucket → ${configured(config) ? (config.destination || config.bucketName).slice(0, 65) : "set up destination"}`, contexts});
  api.contextMenus.create({id: MENU.link, parentId: MENU.root, title: "Save this link", contexts: ["link"]});
  api.contextMenus.create({id: MENU.media, parentId: MENU.root, title: "Save this media URL", contexts: ["image", "video", "audio"]});
  api.contextMenus.create({id: MENU.selected, parentId: MENU.root, title: "Save selected URLs", contexts: ["selection"]});
  api.contextMenus.create({id: MENU.page, parentId: MENU.root, title: "Save current page URL", contexts});
  api.contextMenus.create({id: MENU.collect, parentId: MENU.root, title: "Collect links on this page (up to 5,000)", contexts: ["page"]});
}

// This runs only after the user chooses Collect links, in the browser's isolated world.
// It returns URL/title metadata from the already-open document and makes no requests.
export function enumerateLinks(limit) {
  const items = [];
  const seen = new Set();
  let limitReached = false;
  for (const element of document.querySelectorAll("a[href], area[href]")) {
    let url;
    try { url = new URL(element.href, location.href); } catch { continue; }
    if (!["http:", "https:", "s3:"].includes(url.protocol) || url.username || url.password || !url.hostname || seen.has(url.href)) continue;
    if (items.length >= limit) { limitReached = true; break; }
    seen.add(url.href);
    items.push({url: url.href, title: (element.title || element.textContent || "").trim().slice(0, 512), source_page: location.href});
  }
  return {items, limitReached};
}

async function collect(tabId) {
  if (!Number.isInteger(tabId)) throw new Error("Open a regular webpage to collect links.");
  let results;
  try {
    results = await api.scripting.executeScript({target: {tabId}, func: enumerateLinks, args: [MAX_URLS]});
  } catch {
    throw new Error("The browser cannot read this page. Open a regular HTTP or HTTPS webpage and choose Collect links again.");
  }
  return results[0]?.result || {items: [], limitReached: false};
}

async function capture(items, options = {}) {
  const result = await outbox.enqueue(items, await getConfig(), options);
  // Durable storage completes before any delivery starts.
  await outbox.drain();
  if (options.limitReached) await outbox.notice("Page scan stopped at 5,000 valid links. Later links were not captured. Open the popup to check delivery status.", "warning");
  return result;
}

async function handleContext(info, tab) {
  const config = await getConfig();
  if (!configured(config)) {
    await outbox.notice("Choose a verified bucket capture token in Settings, then capture again.");
    await api.runtime.openOptionsPage();
    return;
  }
  const source = info.pageUrl || tab?.url;
  const metadata = {title: tab?.title, source_page: source};
  switch (info.menuItemId) {
    case MENU.link: return capture([{url: info.linkUrl, ...metadata}]);
    case MENU.media: return capture([{url: info.srcUrl, ...metadata}]);
    case MENU.selected: return capture(selectedUrls(info.selectionText).map(item => ({...item, ...metadata})));
    case MENU.page: return capture([{url: source, title: tab?.title}]);
    case MENU.collect: {
      const result = await collect(tab?.id);
      return capture(result.items, {limitReached: result.limitReached});
    }
  }
}

async function activeTab() {
  const [tab] = await api.tabs.query({active: true, currentWindow: true});
  if (!tab || !normalizeUrl(tab.url)) throw new Error("Open a regular webpage before capturing its URL or links.");
  return tab;
}

async function handleMessage(message, sender) {
  const extensionRoot = api.runtime.getURL("");
  if (sender.id !== api.runtime.id || !sender.url?.startsWith(extensionRoot)) throw new Error("Request must come from this extension's popup or Settings.");
  await ready;
  switch (message?.type) {
    case "status": return status();
    case "getConfig": {
      if (sender.url !== api.runtime.getURL("options.html")) throw new Error("Only Settings can access the capture token.");
      return getConfig();
    }
    case "saveConfig": {
      if (sender.url !== api.runtime.getURL("options.html")) throw new Error("Open Settings to change the capture token.");
      const controllerUrl = validateController(message.config?.controllerUrl);
      const token = validateToken(message.config?.token);
      const target = await requestTarget((...args) => fetch(...args), controllerUrl, token);
      const destination = String(message.config?.destination || target.bucketName).trim().slice(0, 120);
      const config = {controllerUrl, token, destination: destination || target.bucketName, ...target};
      await api.storage.local.set({[CONFIG_KEY]: config});
      await rebuildMenus();
      await updateBadge(await outbox.snapshot());
      return {controllerUrl, destination: config.destination, ...target};
    }
    case "capturePage": {
      const tab = await activeTab();
      return capture([{url: tab.url, title: tab.title}]);
    }
    case "collectLinks": {
      const tab = await activeTab();
      const result = await collect(tab.id);
      return capture(result.items, {limitReached: result.limitReached});
    }
    case "retry": return outbox.retryNow();
    case "recover": {
      const config = await getConfig();
      if (!configured(config)) throw new Error("Set up a verified bucket in Settings first.");
      const target = await requestTarget((...args) => fetch(...args), config.controllerUrl, config.token);
      if (target.bucketId !== config.bucketId) throw new Error("Token now points to a different bucket. Save it in Settings before using it.");
      const result = await outbox.recoverMatching(config);
      await outbox.drain();
      return result;
    }
    case "exportQueue": {
      const state = await outbox.snapshot();
      return {format: "ateable-browser-outbox-v1", exportedAt: new Date().toISOString(), batches: state.batches.map(({token, ...batch}) => batch)};
    }
    case "clearQueue": {
      if (message.confirmation !== "CLEAR") throw new Error("Confirm CLEAR to remove queued captures.");
      return outbox.clear();
    }
    default: throw new Error("Unknown extension action.");
  }
}

api.contextMenus.onClicked.addListener((info, tab) => {
  ready.then(() => handleContext(info, tab)).catch(error => outbox.notice(error.message).catch(() => {}));
});
api.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === ALARM) ready.then(() => outbox.drain()).catch(error => outbox.notice(error.message).catch(() => {}));
});
api.runtime.onInstalled.addListener(() => {
  ready.then(rebuildMenus).catch(error => outbox.notice(error.message).catch(() => {}));
});
api.runtime.onStartup.addListener(() => {
  ready.then(() => outbox.drain()).catch(error => outbox.notice(error.message).catch(() => {}));
});
api.runtime.onMessage.addListener(messageListener(handleMessage));

ready = (async () => {
  await restrictStorage();
  // Recreate on every background start; browser restarts may clear alarms.
  if (!await api.alarms.get(ALARM)) await api.alarms.create(ALARM, {periodInMinutes: 1});
  await updateBadge(await outbox.snapshot());
})();
