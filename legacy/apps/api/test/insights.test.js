import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { once } from "node:events";
import { appObserverBinding } from "../src/metrics.js";
import { createTrafficObserver } from "../src/traffic.js";
import { createAppServer } from "../src/server.js";
import { ProductError } from "../src/product.js";

const now = 1800000000000;
test("traffic is exact-app bound, bounded and never treats missing/stale samples as healthy zero", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "railshot-traffic-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const configPath = join(dir, "observer.json");
  await writeFile(
    configPath,
    JSON.stringify({
      version: 1,
      targets: [
        {
          target_id: "target-one",
          app: "demo-app",
          namespace: "demo",
          node_instance: "10.0.0.1:30910",
          cluster_instance: "10.0.0.1:30081",
          probe_url: "https://app.example/health",
          prometheus_url: "http://prometheus.internal:9090",
          traffic_instance: "10.0.0.1:30940",
        },
      ],
    }),
    { mode: 0o600 },
  );
  let calls = 0,
    age = 10,
    up = 1,
    count = 12,
    missing = false,
    failure = false,
    rangeFailure = false;
  const observe = createTrafficObserver({
    configPath,
    now: () => now,
    fetchImpl: async (url) => {
      calls++;
      assert.equal(url.origin, "http://prometheus.internal:9090");
      assert.match(
        url.searchParams.get("query"),
        /app="demo-app",target_id="target-one"/,
      );
      if (failure) throw new Error("private error");
      if (url.pathname.endsWith("query_range")) {
        if (rangeFailure) throw new Error("range failed");
        assert.equal(url.searchParams.get("step"), "30");
        return Response.json({
          status: "success",
          data: {
            resultType: "matrix",
            result: [
              {
                metric: {},
                values: [
                  [now / 1000 - 30, "0.5"],
                  [now / 1000, "NaN"],
                ],
              },
            ],
          },
        });
      }
      const samples = {
        up,
        observed: now / 1000 - age,
        sample: now / 1000 - age,
        requests: count,
        error_percent: 25,
        p95_ms: 400,
      };
      return Response.json({
        status: "success",
        data: {
          resultType: "vector",
          result: missing
            ? []
            : Object.entries(samples).map(([key, value]) => ({
                metric: { railshot_metric: key },
                value: [now / 1000, String(value)],
              })),
        },
      });
    },
  });
  const record = { target_id: "target-one", app: "demo-app" };
  const result = await observe(record);
  assert.equal(result.state, "ready");
  assert.equal(result.requests, 12);
  assert.equal(result.error_percent, 25);
  assert.equal(result.series[1].requests_per_second, null);
  assert.equal(result.scope, "app_target");
  assert.ok(!JSON.stringify(result).includes("internal"));
  const before = calls;
  assert.equal(
    (await observe({ ...record, app: "other" })).state,
    "unsupported",
  );
  assert.equal(calls, before);
  await assert.rejects(observe(record, 999));
  assert.equal(calls, before);
  age = 91;
  assert.equal((await observe(record)).state, "stale");
  age = 10;
  up = 0;
  assert.equal((await observe(record)).state, "collection_failed");
  up = 1;
  missing = true;
  assert.equal((await observe(record)).state, "no_data");
  missing = false;
  count = 0;
  const zero = await observe(record);
  assert.equal(zero.requests, 0);
  assert.equal(zero.error_percent, null);
  assert.equal(zero.p95_ms, null);
  rangeFailure = true;
  assert.equal((await observe(record)).history_state, "unavailable");
  rangeFailure = false;
  failure = true;
  assert.equal((await observe(record)).state, "unavailable");
});

test("runtime app bindings require both environment and application identity, preserving legacy bindings", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "railshot-runtime-traffic-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const configPath = join(dir, "observer.json");
  const row = { target_id: "k3s-aws", application_id: "app-one", app: "demo-app",
    namespace: "app-one", node_instance: "10.0.0.1:31490", cluster_instance: "10.0.0.1:31491",
    probe_url: "https://app.example/health", prometheus_url: "http://prometheus.internal:9090",
    traffic_instance: "10.0.0.1:30940" };
  const record = { target_id: "app-one", environment_target_id: "k3s-aws", app: "demo-app" };
  await writeFile(configPath, JSON.stringify({ version: 1, targets: [row] }), { mode: 0o600 });
  let calls = 0;
  const observe = createTrafficObserver({ configPath, now: () => now, fetchImpl: async (url) => {
    calls++;
    assert.match(url.searchParams.get("query"), /target_id="app-one"/);
    return Response.json({ status: "success", data: { resultType: "vector", result: [] } });
  } });
  assert.equal((await observe(record)).state, "no_data");
  assert.equal(calls, 1);
  for (const changed of [{ target_id: "app-other" }, { environment_target_id: "k3s-gcp" },
    { environment_target_id: undefined }, { app: "other-app" }]) {
    assert.equal((await observe({ ...record, ...changed })).state, "unsupported");
  }
  assert.equal(calls, 1);
  assert.equal(appObserverBinding([row], record).target_id, "app-one");
  const legacy = { ...row, target_id: "app-one" }; delete legacy.application_id;
  assert.equal(appObserverBinding([legacy], record).target_id, "app-one");
  // Two competing bindings must not silently choose a different collection endpoint.
  assert.throws(() => appObserverBinding([legacy, row], record), /Ambiguous/);
  await writeFile(configPath, JSON.stringify({ version: 1, targets: [legacy, row] }), { mode: 0o600 });
  assert.equal((await observe(record)).state, "unavailable");
  assert.equal(calls, 1);
});

test("insights HTTP routes authorize first, reject writes, preserve partial data and never dispatch work", async (t) => {
  let calls = 0;
  const product = {
    getDeployment: async (id, session) => {
      assert.equal(session, null);
      if (id !== "owned") throw new ProductError(404, "NOT_FOUND", "없음");
      return {
        id,
        app: "demo-app",
        target_id: "target-one",
        status: "succeeded",
        stage: "complete",
        cd: { state: "deployed" },
        observation: {
          metrics: {
            http: {
              state: "ready",
              value: 0,
              observed_at: new Date().toISOString(),
            },
          },
        },
      };
    },
    getDeploymentEvents: async () => {
      throw new Error("collection failed");
    },
    getDeploymentLogs: async () => ({ state: "no_data", entries: [] }),
    getDeploymentDiagnostics: async () => ({
      state: "ready",
      logs: [{ text: "x".repeat(4000) }, { text: "y" }, { text: "z" }],
    }),
  };
  const server = createAppServer({
    product,
    service: null,
    observeTraffic: async () => {
      calls++;
      return { state: "unsupported" };
    },
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => {
    server.closeAllConnections();
    server.close();
  });
  const base = `http://127.0.0.1:${server.address().port}/api/v1/deployments`;
  assert.equal((await fetch(base + "/foreign/insights")).status, 404);
  assert.equal(calls, 0);
  for (const query of [
    "?minutes=99",
    "?minutes=15&minutes=60",
    "?url=http://evil",
  ])
    assert.equal((await fetch(base + "/owned/insights" + query)).status, 422);
  assert.equal(
    (await fetch(base + "/owned/insights", { method: "POST" })).status,
    405,
  );
  const response = await fetch(base + "/owned/insights");
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.deployment.status, "succeeded");
  assert.equal(result.observation.metrics.http.value, 0);
  assert.equal(result.activity_state, "unavailable");
  assert.equal(result.agent_activity, null);
  assert.equal(calls, 1);
  const evidence = await (
    await fetch(base + "/owned/evidence?area=build")
  ).json();
  assert.equal(evidence.logs.length, 2);
  assert.equal(Buffer.byteLength(evidence.logs[0].text), 2000);
  assert.equal((await fetch(base + "/owned/evidence?area=shell")).status, 422);
});
