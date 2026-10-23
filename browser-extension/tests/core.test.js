import test from "node:test";
import assert from "node:assert/strict";
import {BATCH_SIZE, MAX_URLS, OUTBOX_KEY, Outbox, normalizeItems, normalizeUrl, publicStatus, requestTarget, selectedUrls, validateController} from "../core.js";

const config = {controllerUrl: "http://127.0.0.1:8765", token: "capture-token-1", bucketId: "bucket-1", bucketName: "Research", destination: "Read later"};
const item = (id = 1) => ({url: `https://example.test/media/${id}.mp4`, title: `Title ${id}`, source_page: "https://example.test/index"});

function fixture(fetchImpl = async (_url, request) => {
  const payload = JSON.parse(request.body);
  return Response.json({bucket_id: "bucket-1", accepted: payload.items.length, duplicates: 0});
}) {
  const data = {};
  let counter = 0;
  let time = 100000;
  const storage = {
    async get(key) { return {[key]: structuredClone(data[key])}; },
    async set(values) { Object.assign(data, structuredClone(values)); }
  };
  const create = () => new Outbox({storage, fetchImpl, now: () => time, uuid: () => `stable-id-${++counter}`});
  return {data, storage, create, box: create(), advance: milliseconds => { time += milliseconds; }};
}

test("controller allows only HTTP loopback origins", () => {
  assert.equal(validateController("http://localhost:8765/"), "http://localhost:8765");
  assert.equal(validateController("http://[::1]:8765"), "http://[::1]:8765");
  for (const value of ["https://localhost:8765", "http://example.test:8765", "http://192.168.1.20:8765", "http://localhost.evil.test", "http://name:secret@127.0.0.1", "http://127.0.0.1/api", "http://127.0.0.1?token=x", "http://127.0.0.1#fragment", "file:///tmp/api"]) {
    assert.throws(() => validateController(value));
  }
});

test("payload normalization rejects unsafe schemes and credentials", () => {
  const normalized = normalizeItems([
    item(), item(), {url: "s3://private-bucket/folder/object.mp4"},
    {url: "https://u:p@example.test/asset"}, {url: "data:image/png;base64,abc"},
    {url: "blob:https://example.test/asset"}, {url: "javascript:alert(1)"},
    {url: "file:///tmp/file"}, {url: "https://example.test/a\nheader"},
    {url: "https://example.test/2", title: "x".repeat(800), source_page: "https://u:p@example.test/"}
  ]);
  assert.equal(normalized.items.length, 3);
  assert.equal(normalized.skipped, 7);
  assert.equal(normalized.items[2].title.length, 512);
  assert.equal(normalized.items[2].source_page, undefined);
  assert.equal(normalizeUrl("https://example.test/" + "a".repeat(8192)), null);
  assert.deepEqual(selectedUrls("Here: https://example.test/a, and s3://bucket/object.mp4. data:fake"), [{url: "https://example.test/a"}, {url: "s3://bucket/object.mp4"}]);
});

test("queue stores before sending and sends only the controller contract", async () => {
  const calls = [];
  const f = fixture(async (url, request) => {
    assert.equal(f.data[OUTBOX_KEY].batches.length, 1);
    calls.push({url, request});
    return Response.json({bucket_id: "bucket-1", accepted: 1, duplicates: 0});
  });
  await f.box.enqueue([item()], config);
  assert.equal(calls.length, 0);
  await f.box.drain();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://127.0.0.1:8765/api/capture");
  assert.equal(calls[0].request.headers["X-Ateable-Capture"], config.token);
  assert.equal(calls[0].request.credentials, "omit");
  assert.equal(calls[0].request.redirect, "error");
  assert.deepEqual(Object.keys(JSON.parse(calls[0].request.body)).sort(), ["idempotency_key", "items", "origin"]);
  assert.equal(JSON.parse(calls[0].request.body).origin, "browser");
  assert.equal((await f.box.snapshot()).batches.length, 0);
});

test("connection loss and worker restart preserve URLs and exact idempotency payload", async () => {
  const bodies = [];
  let attempts = 0;
  const f = fixture(async (_url, request) => {
    bodies.push(request.body);
    if (attempts++ === 0) throw new TypeError("simulated response lost after commit");
    return Response.json({bucket_id: "bucket-1", accepted: 0, duplicates: 1});
  });
  await f.box.enqueue([item()], config);
  await f.box.drain();
  const first = await f.box.snapshot();
  assert.equal(first.batches[0].items.length, 1);
  assert.equal(first.batches[0].attempts, 1);
  const restarted = f.create();
  await restarted.drain();
  assert.equal(bodies.length, 1, "backoff remains after restart");
  f.advance(30001);
  await restarted.drain();
  assert.equal(bodies[0], bodies[1]);
  assert.equal((await restarted.snapshot()).batches.length, 0);
});

test("401 pauses durable captures without an endless retry loop", async () => {
  let calls = 0;
  const f = fixture(async () => { calls++; return new Response("", {status: 401}); });
  await f.box.enqueue([item()], config);
  await f.box.drain();
  f.advance(86400000);
  await f.create().drain();
  await f.box.retryNow();
  assert.equal(calls, 1);
  assert.equal((await f.box.snapshot()).batches[0].pausedReason, "token");
});

test("same-bucket token replacement preserves key, other destinations remain pinned", async () => {
  const f = fixture(async () => new Response("", {status: 401}));
  await f.box.enqueue([item()], config);
  await f.box.enqueue([item(2)], {...config, token: "other-token", bucketId: "bucket-2", bucketName: "Pictures", destination: "Pictures"});
  await f.box.drain();
  const original = await f.box.snapshot();
  const result = await f.box.recoverMatching({...config, token: "fresh-token"});
  const recovered = await f.box.snapshot();
  assert.equal(result.recovered, 1);
  assert.equal(recovered.batches[0].idempotencyKey, original.batches[0].idempotencyKey);
  assert.equal(recovered.batches[0].token, "fresh-token");
  assert.equal(recovered.batches[0].pausedReason, null);
  assert.equal(recovered.batches[1].token, "other-token");
  assert.equal(recovered.batches[1].pausedReason, "token");
  assert.equal((await f.box.recoverMatching({...config, controllerUrl: "http://localhost:8765"})).recovered, 0);
});

test("concurrent appends and delivery do not lose queued captures", async () => {
  let release;
  let began;
  const started = new Promise(resolve => { began = resolve; });
  const f = fixture(async (_url, request) => {
    const payload = JSON.parse(request.body);
    if (payload.items[0].url === item().url) {
      began();
      await new Promise(resolve => { release = resolve; });
    }
    return Response.json({bucket_id: "bucket-1", accepted: payload.items.length, duplicates: 0});
  });
  await f.box.enqueue([item()], config);
  const delivery = f.box.drain();
  await started;
  await Promise.all([f.box.enqueue([item(2)], config), f.box.enqueue([item(3)], config)]);
  assert.equal((await f.box.snapshot()).batches.length, 3);
  release();
  await delivery;
  assert.equal((await f.box.snapshot()).batches.length, 0);
});

test("a settings switch keeps old controller token and destination pinned", async () => {
  const f = fixture();
  await f.box.enqueue([item()], config);
  await f.box.enqueue([item(2)], {...config, controllerUrl: "http://localhost:8888", token: "token-new", bucketId: "bucket-new", bucketName: "Other", destination: "Other"});
  const state = await f.box.snapshot();
  assert.equal(state.batches[0].endpoint, config.controllerUrl);
  assert.equal(state.batches[0].token, config.token);
  assert.equal(state.batches[0].bucketId, config.bucketId);
  assert.equal(state.batches[1].endpoint, "http://localhost:8888");
});

test("count capacity rejects entire new capture and preserves original queue", async () => {
  const f = fixture();
  await f.box.enqueue(Array.from({length: MAX_URLS}, (_, id) => item(id)), config);
  const before = JSON.stringify(await f.box.snapshot());
  await assert.rejects(() => f.box.enqueue([item(MAX_URLS + 1)], config), /Queue is full/);
  assert.equal(JSON.stringify(await f.box.snapshot()), before);
  assert.equal((await f.box.snapshot()).batches.length, MAX_URLS / BATCH_SIZE);
});

test("byte capacity rejects entire oversized capture and storage failures do not send", async () => {
  const f = fixture();
  await assert.rejects(() => f.box.enqueue(Array.from({length: 600}, (_, id) => ({url: `https://example.test/${id}/` + "a".repeat(6000)})), config), /Queue is full/);
  assert.equal((await f.box.snapshot()).batches.length, 0);
  const failed = new Outbox({storage: {async get() { return {}; }, async set() { throw new Error("disk full"); }}, fetchImpl: () => assert.fail("capture must not fetch after persistence fails"), uuid: () => "x"});
  await assert.rejects(() => failed.enqueue([item()], config), /disk full/);
});

test("pending URLs dedupe per bucket and batching remains bounded", async () => {
  const f = fixture();
  await f.box.enqueue([item()], config);
  const result = await f.box.enqueue([item(), item(2)], config);
  assert.equal(result.queued, 1);
  assert.equal(result.skipped, 1);
  await f.box.enqueue([item()], {...config, bucketId: "other"});
  assert.equal((await f.box.snapshot()).batches.length, 3);
});

test("incomplete, wrong-bucket, and malformed confirmations preserve and pause captures", async () => {
  for (const result of [{bucket_id: "bucket-1", accepted: 0, duplicates: 0}, {bucket_id: "wrong", accepted: 1, duplicates: 0}, {bucket_id: "bucket-1", accepted: -1, duplicates: 2}]) {
    const f = fixture(async () => Response.json(result));
    await f.box.enqueue([item()], config);
    await f.box.drain();
    assert.equal((await f.box.snapshot()).batches[0].pausedReason, "response");
  }
  const malformed = fixture(async () => new Response("not JSON", {status: 200}));
  await malformed.box.enqueue([item()], config);
  await malformed.box.drain();
  assert.equal((await malformed.box.snapshot()).batches[0].pausedReason, "response");
});

test("permanent HTTP errors pause, transient errors retry with exponential backoff", async () => {
  for (const code of [400, 404, 422]) {
    const f = fixture(async () => new Response("", {status: code}));
    await f.box.enqueue([item()], config);
    await f.box.drain();
    assert.equal((await f.box.snapshot()).batches[0].pausedReason, "rejected");
  }
  const f = fixture(async () => new Response("", {status: 503}));
  await f.box.enqueue([item()], config);
  await f.box.drain();
  assert.equal((await f.box.snapshot()).batches[0].nextAttemptAt, 130000);
  f.advance(30000);
  await f.box.drain();
  assert.equal((await f.box.snapshot()).batches[0].nextAttemptAt, 190000);
});

test("status contains counts and labels without any capture tokens", async () => {
  const f = fixture();
  await f.box.enqueue([item()], config);
  const status = publicStatus(await f.box.snapshot(), config);
  assert.equal(status.count, 1);
  assert.equal(status.config.bucketName, "Research");
  assert.equal(JSON.stringify(status).includes(config.token), false);
});

test("capture-target verification is loopback-only, scoped, and redirect-safe", async () => {
  let called = false;
  const target = await requestTarget(async (url, request) => {
    called = true;
    assert.equal(url, `${config.controllerUrl}/api/capture-target`);
    assert.equal(request.headers["X-Ateable-Capture"], config.token);
    assert.equal(request.method, "GET");
    assert.equal(request.credentials, "omit");
    assert.equal(request.redirect, "error");
    return Response.json({bucket_id: "bucket-1", name: "Research"});
  }, config.controllerUrl, config.token);
  assert.deepEqual(target, {bucketId: "bucket-1", bucketName: "Research"});
  assert.equal(called, true);
  await assert.rejects(() => requestTarget(() => assert.fail("no public request"), "http://public.test", config.token), /loopback|Controller/);
  await assert.rejects(() => requestTarget(async () => new Response("", {status: 401}), config.controllerUrl, config.token), /rejected/);
});
