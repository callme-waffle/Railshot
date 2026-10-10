import { readFileSync } from 'node:fs';
import { registerAppResource, registerAppTool, RESOURCE_MIME_TYPE } from '@modelcontextprotocol/ext-apps/server';
import { McpServer } from '@modelcontextprotocol/server';
import { createToolRunner, tools } from './tools.js';

export function createToolServer(api, { publicOrigin = process.env.RAILSHOT_PUBLIC_ORIGIN || 'https://railshot.io', localTools = {} } = {}) {
  const catalog = { ...tools, ...localTools };
  const sourceInstructions = localTools.deploy_local_project
    ? '공개 GitHub 저장소는 deploy_repository, 사용자 컴퓨터의 폴더·ZIP은 deploy_local_project로 배포합니다. 사용자에게서 명시된 경로만 사용하고 배포 요청은 사용자 지시를 확인한 뒤 실행하세요. 로컬 배포 후 조회도 이 MCP의 도구를 사용하세요. 원격 OAuth MCP의 세션과는 별개입니다. '
    : '배포 소스는 공개 GitHub 저장소 URL만 지원합니다. 로컬 폴더·파일·ZIP 업로드 배포는 지원하지 않습니다. ';
  const server = new McpServer({ name: 'railshot-agent-tools', version: '0.1.0' }, {
    instructions: sourceInstructions + '배포 접수 후 get_deployment_progress를 호출하고 poll_after_ms 뒤 activity_cursor를 since로 전달해 반복 조회하세요. deployment_updated이면 배포 단계·상태를, agent_activity_updated이면 AI 작업 요약·현재 작업·변경 파일·검증 결과를 사용자에게 알리세요. run_attempt_changed이면 이전 시도의 결과로 설명하지 마세요. events_state가 unavailable 또는 stale_response이면 최신 AI 작업을 확인했다고 주장하지 마세요. 배포 실패 시 검증된 원인과 CI 로그를 설명하되 agent_activity.state=succeeded는 배포 성공이 아닙니다. MCP 서버는 대화창에 스스로 메시지를 보내지 못하므로 자동 모니터링은 MCP 클라이언트가 반복 호출해야 합니다.',
  });
  const uri = 'ui://railshot/insights.html';
  registerAppResource(server, 'Railshot 운영 인사이트', uri, {}, async () => ({ contents: [{
    uri, mimeType: RESOURCE_MIME_TYPE, text: readFileSync(new URL('./insights-app.html', import.meta.url), 'utf8'),
    _meta: { ui: { csp: { connectDomains: [], resourceDomains: [] } } },
  }] }));
  const call = createToolRunner(api, catalog);
  for (const [name, tool] of Object.entries(catalog)) {
    const register = name === 'get_app_overview' ? (name, config, callback) => registerAppTool(server, name,
      { ...config, _meta: { ui: { resourceUri: uri } } }, callback) : server.registerTool.bind(server);
    register(name, {
      title: tool.title, description: tool.description, inputSchema: tool.schema,
      annotations: { readOnlyHint: tool.readOnly, destructiveHint: false, idempotentHint: tool.readOnly, openWorldHint: false },
    }, async (args) => {
      try {
        const value = await call(name, args);
        if (name === 'get_app_overview') {
          const origin = new URL(publicOrigin).origin;
          const summary = { ...value, traffic: { ...value.traffic, series: undefined },
            dashboard_url: `${origin}/?insights=${encodeURIComponent(value.deployment_id)}` };
          return { content: [{ type: 'text', text: JSON.stringify(summary) }], structuredContent: summary, _meta: { overview: value } };
        }
        return { content: [{ type: 'text', text: JSON.stringify(value) }], structuredContent: value };
      } catch (error) {
        const value = { code: error.code || 'TOOL_ERROR',
          message: error.name === 'ZodError' ? '도구 입력이 잘못되었습니다.' : error.message,
          outcome_unknown: error.outcomeUnknown === true };
        return { content: [{ type: 'text', text: JSON.stringify(value) }], structuredContent: value, isError: true };
      }
    });
  }
  return server;
}
