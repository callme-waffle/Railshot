import { createServer } from "node:http";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { Counter, Histogram, Registry } from "@prometheus-io/client";

export function createDemo() {
  const registry = new Registry();
  const requests = new Counter({
    name: "railshot_http_requests_total",
    help: "Completed application requests excluding health and metrics.",
    labelNames: ["status_class"],
    registers: [registry],
  });
  const duration = new Histogram({
    name: "railshot_http_request_duration_seconds",
    help: "Application request duration.",
    buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
    registers: [registry],
  });
  for (const status of ["2xx", "4xx", "5xx"]) requests.labels(status).inc(0);
  const application = createServer(async (req, res) => {
    const path = new URL(req.url, "http://localhost").pathname;
    if (path === "/health") {
      res.writeHead(200).end("ok");
      return;
    }
    const stop = duration.startTimer();
    res.once("finish", () => {
      requests.labels(`${Math.floor(res.statusCode / 100)}xx`).inc();
      stop();
    });
    if (path === "/api/demo") {
      // Explicit test endpoints affect only this request, never persistent app state.
      const scenario = new URL(req.url, "http://localhost").searchParams.get(
        "scenario",
      );
      if (scenario === "slow")
        await new Promise((done) => setTimeout(done, 800));
      res.writeHead(scenario === "error" ? 503 : 200, {
        "content-type": "application/json",
      });
      res.end(JSON.stringify({ scenario: scenario || "normal", demo: true }));
      return;
    }
    if (path !== "/") {
      res.writeHead(404).end("not found");
      return;
    }
    res.writeHead(200, {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
    });
    res.end(`<!doctype html><html lang="ko"><meta name="viewport" content="width=device-width,initial-scale=1"><meta charset="utf-8"><title>Railshot live demo</title>
      <style>body{font:18px/1.7 system-ui;max-width:640px;margin:8vh auto;padding:24px;background:#f3f8f5;color:#17392e}button{font:inherit;padding:12px;margin:8px;border-radius:8px;border:1px solid #aac7b7;background:white}</style>
      <h1>접속이 운영 데이터가 됩니다</h1><p>이 페이지와 버튼 요청이 Railshot 요청 차트에 집계됩니다. 방문자 수는 수집하지 않습니다.</p>
      <button data-scenario="normal">정상 요청</button><button data-scenario="slow">시험용 지연</button><button data-scenario="error">시험용 오류</button>
      <p id="result" role="status">관측 반영에는 수집 주기에 따른 지연이 있습니다.</p>
      <script>document.querySelectorAll('button').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await fetch('/api/demo?scenario='+b.dataset.scenario);document.querySelector('#result').textContent='시험 요청 결과: HTTP '+r.status;}finally{b.disabled=false;}});</script></html>`);
  });
  // The metrics port is separate so the public app route cannot expose it.
  const metrics = createServer(async (req, res) => {
    if (req.url !== "/metrics") {
      res.writeHead(404).end();
      return;
    }
    res.writeHead(200, { "content-type": registry.contentType });
    res.end(await registry.metrics());
  });
  return { application, metrics, registry };
}
if (
  process.argv[1] &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  const { application, metrics } = createDemo();
  application.listen(Number(process.env.PORT || 8080), "0.0.0.0");
  metrics.listen(Number(process.env.METRICS_PORT || 9400), "0.0.0.0");
}
