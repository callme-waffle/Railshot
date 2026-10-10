// Shared by the dashboard and MCP App; untrusted values are always text nodes.
export const insightsStyle = `
.rs-insights{--ink:#172f2b;--muted:#697c77;--line:#e2eae6;--green:#087f65;box-sizing:border-box;font:14px/1.65 system-ui,-apple-system,sans-serif;color:var(--ink);background:#f5f7f4;padding:32px;max-width:1180px;margin:auto;border-radius:20px;letter-spacing:-.02em}
.rs-insights *{box-sizing:border-box}.rs-insights h2,.rs-insights h3,.rs-insights p{margin:0}.rs-insights button,.rs-insights select,.rs-insights a{font:inherit}.rs-insights button,.rs-insights select{cursor:pointer}.rs-insights button:focus-visible,.rs-insights select:focus-visible,.rs-insights summary:focus-visible,.rs-insights a:focus-visible,.rs-insights input:focus-visible{outline:3px solid #75bea9;outline-offset:4px}
.rs-insights .rs-icon{width:18px;height:18px;flex:none;vertical-align:middle}.rs-insights .rs-eyebrow{font-size:10px;letter-spacing:.17em;font-weight:750;color:var(--muted)}.rs-insights .rs-brand{display:flex;align-items:center;gap:9px;color:var(--green)}.rs-insights .rs-brand .rs-icon{background:var(--green);color:white;padding:5px;width:29px;height:29px;border-radius:9px}
.rs-insights .rs-header,.rs-insights .rs-toolbar,.rs-insights .rs-section-head,.rs-insights .rs-row{display:flex;align-items:center;justify-content:space-between;gap:12px}.rs-insights .rs-header{align-items:flex-start;margin:23px 0}.rs-insights h2{font-size:30px;font-weight:750;line-height:1.25;overflow-wrap:anywhere;letter-spacing:-.045em}.rs-insights .rs-subtitle{color:var(--muted);font-size:12px;margin-top:9px;overflow-wrap:anywhere}.rs-insights .rs-toolbar{flex-wrap:wrap;justify-content:flex-end;gap:7px;padding-top:5px}
.rs-insights .rs-button,.rs-insights select{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid var(--line);background:white;color:var(--ink);border-radius:9px;padding:8px 11px;font-size:12px;white-space:nowrap;text-decoration:none;min-height:37px}.rs-insights .rs-button:hover,.rs-insights select:hover{border-color:#83b5a7;background:#f6faf8}.rs-insights .rs-primary{background:var(--ink);border-color:var(--ink);color:white}.rs-insights .rs-primary:hover{background:var(--green);color:white}.rs-insights .rs-icon-button{padding:8px}
.rs-insights .rs-pill{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:650;border-radius:6px;background:#edf2ef;color:#52645e;padding:3px 8px;white-space:nowrap}.rs-insights .rs-dot{height:6px;width:6px;border-radius:50%;background:currentColor;flex:none}.rs-insights .rs-tone-good{color:#08765c;background:#e7f4ed}.rs-insights .rs-tone-bad{color:#b44832;background:#fcf0eb}.rs-insights .rs-tone-active{color:#5361a2;background:#eeeff9}
.rs-insights .rs-hero{display:flex;align-items:center;gap:16px;padding:22px 24px;background:#183f35;color:#f5fff9;border-radius:14px;position:relative;overflow:hidden}.rs-insights .rs-hero[data-tone=bad]{background:#68382e}.rs-insights .rs-hero[data-tone=neutral]{background:#394c46}.rs-insights .rs-hero-icon{display:grid;place-items:center;width:46px;height:46px;flex:none;border:1px solid #ffffff35;border-radius:14px;background:#ffffff0d}.rs-insights .rs-hero-icon .rs-icon{width:25px;height:25px}.rs-insights .rs-hero h3{font-size:18px;font-weight:650;letter-spacing:-.025em}.rs-insights .rs-hero p{font-size:12px;color:#ffffffb5;margin-top:3px}.rs-insights .rs-hero-meta{margin-left:auto;text-align:right;font-size:11px;color:#ffffffb5;flex:none}.rs-insights .rs-hero-meta span{display:block}.rs-insights .rs-hero-meta b{font-weight:600;color:#e4f1e9}
.rs-insights .rs-cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:16px 0 22px}.rs-insights .rs-card{background:white;border:1px solid var(--line);border-radius:12px;padding:17px 18px}.rs-insights .rs-card-top{display:flex;align-items:center;justify-content:space-between;color:var(--muted);font-size:12px}.rs-insights .rs-card-top .rs-icon{color:#8a9a93}.rs-insights .rs-value{display:block;font-size:29px;line-height:1.3;font-weight:700;letter-spacing:-.045em;margin:11px 0 6px;font-variant-numeric:tabular-nums}.rs-insights .rs-value.rs-text-value{font-size:21px;min-height:38px;display:flex;align-items:center}.rs-insights .rs-value small{font-size:13px;font-weight:500;color:var(--muted);margin-left:4px}.rs-insights .rs-card-caption{font-size:10px;color:var(--muted)}
.rs-insights .rs-panel{background:white;border:1px solid var(--line);border-radius:14px;padding:23px;min-width:0}.rs-insights h3{font-size:15px;font-weight:700}.rs-insights .rs-heading{display:flex;align-items:center;gap:9px}.rs-insights .rs-heading .rs-icon{color:var(--green)}.rs-insights .rs-caption{font-size:11px;color:var(--muted);margin-top:5px}.rs-insights .rs-legend{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);white-space:nowrap}.rs-insights .rs-legend i{width:18px;height:3px;background:var(--green);border-radius:3px}.rs-insights .rs-chart{margin-top:13px}.rs-insights .rs-chart svg{display:block;width:100%;height:auto;overflow:visible}.rs-insights .rs-chart text{font-family:inherit;font-size:10px;fill:#768a80}.rs-insights .rs-chart-readout{display:flex;justify-content:space-between;gap:12px;align-items:center;margin:0 0 4px;font-size:11px;color:var(--muted)}.rs-insights .rs-chart-readout output{font-variant-numeric:tabular-nums;font-weight:600;color:var(--green)}.rs-insights .rs-chart-range{width:100%;height:12px;margin:7px 0 9px;accent-color:var(--green);cursor:ew-resize}.rs-insights .rs-chart-foot{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:10px;color:var(--muted);padding-top:13px;border-top:1px solid #edf1ee}
.rs-insights .rs-grid{display:grid;grid-template-columns:1fr 1.1fr;gap:16px;margin-top:16px}.rs-insights .rs-timeline{list-style:none;padding:0;margin:23px 0 19px}.rs-insights .rs-step{display:flex;gap:12px;position:relative;padding-bottom:23px}.rs-insights .rs-step:last-child{padding-bottom:0}.rs-insights .rs-step:not(:last-child):before{content:'';position:absolute;left:14px;top:31px;bottom:3px;width:1px;background:var(--line)}.rs-insights .rs-step-mark{display:grid;place-items:center;flex:none;width:29px;height:29px;border:1px solid var(--line);border-radius:50%;color:#8b9a94;background:#f8faf8}.rs-insights .rs-step-mark.rs-tone-good{border-color:#c9e9d8;background:#e7f4ed;color:#08765c}.rs-insights .rs-step-mark.rs-tone-bad{color:#b44832;background:#fcf0eb;border-color:#f1d9d0}.rs-insights .rs-step-mark .rs-icon{width:15px;height:15px}.rs-insights .rs-step-info{flex:1;min-width:0}.rs-insights .rs-step-title{display:flex;justify-content:space-between;gap:8px;font-size:12px;font-weight:650}.rs-insights .rs-step-title span:last-child{font-size:11px;color:var(--muted);font-weight:500}.rs-insights .rs-step-info p{font-size:11px;color:var(--muted);margin-top:3px;overflow-wrap:anywhere}
.rs-insights .rs-meta{border-top:1px solid var(--line);padding-top:13px;display:grid;gap:7px}.rs-insights .rs-meta div{display:flex;justify-content:space-between;gap:16px;font-size:11px;color:var(--muted)}.rs-insights code{font:11px/1.6 ui-monospace,SFMono-Regular,monospace;color:#52665e;overflow-wrap:anywhere}.rs-insights .rs-ai-summary{margin:18px 0 14px;padding:14px;background:#f4f7f3;border-radius:9px}.rs-insights .rs-ai-summary p{font-size:12px;line-height:1.8}.rs-insights .rs-ai-summary small{font-size:10px;color:var(--muted)}.rs-insights .rs-file{border:1px solid var(--line);border-radius:8px;margin-top:8px;overflow:hidden}.rs-insights summary{cursor:pointer;list-style:none}.rs-insights summary::-webkit-details-marker{display:none}.rs-insights .rs-file summary{display:flex;align-items:center;gap:8px;padding:10px 12px;font-size:12px}.rs-insights .rs-file summary .rs-icon{color:var(--muted);width:15px;height:15px}.rs-insights .rs-file summary code{flex:1;min-width:0;color:var(--ink)}.rs-insights .rs-chevron{transition:transform .15s}.rs-insights details[open]>summary .rs-chevron{transform:rotate(90deg)}.rs-insights .rs-file p{padding:0 13px 12px;font-size:12px;color:var(--muted);overflow-wrap:anywhere}.rs-insights .rs-checks{display:flex;flex-wrap:wrap;gap:7px;margin-top:15px}.rs-insights .rs-checks .rs-pill{font-size:10px}.rs-insights .rs-checks .rs-icon{width:13px;height:13px}
.rs-insights .rs-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;min-height:195px;gap:8px;color:var(--muted)}.rs-insights .rs-empty .rs-icon{width:30px;height:30px;margin-bottom:3px;color:#8ba397}.rs-insights .rs-empty strong{font-size:14px;color:#50695d}.rs-insights .rs-empty p{font-size:12px;max-width:350px}.rs-insights .rs-notes{margin-top:22px;color:var(--muted);font-size:11px}.rs-insights .rs-notes summary{display:flex;align-items:center;gap:7px}.rs-insights .rs-notes ul{padding-left:20px;margin:10px 0;line-height:1.9}.rs-insights .rs-footer{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:16px;margin-top:18px;font-size:10px;color:var(--muted)}
.rs-insights-dialog{width:min(1180px,96vw);max-height:94vh;border:1px solid #dbe6df;border-radius:20px;padding:0;background:#f5f7f4;box-shadow:0 24px 100px #102e3338}.rs-insights-dialog::backdrop{background:#142c2c75;backdrop-filter:blur(4px)}.rs-insights-dialog>.rs-close{position:absolute;right:16px;top:12px;border:0;border-radius:7px;background:transparent;color:#62776c;font:12px system-ui;padding:8px;cursor:pointer}
@media(max-width:850px){.rs-insights{padding:23px}.rs-insights .rs-header{flex-direction:column}.rs-insights .rs-toolbar{justify-content:flex-start}.rs-insights .rs-card{padding:14px}.rs-insights .rs-value{font-size:25px}.rs-insights .rs-hero-meta{display:none}}
@media(max-width:600px){.rs-insights{padding:18px 14px;border-radius:12px}.rs-insights h2{font-size:26px}.rs-insights .rs-header{margin:18px 0}.rs-insights .rs-cards{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.rs-insights .rs-grid{grid-template-columns:1fr}.rs-insights .rs-panel{padding:17px}.rs-insights .rs-hero{padding:18px 16px;gap:12px;align-items:flex-start}.rs-insights .rs-hero h3{font-size:16px}.rs-insights .rs-hero-icon{width:36px;height:36px;border-radius:10px}.rs-insights .rs-hero-icon .rs-icon{width:20px;height:20px}.rs-insights .rs-hero p{font-size:11px}.rs-insights .rs-legend{font-size:10px}.rs-insights .rs-chart text{font-size:24px}.rs-insights .rs-chart-readout{align-items:flex-start;flex-direction:column;gap:2px}.rs-insights .rs-section-head{gap:8px}.rs-insights .rs-value.rs-text-value{font-size:20px}.rs-insights .rs-step-title{flex-wrap:wrap;gap:3px}}
@media(prefers-reduced-motion:reduce){.rs-insights *{transition:none!important}}`;

const el = (tag, text, className) => {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
};
const svgNode = (tag, attrs, text) => {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs))
    node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  return node;
};
const iconPaths = {
  pulse: "M3 12h4l3-8 4 16 3-8h4",
  check: "m5 12 4 4L19 6",
  alert:
    "M12 8v5m0 3v.01M10 3 2 18a2 2 0 0 0 2 3h16a2 2 0 0 0 2-3L14 3a2 2 0 0 0-4 0Z",
  clock: "M12 8v4l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  arrow: "M4 16 9 11l4 3 7-10m-6 0h6v6",
  shield: "M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4Zm-4 9 3 3 5-6",
  refresh: "M20 8a8 8 0 1 0 0 8M20 3v5h-5",
  download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  box: "m12 3 9 5v9l-9 5-9-5V8l9-5Zm-9 5 9 5 9-5m-9 5v9M7 5.8l9 5",
  spark: "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z",
  file: "M14 2H5v20h14V7l-5-5Zm0 0v6h5M8 13h8m-8 4h6",
  chevron: "m9 5 7 7-7 7",
  info: "M12 11v6m0-10v.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  external: "M14 3h7v7m0-7L10 14M10 3H3v18h18v-7",
};
function icon(name, className = "") {
  const node = svgNode("svg", {
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.7,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    class: `rs-icon ${className}`,
    "aria-hidden": "true",
  });
  node.append(svgNode("path", { d: iconPaths[name] || iconPaths.info }));
  return node;
}
const labels = {
  ready: "수집됨",
  unsupported: "앱 관측 미등록",
  not_configured: "관측 미설정",
  unavailable: "조회 불가",
  no_data: "표본 부족",
  stale: "오래된 관측",
  collection_failed: "수집 실패",
  succeeded: "완료",
  published: "이미지 게시 완료",
  deployed: "적용 완료",
  failed: "실패",
  blocked: "조치 필요",
  running: "진행 중",
  progressing: "진행 중",
  queued: "대기 중",
  pending: "대기 중",
  unknown: "확인 필요",
  analyzing: "AI 처리 중",
  verifying: "재검증 중",
  current: "최신",
  not_run: "실행 전",
  unverified: "미검증",
  idle: "대기 중",
};
const label = (value) => labels[value] || value || "기록 없음";
const time = (value) =>
  Number.isFinite(Date.parse(value))
    ? new Date(value).toLocaleString("ko-KR")
    : "기록 없음";
const shortTime = (value) =>
  new Date(value).toLocaleTimeString("ko-KR", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
  });
const number = (value, suffix = "", precision = suffix === "%" ? 2 : 1) =>
  Number.isFinite(value)
    ? `${value.toLocaleString("ko-KR", { maximumFractionDigits: precision })}${suffix}`
    : "자료 없음";
const tone = (state) =>
  ["succeeded", "published", "deployed"].includes(state)
    ? "good"
    : ["failed", "blocked"].includes(state)
      ? "bad"
      : ["running", "progressing", "analyzing", "verifying"].includes(state)
        ? "active"
        : "neutral";
function pill(text, state) {
  const node = el("span", null, `rs-pill rs-tone-${tone(state)}`);
  node.append(el("i", null, "rs-dot"), document.createTextNode(text));
  return node;
}
// Use the sample timestamp, never the report's fetch time, to decide freshness.
const observationState = (metric) => {
  if (metric?.state !== "ready") return metric?.state;
  const age = Date.now() - Date.parse(metric.observed_at);
  return Number.isFinite(age) && age >= -5000 && age <= 90000
    ? "ready"
    : "stale";
};
const httpSummary = (http) =>
  observationState(http) === "ready"
    ? http.value === 1
      ? "응답 정상"
      : http.value === 0
        ? "응답 실패"
        : "확인 필요"
    : label(observationState(http));
export function insightsMarkdown(data) {
  const d = data.deployment,
    t = data.traffic,
    http = data.observation?.metrics?.http,
    fresh = observationState(t) === "ready";
  return [
    `# ${data.app} 운영 확인`,
    `조회 시각: ${data.checked_at}`,
    `배포 ID: ${data.deployment_id}`,
    `대상: ${data.target_id}`,
    `배포 결과: ${d.status} / ${d.stage}`,
    `소스: ${d.source_commit || "미제공"}`,
    `GitOps: ${d.revision || "미제공"}`,
    `현재 HTTP: ${httpSummary(http)} · 표본 시각: ${http?.observed_at || "없음"}`,
    `트래픽 구간: ${t.window.start} ~ ${t.window.end}`,
    `관측 상태: ${observationState(t)}`,
    `실제 표본 시각: ${t.observed_at || "없음"}`,
    `앱 요청 추정 집계: ${number(fresh ? t.requests : null)}`,
    `5xx 오류율: ${number(fresh ? t.error_percent : null, "%")}`,
    `p95: ${number(fresh ? t.p95_ms : null, " ms")}`,
    `AI 활동 조회: ${data.activity_state}`,
    ...(data.agent_activity?.changes || []).map(
      (c) => `- 적용: ${c.path} — AI 설명: ${c.summary}`,
    ),
    ...(data.agent_activity?.verification || []).map(
      (v) => `- 검사: ${v.label || v.key} — ${v.state}`,
    ),
    "",
    ...data.interpretation,
  ].join("\n\n");
}

let chartSequence = 0;
function chart(traffic, deployedAt) {
  const container = el("div", null, "rs-chart");
  const start = Date.parse(traffic.window.start),
    end = Date.parse(traffic.window.end);
  const points = (traffic.series || []).filter(
    (p) => Date.parse(p.at) >= start && Date.parse(p.at) <= end,
  );
  const values = points.filter((p) => Number.isFinite(p.requests_per_second));
  if (!values.length || !(end > start))
    return empty(
      "pulse",
      "표본 부족",
      "요청 추이를 그릴 수 있는 표본이 아직 없습니다.",
    );
  const max = Math.max(0.1, ...values.map((p) => p.requests_per_second)) * 1.15;
  const x = (at) => 46 + ((Date.parse(at) - start) / (end - start)) * 740;
  const y = (value) => 204 - (value / max) * 158;
  const readout = el("div", null, "rs-chart-readout"),
    output = el("output");
  readout.append(
    el("span", "그래프에 마우스를 올리거나 아래 슬라이더로 시점을 확인하세요."),
    output,
  );
  const svg = svgNode("svg", {
    viewBox: "0 0 810 246",
    role: "img",
    "aria-label": "앱 요청 추이와 배포 검증 시점",
  });
  const gradientId = `rs-traffic-fill-${++chartSequence}`;
  const defs = svgNode("defs", {}),
    gradient = svgNode("linearGradient", {
      id: gradientId,
      x1: 0,
      y1: 0,
      x2: 0,
      y2: 1,
    });
  gradient.append(
    svgNode("stop", {
      offset: "0%",
      "stop-color": "#269c79",
      "stop-opacity": ".2",
    }),
    svgNode("stop", {
      offset: "100%",
      "stop-color": "#269c79",
      "stop-opacity": ".015",
    }),
  );
  defs.append(gradient);
  svg.append(defs);
  for (let i = 0; i <= 4; i++) {
    const value = (max * i) / 4,
      at = y(value);
    svg.append(
      svgNode("line", {
        x1: 46,
        x2: 786,
        y1: at,
        y2: at,
        stroke: "#e7eee8",
        "stroke-dasharray": i ? "3 5" : "",
      }),
      svgNode(
        "text",
        { x: 33, y: at + 4, "text-anchor": "end" },
        value.toLocaleString("ko-KR", { maximumFractionDigits: 2 }),
      ),
    );
  }
  svg.append(svgNode("text", { x: 46, y: 17 }, "req/s"));
  // Split missing samples AND time gaps: missing observations must never become a flat line.
  const segments = [];
  let segment = [];
  for (const point of points) {
    const previous = segment.at(-1);
    if (
      !Number.isFinite(point.requests_per_second) ||
      (previous &&
        Date.parse(point.at) - Date.parse(previous.at) >
          traffic.window.step_seconds * 1500)
    ) {
      if (segment.length) segments.push(segment);
      segment = [];
    }
    if (Number.isFinite(point.requests_per_second)) segment.push(point);
  }
  if (segment.length) segments.push(segment);
  for (const group of segments) {
    const line = group
      .map((p, i) => `${i ? "L" : "M"}${x(p.at)},${y(p.requests_per_second)}`)
      .join(" ");
    svg.append(
      svgNode("path", {
        d: `${line} L${x(group.at(-1).at)},204 L${x(group[0].at)},204 Z`,
        fill: `url(#${gradientId})`,
        class: "rs-chart-area",
      }),
    );
    svg.append(
      svgNode("path", {
        d: line,
        fill: "none",
        stroke: "#138564",
        "stroke-width": 2.5,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
        class: "rs-chart-line",
      }),
    );
    if (group.length === 1)
      svg.append(
        svgNode("circle", {
          cx: x(group[0].at),
          cy: y(group[0].requests_per_second),
          r: 3,
          fill: "#138564",
        }),
      );
  }
  const deployed = Date.parse(deployedAt);
  if (deployed >= start && deployed <= end) {
    svg.append(
      svgNode("line", {
        x1: x(deployedAt),
        x2: x(deployedAt),
        y1: 34,
        y2: 204,
        stroke: "#b4944b",
        "stroke-dasharray": "4 4",
      }),
    );
    svg.append(
      svgNode(
        "text",
        { x: Math.min(x(deployedAt) - 6, 786), y: 29, "text-anchor": "end" },
        "배포 HTTP 검증",
      ),
    );
  }
  for (const fraction of [0, 0.25, 0.5, 0.75, 1])
    svg.append(
      svgNode(
        "text",
        {
          x: 46 + fraction * 740,
          y: 233,
          "text-anchor":
            fraction === 0 ? "start" : fraction === 1 ? "end" : "middle",
        },
        shortTime(start + (end - start) * fraction),
      ),
    );
  const cursor = svgNode("line", {
    y1: 39,
    y2: 204,
    stroke: "#94b9a7",
    "stroke-dasharray": "3 4",
  });
  const dot = svgNode("circle", {
    r: 4,
    fill: "#087f65",
    stroke: "white",
    "stroke-width": 2,
  });
  svg.append(cursor, dot);
  const range = el("input", null, "rs-chart-range");
  range.type = "range";
  range.min = 0;
  range.max = points.length - 1;
  range.step = 1;
  range.setAttribute("aria-label", "요청 추이 시점");
  const selectPoint = (index) => {
    const point = points[index],
      valid = Number.isFinite(point.requests_per_second);
    range.value = index;
    const text = `${shortTime(point.at)} · ${valid ? number(point.requests_per_second, " req/s", 3) : "관측 없음"}`;
    output.textContent = text;
    range.setAttribute("aria-valuetext", text);
    cursor.setAttribute("x1", x(point.at));
    cursor.setAttribute("x2", x(point.at));
    dot.setAttribute("visibility", valid ? "visible" : "hidden");
    if (valid) {
      dot.setAttribute("cx", x(point.at));
      dot.setAttribute("cy", y(point.requests_per_second));
    }
  };
  range.oninput = () => selectPoint(Number(range.value));
  svg.onpointermove = (event) => {
    const rect = svg.getBoundingClientRect();
    const at =
      start +
      Math.max(
        0,
        Math.min(
          1,
          (((event.clientX - rect.left) / rect.width) * 810 - 46) / 740,
        ),
      ) *
        (end - start);
    const index = points.reduce(
      (nearest, p, i) =>
        Math.abs(Date.parse(p.at) - at) <
        Math.abs(Date.parse(points[nearest].at) - at)
          ? i
          : nearest,
      0,
    );
    selectPoint(index);
  };
  selectPoint(points.length - 1);
  container.append(readout, svg, range);
  return container;
}
function empty(name, title, description) {
  const node = el("div", null, "rs-empty");
  node.append(icon(name), el("strong", title), el("p", description));
  return node;
}
function panel(name, title, badge) {
  const node = el("section", null, "rs-panel"),
    header = el("div", null, "rs-section-head"),
    heading = el("div", null, "rs-heading");
  heading.append(icon(name), el("h3", title));
  header.append(heading);
  if (badge) header.append(badge);
  node.append(header);
  return node;
}
function action(name, text, callback, primary = false) {
  const node = el("button", null, `rs-button${primary ? " rs-primary" : ""}`);
  node.type = "button";
  node.append(icon(name), el("span", text));
  node.onclick = callback;
  return node;
}
function deploymentPanel(d) {
  const node = panel(
    "box",
    "배포 타임라인",
    pill(`전체: ${label(d.status)}`, d.status),
  );
  node.append(
    el("p", "이 배포가 통과한 과정 · 현재 응답 상태와 별도", "rs-caption"),
  );
  const list = el("ol", null, "rs-timeline");
  for (const [title, state, description] of [
    ["이미지 빌드 · 게시", d.ci_state, "CI에서 컨테이너 이미지를 준비합니다."],
    [
      "배포 반영",
      d.cd_state,
      d.revision
        ? `GitOps ${d.revision.slice(0, 12)}`
        : "배포 적용 결과를 확인합니다.",
    ],
    [
      "서비스 HTTP 검증",
      d.public_http?.state,
      d.public_http?.verified_at
        ? time(d.public_http.verified_at)
        : "배포 후 외부 응답을 확인합니다.",
    ],
  ]) {
    const row = el("li", null, "rs-step"),
      mark = el("span", null, `rs-step-mark rs-tone-${tone(state)}`),
      info = el("div", null, "rs-step-info"),
      titleRow = el("div", null, "rs-step-title");
    mark.append(
      icon(
        tone(state) === "good"
          ? "check"
          : tone(state) === "bad"
            ? "alert"
            : "clock",
      ),
    );
    titleRow.append(el("span", title), el("span", label(state)));
    info.append(titleRow, el("p", description));
    row.append(mark, info);
    list.append(row);
  }
  const meta = el("div", null, "rs-meta");
  for (const [key, value] of [
    ["소스 커밋", d.source_commit?.slice(0, 12)],
    ["GitOps 커밋", d.revision?.slice(0, 12)],
  ]) {
    const row = el("div");
    row.append(el("span", key), el("code", value || "미제공"));
    meta.append(row);
  }
  node.append(list, meta);
  return node;
}
function agentPanel(data) {
  const agent = data.agent_activity;
  const node = panel(
    "spark",
    "AI 작업 기록",
    agent ? pill(label(agent.state), agent.state) : null,
  );
  node.append(el("p", "에이전트의 작업과 검증 결과", "rs-caption"));
  if (!agent) {
    node.append(
      empty(
        "spark",
        data.activity_state === "unavailable"
          ? "활동을 조회하지 못했어요"
          : "아직 확인된 작업이 없어요",
        "AI가 호출되면 처리 내용과 변경 파일이 여기에 표시됩니다.",
      ),
    );
    return node;
  }
  const summary = el("div", null, "rs-ai-summary");
  summary.append(
    el(
      "p",
      agent.current_action ||
        agent.summary ||
        `${agent.attempt}차 작업 · ${label(agent.state)}`,
    ),
    el(
      "small",
      `${agent.attempt}차 시도 · 활동 관측 ${label(agent.observation?.state)}${agent.updated_at ? ` · ${time(agent.updated_at)}` : ""}`,
    ),
  );
  node.append(summary);
  for (const change of agent.changes || []) {
    const detail = el("details", null, "rs-file"),
      heading = el("summary");
    detail.dataset.detailKey = `file:${change.path}`;
    heading.append(
      icon("file"),
      el("code", change.path),
      el("span", "적용됨", "rs-pill rs-tone-good"),
      icon("chevron", "rs-chevron"),
    );
    detail.append(
      heading,
      el("p", `AI 설명 · ${change.summary || "설명 미제공"}`),
    );
    node.append(detail);
  }
  const checks = el("div", null, "rs-checks");
  for (const verification of agent.verification || []) {
    const badge = el(
      "span",
      null,
      `rs-pill rs-tone-${tone(verification.state)}`,
    );
    badge.append(
      icon(tone(verification.state) === "good" ? "check" : "info"),
      document.createTextNode(
        `${verification.label || verification.key} · ${label(verification.state)}`,
      ),
    );
    checks.append(badge);
  }
  node.append(checks);
  if (agent.omitted?.changes)
    node.append(
      el(
        "p",
        `외 ${agent.omitted.changes}개 변경 · 요약에 포함되지 않음`,
        "rs-caption",
      ),
    );
  return node;
}

export function renderInsights(host, data, { refresh, minutes = 15 } = {}) {
  // Retain expanded evidence across periodic refreshes, but never across deployments.
  const openDetails =
    host.dataset.deploymentId === data.deployment_id
      ? new Set(
          [...host.querySelectorAll("details[open]")].map(
            (node) => node.dataset.detailKey,
          ),
        )
      : new Set();
  host.replaceChildren();
  host.className = "rs-insights";
  host.dataset.deploymentId = data.deployment_id;
  const t = data.traffic,
    d = data.deployment,
    http = data.observation?.metrics?.http;
  const freshTraffic = observationState(t) === "ready",
    httpState = observationState(http);
  const health =
    httpState === "ready" && http?.value === 1
      ? "good"
      : httpState === "ready" && http?.value === 0
        ? "bad"
        : "neutral";
  const brand = el("div", null, "rs-brand rs-eyebrow");
  brand.append(icon("pulse"), el("span", "RAILSHOT / OPERATIONS"));
  host.append(brand);
  const header = el("header", null, "rs-header"),
    heading = el("div"),
    controls = el("nav", null, "rs-toolbar");
  controls.setAttribute("aria-label", "운영 보고서 도구");
  heading.append(
    el("h2", data.app),
    el("p", `${data.target_id} · 운영 인사이트`, "rs-subtitle"),
  );
  const select = el("select");
  select.setAttribute("aria-label", "조회 기간");
  for (const value of [15, 60]) {
    const option = el("option", `최근 ${value}분`);
    option.value = value;
    select.append(option);
  }
  select.value = minutes;
  select.onchange = () => refresh?.(Number(select.value));
  const reload = action("refresh", "새로고침", () =>
    refresh?.(Number(select.value)),
  );
  const download = action(
    "download",
    "보고서 저장",
    () => {
      const url = URL.createObjectURL(
          new Blob([insightsMarkdown(data)], {
            type: "text/markdown;charset=utf-8",
          }),
        ),
        link = el("a");
      link.href = url;
      link.download = "railshot-insights.md";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
    true,
  );
  controls.append(select, reload, download);
  header.append(heading, controls);
  host.append(header);
  const hero = el("div", null, "rs-hero"),
    heroIcon = el("span", null, "rs-hero-icon"),
    heroText = el("div"),
    heroMeta = el("div", null, "rs-hero-meta");
  hero.dataset.tone = health;
  heroIcon.append(
    icon(health === "good" ? "check" : health === "bad" ? "alert" : "pulse"),
  );
  heroText.append(
    el(
      "h3",
      health === "good"
        ? "서비스가 정상 응답하고 있어요"
        : health === "bad"
          ? "서비스 응답을 확인해 주세요"
          : "최신 관측을 기다리고 있어요",
    ),
    el(
      "p",
      health === "good"
        ? "현재 HTTP 관측에서 정상 응답이 확인되었습니다."
        : health === "bad"
          ? "배포 결과와 별개로, 현재 HTTP 관측에서 응답 실패가 확인되었습니다."
          : `${httpSummary(http)} · 관측이 확인되면 현재 상태를 표시합니다.`,
    ),
  );
  heroMeta.append(
    el("b", "현재 HTTP 관측"),
    el("span", time(http?.observed_at)),
  );
  hero.append(heroIcon, heroText, heroMeta);
  host.append(hero);
  const cards = el("div", null, "rs-cards");
  for (const [name, title, value, unit, caption, text] of [
    [
      "pulse",
      "현재 HTTP",
      httpSummary(http),
      "",
      "앱에 대한 가장 최근 관측",
      true,
    ],
    [
      "arrow",
      "앱 요청 집계",
      freshTraffic ? t.requests : null,
      "건",
      `최근 ${minutes}분 · 추정 집계`,
      false,
    ],
    [
      "shield",
      "서버 오류율",
      freshTraffic ? t.error_percent : null,
      "%",
      "전체 요청 중 HTTP 5xx 비율",
      false,
    ],
    [
      "clock",
      "응답 시간 p95",
      freshTraffic ? t.p95_ms : null,
      "ms",
      "요청의 95%가 이 시간 이내 응답",
      false,
    ],
  ]) {
    const card = el("article", null, "rs-card"),
      top = el("div", null, "rs-card-top"),
      metric = el(
        "strong",
        text
          ? value
          : Number.isFinite(value)
            ? number(value, "", unit === "%" ? 2 : 1)
            : "—",
        `rs-value${text ? " rs-text-value" : ""}`,
      );
    top.append(el("span", title), icon(name));
    if (!text && Number.isFinite(value)) metric.append(el("small", unit));
    card.append(
      top,
      metric,
      el(
        "p",
        !text && !Number.isFinite(value)
          ? freshTraffic
            ? "집계할 표본이 없습니다"
            : label(observationState(t))
          : caption,
        "rs-card-caption",
      ),
    );
    cards.append(card);
  }
  host.append(cards);
  const legend = el("div", null, "rs-legend");
  legend.append(el("i"), el("span", "초당 요청 수"));
  const traffic = panel("pulse", "요청 추이", legend);
  traffic.append(
    el(
      "p",
      "앱에 도달한 요청을 시간순으로 확인하세요. 순 방문자 수와는 다릅니다.",
      "rs-caption",
    ),
  );
  if (freshTraffic && t.history_state === "ready")
    traffic.append(chart(t, d.public_http?.verified_at));
  else
    traffic.append(
      empty(
        "pulse",
        label(freshTraffic ? t.history_state : observationState(t)),
        "사용 가능한 최신 표본이 없어 요청 추이를 표시하지 않습니다.",
      ),
    );
  const chartFoot = el("div", null, "rs-chart-foot");
  chartFoot.append(
    el("span", `실제 표본 ${time(t.observed_at)}`),
    el(
      "span",
      `${t.window.step_seconds}초 간격 · 구간 집계는 Prometheus 추정값`,
    ),
  );
  traffic.append(chartFoot);
  host.append(traffic);
  const grid = el("div", null, "rs-grid");
  grid.append(deploymentPanel(d), agentPanel(data));
  host.append(grid);
  const notes = el("details", null, "rs-notes"),
    notesTitle = el("summary"),
    notesList = el("ul");
  notes.dataset.detailKey = "notes";
  notesTitle.append(
    icon("info"),
    el("span", "관측 기준과 해석"),
    icon("chevron", "rs-chevron"),
  );
  for (const note of data.interpretation || [])
    notesList.append(el("li", note));
  notes.append(notesTitle, notesList);
  host.append(notes);
  const footer = el("footer", null, "rs-footer");
  footer.append(
    el("span", `조회 ${time(data.checked_at)} · 30초마다 갱신`),
    el("span", `배포 ${data.deployment_id}`),
  );
  host.append(footer);
  for (const detail of host.querySelectorAll("details"))
    detail.open = openDetails.has(detail.dataset.detailKey);
}
