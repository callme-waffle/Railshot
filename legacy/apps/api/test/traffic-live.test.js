import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { setTimeout } from "node:timers/promises";
import { createTrafficObserver } from "../src/traffic.js";

test(
  "local demo HTTP requests become real Prometheus summaries and chart samples",
  { skip: process.env.RAILSHOT_LIVE_TRAFFIC_TEST !== "1", timeout: 60000 },
  async (t) => {
    const dir = await mkdtemp(join(tmpdir(), "railshot-live-traffic-"));
    t.after(() => rm(dir, { recursive: true, force: true }));
    const configPath = join(dir, "observer.json");
    await writeFile(
      configPath,
      JSON.stringify({
        version: 1,
        targets: [
          {
            target_id: "demo-local",
            app: "insights-demo",
            namespace: "demo",
            node_instance: "demo:9100",
            cluster_instance: "demo:8081",
            probe_url: "http://127.0.0.1:18080/health",
            prometheus_url: "http://127.0.0.1:19090",
            traffic_instance: "demo:9400",
          },
        ],
      }),
      { mode: 0o600 },
    );
    const observe = createTrafficObserver({ configPath });
    // At least two real scrapes are needed for rate/increase.
    await setTimeout(11000);
    for (let i = 0; i < 12; i++)
      assert.equal(
        (await fetch("http://127.0.0.1:18080/api/demo?scenario=normal")).status,
        200,
      );
    for (let i = 0; i < 3; i++)
      assert.equal(
        (await fetch("http://127.0.0.1:18080/api/demo?scenario=error")).status,
        503,
      );
    await fetch("http://127.0.0.1:18080/api/demo?scenario=slow");
    await setTimeout(6500);
    const actual = await observe({
      target_id: "demo-local",
      app: "insights-demo",
    });
    assert.equal(actual.state, "ready");
    assert.ok(actual.requests >= 15);
    assert.ok(actual.error_percent > 0 && actual.error_percent < 100);
    assert.ok(actual.p95_ms > 0);
    assert.equal(actual.history_state, "ready");
    assert.ok(actual.series.some((p) => p.requests_per_second > 0));
    await mkdir(new URL("../../../outputs/", import.meta.url), {
      recursive: true,
    });
    await writeFile(
      new URL("../../../outputs/insights-live-traffic.json", import.meta.url),
      JSON.stringify(actual, null, 2),
    );
  },
);
