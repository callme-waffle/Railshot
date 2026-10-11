import { renderInsights, insightsStyle } from "./insights-view.js";

// One controller owns polling and cancellation; closing the view never starts work.
export function openInsights(record, request) {
  const dialog = document.createElement("dialog"),
    style = document.createElement("style"),
    content = document.createElement("div");
  style.textContent = insightsStyle;
  dialog.className = "rs-insights-dialog";
  dialog.setAttribute("aria-label", `${record.app || "배포"} 운영 인사이트`);
  const close = document.createElement("button");
  close.textContent = "닫기";
  close.className = "rs-close";
  close.setAttribute("aria-label", "운영 인사이트 닫기");
  dialog.append(style, close, content);
  document.body.append(dialog);
  dialog.showModal();
  let stopped = false,
    timer,
    controller,
    minutes = 15,
    epoch = 0;
  const dispose = () => {
    stopped = true;
    epoch++;
    clearTimeout(timer);
    controller?.abort();
    dialog.remove();
  };
  dialog.addEventListener("close", dispose, { once: true });
  close.onclick = () => dialog.close();
  async function refresh(next = minutes) {
    minutes = next;
    const seq = ++epoch;
    clearTimeout(timer);
    controller?.abort();
    controller = new AbortController();
    try {
      const { data } = await request(
        `/api/v1/deployments/${encodeURIComponent(record.id)}/insights?minutes=${minutes}`,
        {},
        controller,
      );
      if (stopped || seq !== epoch) return;
      if (data.deployment_id !== record.id)
        throw new Error("배포 정보가 일치하지 않습니다.");
      renderInsights(content, data, { refresh, minutes });
    } catch (error) {
      if (stopped || seq !== epoch) return;
      content.replaceChildren();
      const p = document.createElement("p");
      p.textContent = `운영 정보 조회 실패: ${error.message}`;
      content.append(p);
    }
    if (!stopped && seq === epoch) timer = setTimeout(() => refresh(), 30000);
  }
  refresh();
  return dispose;
}
