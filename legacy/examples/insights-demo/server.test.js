import test from "node:test";
import assert from "node:assert/strict";
import { once } from "node:events";
import { createDemo } from "./server.js";

test("real requests, explicit faults, and health checks have distinct metrics", async (t) => {
  const { application, metrics, registry } = createDemo();
  for (const server of [application, metrics]) {
    server.listen(0, "127.0.0.1");
    await once(server, "listening");
  }
  t.after(() => {
    application.closeAllConnections();
    metrics.closeAllConnections();
    application.close();
    metrics.close();
  });
  const url = `http://127.0.0.1:${application.address().port}`;
  await fetch(url + "/health");
  await fetch(url + "/");
  assert.equal((await fetch(url + "/api/demo?scenario=error")).status, 503);
  assert.equal((await fetch(url + "/api/demo?scenario=normal")).status, 200);
  const output = await registry.metrics();
  assert.match(output, /railshot_http_requests_total\{status_class="2xx"\} 2/);
  assert.match(output, /railshot_http_requests_total\{status_class="5xx"\} 1/);
  assert.match(output, /railshot_http_request_duration_seconds_count 3/);
  assert.equal(
    (await fetch(url + "/metrics")).status,
    404,
    "public app port must not expose metrics",
  );
  assert.match(
    await (
      await fetch(`http://127.0.0.1:${metrics.address().port}/metrics`)
    ).text(),
    /railshot_http_requests_total/,
  );
});
