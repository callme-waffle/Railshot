import test from "node:test";
import assert from "node:assert/strict";
import { Client, InMemoryTransport } from "@modelcontextprotocol/client";
import { createToolServer } from "../src/mcp-tools.js";

test("MCP exposes a read-only overview with chart points outside model text and a self-contained UI", async (t) => {
  const value = {
    deployment_id: "dep-one",
    traffic: {
      state: "ready",
      series: [{ at: "2026-10-04T01:00:00Z", requests_per_second: 12 }],
    },
  };
  let calls = 0;
  const server = createToolServer({
    appOverview: async (id, minutes) => {
      calls++;
      assert.equal(id, "dep-one");
      assert.equal(minutes, 15);
      return value;
    },
  });
  const client = new Client({ name: "test", version: "1" });
  const [a, b] = InMemoryTransport.createLinkedPair();
  await server.connect(a);
  await client.connect(b);
  t.after(async () => {
    await client.close();
    await server.close();
  });
  const { tools } = await client.listTools();
  const tool = tools.find((t) => t.name === "get_app_overview");
  assert.equal(tool.annotations.readOnlyHint, true);
  assert.equal(tool._meta.ui.resourceUri, "ui://railshot/insights.html");
  const result = await client.callTool({
    name: "get_app_overview",
    arguments: { deployment_id: "dep-one" },
  });
  assert.equal(result.isError, undefined);
  assert.equal(result._meta.overview.traffic.series[0].requests_per_second, 12);
  assert.equal(result.structuredContent.traffic.series, undefined);
  assert.ok(!result.content[0].text.includes("requests_per_second"));
  assert.match(result.structuredContent.dashboard_url, /\?insights=dep-one$/);
  const resource = await client.readResource({
    uri: tool._meta.ui.resourceUri,
  });
  assert.equal(resource.contents[0].mimeType, "text/html;profile=mcp-app");
  assert.match(resource.contents[0].text, /Railshot/);
  assert.ok(!resource.contents[0].text.includes("<script src="));
  const invalid = await client.callTool({
    name: "get_app_overview",
    arguments: { deployment_id: "dep-one", minutes: 999 },
  });
  assert.equal(invalid.isError, true);
  assert.equal(calls, 1);
});
