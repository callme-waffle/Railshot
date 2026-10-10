import { appObserverBinding, observerConfiguration, prometheusQuery } from "./metrics.js";

const named = (name, expression) =>
  `label_replace((${expression}), "railshot_metric", "${name}", "", "")`;
const unavailable = (state, window) => ({
  state,
  scope: "app_target",
  window,
  observed_at: null,
  requests: null,
  error_percent: null,
  p95_ms: null,
  series: [],
  truncated: false,
});
const scalar = (rows, name) => {
  const matches = rows.filter((row) => row.metric?.railshot_metric === name);
  if (
    matches.length !== 1 ||
    typeof matches[0].value?.[1] !== "string" ||
    !matches[0].value[1].trim()
  )
    return null;
  const value = Number(matches[0].value[1]);
  return Number.isFinite(value) && value >= 0 ? value : null;
};

// Traffic belongs to an app/target/time window, not necessarily one deployed revision.
// Only an operator's exact app binding can select a Prometheus instance or target.
export function createTrafficObserver({
  configPath,
  fetchImpl = fetch,
  now = Date.now,
} = {}) {
  return async (record, minutes = 15) => {
    if (![15, 60].includes(minutes))
      throw new Error("Unsupported traffic window");
    const end = Math.floor(now() / 1000),
      start = end - minutes * 60,
      step = minutes === 15 ? 30 : 60;
    const window = {
      start: new Date(start * 1000).toISOString(),
      end: new Date(end * 1000).toISOString(),
      step_seconds: step,
    };
    const empty = (state) => unavailable(state, window);
    if (!configPath) return empty("not_configured");
    try {
      const config = observerConfiguration(configPath);
      if (config.collector && Date.parse(config.collector.expires_at) <= now())
        return empty("unavailable");
      const target = appObserverBinding(config.targets, record);
      if (!target?.traffic_instance) return empty("unsupported");
      const labels = `job="app_traffic",instance=${JSON.stringify(target.traffic_instance)},app=${JSON.stringify(record.app)},target_id=${JSON.stringify(record.target_id)}`;
      const counter = `railshot_http_requests_total{${labels}}`,
        histogram = `railshot_http_request_duration_seconds_bucket{${labels}}`;
      const up = `up{${labels}}`,
        count = `sum(increase(${counter}[${minutes}m]))`;
      const snapshot = [
        named("up", up),
        named("observed", `timestamp(${up})`),
        named("sample", `min(timestamp(${counter}))`),
        named("requests", count),
        named(
          "error_percent",
          `100 * sum(increase(railshot_http_requests_total{${labels},status_class="5xx"}[${minutes}m])) / (${count})`,
        ),
        named(
          "p95_ms",
          `1000 * histogram_quantile(0.95, sum by (le) (rate(${histogram}[${minutes}m])))`,
        ),
      ].join(" or ");
      const rows = await prometheusQuery(
        target.prometheus_url,
        { query: snapshot, time: end },
        fetchImpl,
      );
      const observed = scalar(rows, "observed"),
        sample = scalar(rows, "sample"),
        alive = scalar(rows, "up");
      if (observed === null || alive === null) return empty("no_data");
      if (end - observed > 90 || observed > end + 5) return empty("stale");
      if (alive === 0) return empty("collection_failed");
      if (alive !== 1 || sample === null) return empty("no_data");
      if (end - sample > 90 || sample > end + 5) return empty("stale");
      const requests = scalar(rows, "requests");
      if (requests === null) return empty("no_data");
      const result = {
        ...empty("ready"),
        observed_at: new Date(Math.min(observed, sample) * 1000).toISOString(),
        requests,
        error_percent: requests > 0 ? scalar(rows, "error_percent") : null,
        p95_ms: requests > 0 ? scalar(rows, "p95_ms") : null,
      };
      if (result.error_percent > 100) result.error_percent = null;
      // Range samples use the evaluation timestamp; actual freshness is above.
      // Mask missing/failed scrapes instead of drawing a fabricated zero line.
      try {
        const query = `sum(rate(${counter}[2m])) and on() (${up} == 1) and on() (time() - timestamp(${up}) <= 90)`;
        const history = await prometheusQuery(
          target.prometheus_url,
          { query, start, end, step },
          fetchImpl,
          true,
        );
        if (
          history.length > 1 ||
          history.some(
            (row) => !Array.isArray(row.values) || row.values.length > 121,
          )
        )
          throw new Error("Ambiguous series");
        result.series = (history[0]?.values || []).map(([time, text]) => {
          if (!Number.isFinite(time) || time < start || time > end)
            throw new Error("Invalid sample");
          const value =
            typeof text === "string" && text.trim() ? Number(text) : NaN;
          return {
            at: new Date(time * 1000).toISOString(),
            requests_per_second:
              Number.isFinite(value) && value >= 0 ? value : null,
          };
        });
        result.history_state = result.series.length ? "ready" : "no_data";
      } catch {
        result.history_state = "unavailable";
      }
      return result;
    } catch {
      return empty("unavailable");
    }
  };
}
