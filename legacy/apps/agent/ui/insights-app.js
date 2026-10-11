import { App } from "@modelcontextprotocol/ext-apps";
import {
  renderInsights,
  insightsStyle,
} from "../../dashboard/src/insights-view.js";

const app = new App({ name: "Railshot 운영 인사이트", version: "1.0.0" });
const style = document.createElement("style");
style.textContent = insightsStyle;
document.head.append(style);
const host = document.getElementById("insights");
let current,
  timer,
  generation = 0,
  minutes = 15,
  disposed = false;
const receive = (result) => {
  // UI-only payload avoids asking the model to read every point on every refresh.
  const value = result._meta?.overview || result.structuredContent;
  if (!value?.deployment_id || !value.traffic || result.isError)
    throw new Error("운영 정보를 확인하지 못했습니다.");
  if (current && value.deployment_id !== current.deployment_id)
    throw new Error("배포가 변경되었습니다.");
  current = value;
  minutes = Math.round(
    (Date.parse(value.traffic.window.end) -
      Date.parse(value.traffic.window.start)) /
      60000,
  );
  renderInsights(host, current, { refresh, minutes });
};
const schedule = () => {
  clearTimeout(timer);
  if (!disposed) timer = setTimeout(() => refresh(minutes), 30000);
};
async function refresh(next = minutes) {
  if (!current || disposed) return;
  minutes = next;
  const seq = ++generation;
  clearTimeout(timer);
  try {
    const result = await app.callServerTool({
      name: "get_app_overview",
      arguments: { deployment_id: current.deployment_id, minutes },
    });
    if (seq !== generation || disposed) return;
    receive(result);
  } catch {
    if (seq !== generation || disposed) return;
    host.textContent = "운영 정보 조회 실패. 다음 조회에서 다시 확인합니다.";
  }
  schedule();
}
app.ontoolresult = (result) => {
  generation++;
  clearTimeout(timer);
  try {
    receive(result);
    schedule();
  } catch {
    host.textContent = "운영 정보를 표시할 수 없습니다.";
  }
};
app.onteardown = async () => {
  disposed = true;
  generation++;
  clearTimeout(timer);
  return {};
};
window.addEventListener("pagehide", () => {
  disposed = true;
  generation++;
  clearTimeout(timer);
});
await app.connect();
