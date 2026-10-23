export const DEFAULT_CONTROLLER = "http://127.0.0.1:8765";
export const MAX_URLS = 5000;
export const MAX_QUEUE_BYTES = 3 * 1024 * 1024;
export const BATCH_SIZE = 100;
export const OUTBOX_KEY = "ateableOutboxV1";
export const CONFIG_KEY = "ateableConfigV1";
const MAX_URL_LENGTH = 8192;
const QUEUE_METADATA_RESERVE = 64 * 1024;

export function validateController(value) {
  let url;
  try { url = new URL(String(value).trim()); } catch { throw new Error("Enter a valid local controller URL."); }
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("Controller must use HTTP on 127.0.0.1, localhost, or [::1]. Public and HTTPS endpoints are not allowed.");
  }
  if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("Controller URL must be an origin only, with no credentials, path, query, or fragment.");
  }
  return url.origin;
}

export function validateToken(value) {
  const token = String(value ?? "").trim();
  if (!token || token.length > 2048 || !/^[\x21-\x7e]+$/.test(token)) {
    throw new Error("Paste the bucket's capture token, without spaces. Do not use the administrator key.");
  }
  return token;
}

export function normalizeUrl(value) {
  if (typeof value !== "string" || value.length > MAX_URL_LENGTH || /[\u0000-\u0020\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (!["http:", "https:", "s3:"].includes(url.protocol) || !url.hostname || url.username || url.password) return null;
    return url.href.length <= MAX_URL_LENGTH ? url.href : null;
  } catch { return null; }
}

export function normalizeItems(values) {
  if (!Array.isArray(values) || values.length > MAX_URLS) throw new Error(`One capture can contain at most ${MAX_URLS.toLocaleString()} URLs.`);
  const seen = new Set();
  const items = [];
  let skipped = 0;
  for (const value of values) {
    const url = normalizeUrl(value?.url);
    if (!url || seen.has(url)) { skipped++; continue; }
    seen.add(url);
    const item = {url};
    if (typeof value.title === "string" && value.title.trim()) item.title = value.title.trim().slice(0, 512);
    const sourcePage = normalizeUrl(value.source_page);
    if (sourcePage) item.source_page = sourcePage;
    items.push(item);
  }
  return {items, skipped};
}

export function selectedUrls(text) {
  const matches = String(text ?? "").match(/(?:https?|s3):\/\/[^\s<>"`]+/gi) ?? [];
  if (matches.length > MAX_URLS) throw new Error(`Selection contains more than ${MAX_URLS.toLocaleString()} URLs. Select a smaller part.`);
  return matches.map(value => ({url: value.replace(/[.,;!?]+$/, "").replace(/[)\]}]+$/, "")}));
}

export function configured(config) {
  return Boolean(config?.controllerUrl && config?.token && config?.bucketId && config?.bucketName);
}

export function validateConfig(config) {
  if (!configured(config)) throw new Error("Set up a verified bucket capture token in Settings first.");
  return {
    controllerUrl: validateController(config.controllerUrl),
    token: validateToken(config.token),
    bucketId: String(config.bucketId),
    bucketName: String(config.bucketName).slice(0, 512),
    destination: String(config.destination || config.bucketName).trim().slice(0, 120)
  };
}

function emptyState() {
  return {version: 1, batches: [], lastNotice: null, lastSuccessAt: null, lastResult: null};
}
function byteSize(value) { return new TextEncoder().encode(JSON.stringify(value)).byteLength; }
function countItems(state) { return state.batches.reduce((count, batch) => count + batch.items.length, 0); }

export async function requestTarget(fetchImpl, controllerUrl, token) {
  const controller = validateController(controllerUrl);
  const captureToken = validateToken(token);
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 12000);
  try {
    const response = await fetchImpl(`${controller}/api/capture-target`, {
      method: "GET", headers: {"X-Ateable-Capture": captureToken}, credentials: "omit",
      redirect: "error", cache: "no-store", referrerPolicy: "no-referrer", signal: abort.signal
    });
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) throw new Error("Capture token was rejected. Copy a current bucket capture token from the controller.");
      throw new Error(`Controller returned HTTP ${response.status}. Check that the current controller is running.`);
    }
    const body = await response.json();
    if (!body || typeof body.bucket_id !== "string" || !body.bucket_id || typeof body.name !== "string" || !body.name) throw new Error("Controller did not return a valid capture target.");
    return {bucketId: body.bucket_id, bucketName: body.name};
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Controller verification timed out. Start the local controller and try again.");
    if (error instanceof TypeError) throw new Error("Cannot reach the local controller. Start it, check the port, and try again.");
    throw error;
  } finally { clearTimeout(timeout); }
}

export class Outbox {
  constructor({storage, fetchImpl, now = Date.now, uuid = () => crypto.randomUUID(), onChange = async () => {}}) {
    this.storage = storage;
    this.fetchImpl = fetchImpl;
    this.now = now;
    this.uuid = uuid;
    this.onChange = onChange;
    this.serial = Promise.resolve();
    this.draining = null;
  }

  _exclusive(operation) {
    const next = this.serial.then(operation, operation);
    this.serial = next.catch(() => {});
    return next;
  }

  async _read() {
    const values = await this.storage.get(OUTBOX_KEY);
    const state = values[OUTBOX_KEY];
    if (!state) return emptyState();
    if (state.version !== 1 || !Array.isArray(state.batches)) throw new Error("Saved queue needs recovery. Export the extension storage before changing it.");
    return state;
  }

  async _write(state) {
    if (countItems(state) > MAX_URLS || byteSize(state) > MAX_QUEUE_BYTES) throw new Error("Queue storage limit reached. URLs are preserved; export or drain the existing queue first.");
    await this.storage.set({[OUTBOX_KEY]: state});
    await this.onChange(state);
  }

  async snapshot() { return this._exclusive(() => this._read()); }

  async notice(message, type = "error") {
    return this._exclusive(async () => {
      const state = await this._read();
      state.lastNotice = {message, type, at: this.now()};
      await this._write(state);
    });
  }

  async enqueue(values, inputConfig, {limitReached = false} = {}) {
    const config = validateConfig(inputConfig);
    const normalized = normalizeItems(values);
    if (!normalized.items.length) throw new Error("No valid HTTP, HTTPS, or S3 URLs were found. Data, blob, script, file, and credential-bearing URLs are skipped.");
    return this._exclusive(async () => {
      const state = await this._read();
      const pending = new Set(state.batches.filter(batch => batch.endpoint === config.controllerUrl && batch.bucketId === config.bucketId).flatMap(batch => batch.items.map(item => item.url)));
      const items = normalized.items.filter(item => !pending.has(item.url));
      const skipped = normalized.skipped + normalized.items.length - items.length;
      const candidate = structuredClone(state);
      for (let offset = 0; offset < items.length; offset += BATCH_SIZE) {
        candidate.batches.push({
          id: this.uuid(), idempotencyKey: this.uuid(), items: items.slice(offset, offset + BATCH_SIZE),
          endpoint: config.controllerUrl, token: config.token, bucketId: config.bucketId,
          bucketName: config.bucketName, destination: config.destination, createdAt: this.now(),
          attempts: 0, nextAttemptAt: 0, pausedReason: null, lastError: null
        });
      }
      candidate.lastNotice = {type: "success", at: this.now(), message: `${items.length} URL${items.length === 1 ? "" : "s"} saved to the local queue for ${config.destination}.${skipped ? ` ${skipped} invalid or duplicate URL${skipped === 1 ? " was" : "s were"} skipped.` : ""}${limitReached ? " Page scan stopped at the 5,000-link limit; later links were not captured." : ""}`};
      // Keep room for per-batch failure diagnostics, so a full queue can persist
      // revoked-token pauses without discarding URLs or repeatedly resending.
      if (countItems(candidate) > MAX_URLS || byteSize(candidate) > MAX_QUEUE_BYTES - QUEUE_METADATA_RESERVE) {
        throw new Error(`Queue is full (limit ${MAX_URLS.toLocaleString()} URLs / 3 MiB). This capture was not saved. Start the controller to drain the queue, or export and clear it before capturing again.`);
      }
      await this._write(candidate);
      return {queued: items.length, skipped, limitReached};
    });
  }

  async retryNow() {
    await this._exclusive(async () => {
      const state = await this._read();
      state.batches.forEach(batch => { if (!batch.pausedReason) batch.nextAttemptAt = 0; });
      await this._write(state);
    });
    return this.drain();
  }

  async recoverMatching(inputConfig) {
    const config = validateConfig(inputConfig);
    return this._exclusive(async () => {
      const state = await this._read();
      let recovered = 0;
      for (const batch of state.batches) {
        if (batch.endpoint !== config.controllerUrl || batch.bucketId !== config.bucketId) continue;
        batch.token = config.token;
        batch.pausedReason = null;
        batch.lastError = null;
        batch.nextAttemptAt = 0;
        recovered += batch.items.length;
      }
      state.lastNotice = {type: "success", at: this.now(), message: `${recovered} queued URL${recovered === 1 ? "" : "s"} will retry for verified bucket ${config.bucketName}.`};
      await this._write(state);
      return {recovered};
    });
  }

  async clear() {
    return this._exclusive(async () => {
      const state = await this._read();
      if (this.draining) throw new Error("A delivery is in progress. Wait a moment before clearing the queue.");
      const count = countItems(state);
      state.batches = [];
      state.lastNotice = {type: "success", at: this.now(), message: `${count} queued URLs removed from this browser.`};
      await this._write(state);
      return {removed: count};
    });
  }

  drain() {
    if (this.draining) return this.draining;
    this.draining = this._drain().finally(() => { this.draining = null; });
    return this.draining;
  }

  async _drain() {
    let sent = 0;
    for (let turn = 0; turn < 4; turn++) {
      const batch = await this._exclusive(async () => {
        const state = await this._read();
        return structuredClone(state.batches.find(item => !item.pausedReason && item.nextAttemptAt <= this.now()) ?? null);
      });
      if (!batch) break;
      const outcome = await this._send(batch);
      await this._exclusive(async () => {
        const state = await this._read();
        const index = state.batches.findIndex(item => item.id === batch.id && item.idempotencyKey === batch.idempotencyKey);
        if (index === -1) return;
        if (outcome.ok) {
          state.batches.splice(index, 1);
          state.lastSuccessAt = this.now();
          state.lastResult = {bucketId: batch.bucketId, bucketName: batch.bucketName, ...outcome.result};
          state.lastNotice = {type: "success", at: this.now(), message: `Delivered to ${batch.destination}: ${outcome.result.accepted} new, ${outcome.result.duplicates} already saved.`};
          sent += batch.items.length;
        } else {
          const stored = state.batches[index];
          stored.attempts++;
          stored.lastError = outcome.error;
          stored.pausedReason = outcome.pausedReason || null;
          stored.nextAttemptAt = this.now() + Math.min(3600000, 30000 * 2 ** Math.min(stored.attempts - 1, 7));
          state.lastNotice = {type: "error", at: this.now(), message: outcome.error};
        }
        await this._write(state);
      });
    }
    return {sent};
  }

  async _send(batch) {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 12000);
    try {
      const endpoint = validateController(batch.endpoint);
      const token = validateToken(batch.token);
      const payload = {items: batch.items, idempotency_key: batch.idempotencyKey, origin: "browser"};
      const response = await this.fetchImpl(`${endpoint}/api/capture`, {
        method: "POST", headers: {"Content-Type": "application/json", "X-Ateable-Capture": token},
        body: JSON.stringify(payload), credentials: "omit", redirect: "error", cache: "no-store",
        referrerPolicy: "no-referrer", signal: abort.signal
      });
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) return {ok: false, pausedReason: "token", error: `Capture token rejected for ${batch.destination}. Queue kept; get a new token for the same bucket in Settings, then use Recover matching queue.`};
        if (response.status >= 400 && response.status < 500 && ![408, 429].includes(response.status)) return {ok: false, pausedReason: "rejected", error: `Controller rejected queued URLs for ${batch.destination} (HTTP ${response.status}). Queue kept; check the controller, then recover the matching queue or export it.`};
        return {ok: false, error: `Controller unavailable for ${batch.destination} (HTTP ${response.status}). URLs are saved locally and will retry.`};
      }
      const result = await response.json();
      if (result?.bucket_id !== batch.bucketId || !Number.isSafeInteger(result.accepted) || !Number.isSafeInteger(result.duplicates) || result.accepted < 0 || result.duplicates < 0 || result.accepted + result.duplicates !== batch.items.length) {
        return {ok: false, pausedReason: "response", error: `Controller response did not confirm every URL for ${batch.destination}. Queue kept; verify the controller and recover the matching queue.`};
      }
      return {ok: true, result: {accepted: result.accepted, duplicates: result.duplicates}};
    } catch (error) {
      if (error.name === "SyntaxError") return {ok: false, pausedReason: "response", error: `Controller returned unreadable confirmation for ${batch.destination}. Queue kept for recovery.`};
      return {ok: false, error: `Cannot reach controller for ${batch.destination}. URLs are saved locally; retries continue when the browser is open.`};
    } finally { clearTimeout(timeout); }
  }
}

export function publicStatus(state, config) {
  const groups = new Map();
  for (const batch of state.batches) {
    const key = `${batch.endpoint}|${batch.bucketId}`;
    if (!groups.has(key)) groups.set(key, {controllerUrl: batch.endpoint, bucketId: batch.bucketId, bucketName: batch.bucketName, destination: batch.destination, count: 0, paused: 0, error: null});
    const group = groups.get(key);
    group.count += batch.items.length;
    if (batch.pausedReason) group.paused += batch.items.length;
    if (batch.lastError) group.error = batch.lastError;
  }
  const count = countItems(state);
  const paused = state.batches.filter(batch => batch.pausedReason).reduce((total, batch) => total + batch.items.length, 0);
  const safeConfig = configured(config) ? {controllerUrl: config.controllerUrl, bucketId: config.bucketId, bucketName: config.bucketName, destination: config.destination} : {controllerUrl: DEFAULT_CONTROLLER};
  return {configured: configured(config), config: safeConfig, count, paused, bytes: byteSize(state), maxCount: MAX_URLS, maxBytes: MAX_QUEUE_BYTES, groups: [...groups.values()], lastNotice: state.lastNotice, lastSuccessAt: state.lastSuccessAt, lastResult: state.lastResult};
}
