import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { readFile, mkdir } from "node:fs/promises";
import { chromium } from "playwright";

const fixture = () => {
  const now = Date.now();
  return {
    deployment_id: "demo-one",
    app: "insights-demo",
    target_id: "demo-local",
    checked_at: new Date(now).toISOString(),
    deployment: {
      status: "succeeded",
      stage: "complete",
      ci_state: "published",
      cd_state: "deployed",
      source_commit: "a".repeat(40),
      revision: "b".repeat(40),
      public_http: {
        state: "succeeded",
        verified_at: new Date(now - 60000).toISOString(),
      },
    },
    observation: {
      metrics: {
        http: {
          state: "ready",
          value: 0,
          observed_at: new Date(now).toISOString(),
        },
      },
    },
    agent_activity: {
      state: "succeeded",
      attempt: 1,
      changes: [
        {
          path: "Dockerfile",
          summary: "<img src=x onerror=alert(1)>",
          status: "applied",
        },
      ],
      verification: [
        { key: "image.build", label: "이미지 빌드", state: "succeeded" },
      ],
      observation: { state: "current" },
    },
    activity_state: "current",
    traffic: {
      state: "ready",
      observed_at: new Date(now).toISOString(),
      scope: "app_target",
      requests: 32,
      error_percent: 6.25,
      p95_ms: 780,
      history_state: "ready",
      window: {
        start: new Date(now - 900000).toISOString(),
        end: new Date(now).toISOString(),
        step_seconds: 30,
      },
      series: Array.from({ length: 31 }, (_, i) => ({
        at: new Date(now - 900000 + i * 30000).toISOString(),
        requests_per_second: i < 20 ? 0.1 : ((i % 4) + 1) / 4,
      })),
    },
    interpretation: [
      "트래픽은 앱 요청이며 방문자 수가 아닙니다.",
      "배포 결과와 현재 상태는 별개입니다.",
    ],
  };
};

test("operational view separates historical success from current failure, escapes evidence and renders responsive charts", async (t) => {
  const source = await readFile(
    new URL("../../apps/dashboard/src/insights-view.js", import.meta.url),
  );
  const server = createServer((req, res) => {
    res.setHeader(
      "content-type",
      req.url === "/view.js" ? "text/javascript" : "text/html",
    );
    res.end(
      req.url === "/view.js" ? source : '<html><main id="host"></main></html>',
    );
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_EXECUTABLE || undefined,
  });
  t.after(async () => {
    await browser.close();
    server.close();
  });
  const page = await browser.newPage({
    viewport: { width: 1100, height: 1000 },
  });
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.evaluate(async (data) => {
    const m = await import("/view.js");
    const s = document.createElement("style");
    s.textContent = m.insightsStyle;
    document.head.append(s);
    window.render = m.renderInsights;
    m.renderInsights(document.querySelector("#host"), data);
  }, fixture());
  assert.match(await page.locator("#host").innerText(), /응답 실패/);
  assert.match(await page.locator("#host").innerText(), /전체: 완료/);
  assert.equal(await page.locator("#host img").count(), 0);
  assert.equal(await page.locator(".rs-chart-line").count(), 1);
  // Keyboard navigation exposes the actual sample, not an interpolated value.
  const slider = page.getByRole("slider", { name: "요청 추이 시점" });
  await slider.focus();
  await slider.press("Home");
  assert.match(await slider.getAttribute("aria-valuetext"), /0.1 req\/s/);
  await page.locator(".rs-file summary").click();
  assert.match(
    await page.locator(".rs-file").innerText(),
    /<img src=x onerror=alert\(1\)>/,
  );
  await page.evaluate(
    (data) => window.render(document.querySelector("#host"), data),
    fixture(),
  );
  assert.equal(await page.locator(".rs-file").getAttribute("open"), "");
  await page.locator(".rs-file summary").click();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "보고서 저장" }).click();
  assert.equal((await download).suggestedFilename(), "railshot-insights.md");
  await mkdir(new URL("../../outputs/", import.meta.url), { recursive: true });
  await page.screenshot({
    path: new URL("../../outputs/insights-desktop.png", import.meta.url)
      .pathname,
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
    true,
  );
  await page.screenshot({
    path: new URL("../../outputs/insights-mobile-ready.png", import.meta.url)
      .pathname,
    fullPage: true,
  });
  const gaps = fixture();
  gaps.traffic.series[10].requests_per_second = null;
  gaps.traffic.series.splice(20, 2);
  await page.evaluate(
    (data) => window.render(document.querySelector("#host"), data),
    gaps,
  );
  assert.equal(await page.locator(".rs-chart-line").count(), 3);
  await slider.focus();
  await slider.press("Home");
  for (let i = 0; i < 10; i++) await slider.press("ArrowRight");
  assert.match(await slider.getAttribute("aria-valuetext"), /관측 없음/);
  assert.equal(
    await page.locator(".rs-chart circle").last().getAttribute("visibility"),
    "hidden",
  );
  const stale = fixture();
  stale.traffic.observed_at = "2000-01-01T00:00:00Z";
  stale.observation.metrics.http.observed_at = "2000-01-01T00:00:00Z";
  await page.evaluate(
    (data) => window.render(document.querySelector("#host"), data),
    stale,
  );
  assert.equal(await page.locator(".rs-chart svg").count(), 0);
  assert.match(await page.locator("#host").innerText(), /오래된 관측/);
  await page.screenshot({
    path: new URL("../../outputs/insights-mobile.png", import.meta.url)
      .pathname,
    fullPage: true,
  });
});

test("bundled MCP App initializes through the official host bridge and refreshes without a model call", async (t) => {
  const { build } = await import("esbuild");
  const bundle = await build({
    stdin: {
      contents: `
    import { AppBridge, PostMessageTransport } from '@modelcontextprotocol/ext-apps/app-bridge';
    const frame = document.querySelector('iframe');
    const bridge = new AppBridge(null, {name:'test-host',version:'1'}, {serverTools:{}});
    const result = () => ({content:[],structuredContent:window.overview,_meta:{overview:window.overview}});
    bridge.oncalltool = async params => { window.calls=(window.calls||0)+1; window.lastArgs=params.arguments; return result(); };
    bridge.oninitialized = async () => { await bridge.sendToolInput({arguments:{deployment_id:'demo-one',minutes:15}}); await bridge.sendToolResult(result()); };
    await bridge.connect(new PostMessageTransport(frame.contentWindow,frame.contentWindow));
    window.bridge=bridge;frame.src='/app';`,
      resolveDir: new URL("../../", import.meta.url).pathname,
    },
    bundle: true,
    write: false,
    format: "esm",
    platform: "browser",
  });
  const app = await readFile(
    new URL("../../apps/agent/src/insights-app.html", import.meta.url),
  );
  const server = createServer((req, res) => {
    res.setHeader(
      "content-type",
      req.url === "/host.js" ? "text/javascript" : "text/html",
    );
    res.end(
      req.url === "/host.js"
        ? bundle.outputFiles[0].text
        : req.url === "/app"
          ? app
          : '<iframe style="width:100%;height:900px" sandbox="allow-scripts allow-same-origin allow-downloads"></iframe>',
    );
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_EXECUTABLE || undefined,
  });
  t.after(async () => {
    await browser.close();
    server.close();
  });
  const page = await browser.newPage();
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.evaluate(async (data) => {
    window.overview = data;
    await import("/host.js");
  }, fixture());
  const frame = page.frameLocator("iframe");
  await frame
    .getByRole("heading", { name: "insights-demo", exact: true })
    .waitFor();
  await frame.getByRole("button", { name: "새로고침", exact: true }).click();
  await page.waitForFunction(() => window.calls === 1);
  assert.equal(
    await page.evaluate(() => window.lastArgs.deployment_id),
    "demo-one",
  );
  await page.evaluate(() => window.bridge.teardownResource({}));
});
