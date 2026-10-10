const settle = async (work) => {
  try {
    return await work();
  } catch {
    return null;
  }
};
const cut = (text) =>
  new TextDecoder().decode(Buffer.from(String(text || "")).subarray(0, 2000), {
    stream: true,
  });

// Projection only: use existing session-authorized readers, never start or retry work.
export function createInsightsService(products, observeTraffic) {
  return {
    async overview(id, session, minutes = 15) {
      const record = await products.getDeployment(id, session); // Authorize before external reads.
      const [events, traffic] = await Promise.all([
        settle(() => products.getDeploymentEvents(id, session)),
        observeTraffic(record, minutes),
      ]);
      const bound = events?.deployment_id === id;
      return {
        schema_version: 1,
        deployment_id: id,
        app: record.app,
        target_id: record.target_id,
        checked_at: new Date().toISOString(),
        refresh_after_ms: 30000,
        deployment: {
          status: record.status,
          stage: record.stage,
          created_at: record.created_at,
          source_commit: record.source_commit,
          images: record.ci?.images || {},
          ci_state: record.ci?.state || "unknown",
          cd_state: record.cd?.state || "unknown",
          revision: record.cd?.revision || null,
          public_http: record.public_http || null,
          url: record.url || null,
        },
        agent_activity: bound ? events.agent_activity || null : null,
        activity_state: bound ? events.state || "unknown" : "unavailable",
        observation: record.observation || null,
        traffic,
        interpretation: [
          "트래픽은 해당 앱·대상의 시간 구간 집계이며 순 방문자 수가 아닙니다.",
          "배포 기록과 현재 운영 상태는 별개입니다. 수집되지 않은 값은 정상으로 판단하지 마세요.",
          "배포 시점과 지표 변화의 일치만으로 원인을 확정하지 마세요.",
        ],
      };
    },
    async evidence(id, session, area) {
      const record = await products.getDeployment(id, session);
      if (area === "build") {
        const diagnostic = await products.getDeploymentDiagnostics(id, session);
        return {
          deployment_id: id,
          area,
          state: diagnostic.state,
          failure: diagnostic.failure
            ? {
                layer: diagnostic.failure.layer,
                code: diagnostic.failure.code,
                excerpt: cut(diagnostic.failure.excerpt),
              }
            : null,
          logs: (diagnostic.logs || []).slice(0, 2).map((log) => ({
            process_id: log.process_id,
            text: cut(log.text),
          })),
          bounded: true,
        };
      }
      if (area === "runtime")
        return { ...(await products.getDeploymentLogs(id, session)), area };
      return {
        deployment_id: id,
        area: "deploy",
        cd: record.cd || null,
        public_http: record.public_http || null,
        error: record.error || null,
      };
    },
  };
}
