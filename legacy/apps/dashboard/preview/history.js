import { createHistoryDetail, filterHistory, stateBadge, time } from '../src/deployment-history.js';
import { node, button } from '../src/recovery.js';
// Isolated design fixtures; neither the production entry nor the API imports this file.
const timestamp = (hours) => new Date(Date.now() - hours * 3600000).toISOString();
const records = [['shop-api', 'succeeded'], ['blog-web', 'running'], ['chat-server', 'blocked'], ['auth-service', 'failed'], ['landing-page', 'succeeded'], ['batch-worker', 'succeeded']].map(([app, status], index) => ({
  id: `demo-${index}`, application_id: `application-${index}`, kind: 'deployments', app, status, stage: 'cd', target_id: 'AWS · ap-northeast-2', created_at: timestamp(index * 8),
  source_commit: 'a1b2c3d4e5f6', ci: { state: 'published', images: { [app]: 'sha256:4c5b6a7' }, steps: [{ name: '컨테이너 이미지 빌드', conclusion: 'success' }] },
  environment: { status: 'succeeded' }, cd: { state: status === 'succeeded' ? 'deployed' : status, deployed: status === 'succeeded' },
  ...(status === 'failed' ? { error: { message: '필수 배포 입력이 없어 컨테이너가 시작되지 않았습니다.' } } : {}),
  telemetry: { items: [{ event_name: 'agent.completed', phase: 'agent', outcome: 'PASS', occurred_at: timestamp(index * 8) },
    { event_name: 'controller.observed', phase: 'controller', outcome: status === 'failed' ? 'FAIL' : 'RUNNING', occurred_at: timestamp(index * 8) }] },
}));
// A completed repair stays inspectable even when deployment has succeeded.
const sampleActivity = record => ({ schema_version: 1, id: `repair-${record.id}`, revision: 2, stage: 'build',
  state: record.status === 'running' ? 'verifying' : 'succeeded', attempt: 1,
  started_at: new Date(Date.parse(record.created_at) - 58000).toISOString(), updated_at: record.created_at,
  finished_at: record.status === 'running' ? null : record.created_at, observation: { state: 'current' },
  summary: '컨테이너 시작 검사 실패를 처리했습니다. (예시)',
  current_action: record.status === 'running' ? '수정한 이미지의 실행 상태를 확인하고 있습니다.' : '필수 검사를 통과했습니다.',
  changes: [{ path: 'Dockerfile', summary: '빌드 결과물 경로에 맞게 시작 명령 변경', status: 'applied' }],
  verification: [{ key: 'image.build', label: '이미지 빌드', state: 'succeeded' },
    { key: 'image.runtime', label: '컨테이너 실행 검사', state: record.status === 'running' ? 'running' : 'succeeded' }],
  previous_attempts: [], question: null });
records[0].agent_activity_summary = { id: 'repair-demo-0', state: 'succeeded', attempt: 1 };
records[1].agent_activity_summary = { id: 'repair-demo-1', state: 'verifying', attempt: 1 };
const previous = { ...records[0], id: 'demo-old', created_at: timestamp(40), source_commit: '9f8e7d6c5b4a', ci: { state: 'published', images: { 'shop-api': 'sha256:9f8e7d6' } } };
const pastFailure = { ...records[0], id: 'demo-shop-failed', agent_activity_summary: null, status: 'failed', created_at: timestamp(24),
  source_commit: '4c5b6a7', cd: { state: 'failed', deployed: false }, error: { message: '이전 배포에서 필수 배포 입력이 누락되었습니다.' },
  telemetry: { items: [{ event_name: 'controller.observed', phase: 'controller', outcome: 'FAIL', occurred_at: timestamp(24) }] } };
const applications = records.map((record) => ({ id: record.application_id, current_deployment_state: record.status === 'succeeded' ? 'verified' : 'not_deployed', current_deployment: record.status === 'succeeded' ? record : null }));
export const sampleQuestion = (record) => ({
  id: `question-${record.id}`, revision: '1', deployment_id: record.id, stage: 'deploy', expires_at: new Date(Date.now() + 3600000).toISOString(),
  summary: '필수 배포 입력이 없어 앱이 종료된 것으로 보입니다.', prompt: '권장 해결방법 · 하나를 선택해 주세요',
  evidence: [{ label: '앱 시작 로그 · 예시', text: '[16:32:10] Readiness probe failed: connection refused :8080\n[16:32:55] Error: DATABASE_URL is not defined' }],
  options: [{ id: 'configure', label: '필수 배포 입력을 보완하고 다시 시도', description: '선택한 항목과 입력값을 함께 제출합니다.', fields: [
    { id: 'secret', type: 'text', label: '연결 문자열', required: true, sensitive: true },
    { id: 'mode', type: 'single_select', label: '재시도 방식', required: true, choices: [{ id: 'failed_step', label: '실패한 단계부터' }, { id: 'all', label: '전체 단계 다시 확인' }] },
    { id: 'checks', type: 'multi_select', label: '추가 확인 항목', required: true, choices: [{ id: 'network', label: '네트워크 연결' }, { id: 'health', label: '상태 확인 경로' }] },
    { id: 'notes', type: 'text_list', label: '전달할 사항', required: false },
  ] }, { id: 'pause', label: '작업을 중단하고 직접 확인', description: '현재 실패 기록을 유지합니다.', fields: [
    { id: 'reason', type: 'text', label: '전달할 내용', required: false },
  ] }],
});
const overview = document.querySelector('#preview-overview');
const detail = createHistoryDetail({ host: document.querySelector('#preview-detail'),
  request: async (path) => {
    path = new URL(path, location.origin).pathname;
    const record = [...records, previous, pastFailure].find(item => path.endsWith(`/${item.id}`) || path.endsWith(`/${item.id}/events`));
    if (path.endsWith('/events')) return { data: { deployment_id: record?.id,
      agent_activity: record?.agent_activity_summary ? sampleActivity(record) : null,
      status: record?.status === 'running' ? 'running' : 'completed' } };
    return { data: path.endsWith('/diagnostics') ? { state: 'unavailable' } : record };
  },
  getRecords: () => [...records, previous, pastFailure], getApplications: () => applications, serviceUrl: () => null,
  onLogs: () => { document.querySelector('.dh-code')?.scrollIntoView({ block: 'center' }); },
  onMonitor: () => { alert('미리보기에서는 모니터링 서비스에 연결하지 않습니다.'); },
  preview: true, recovery: { load: async (record) => ['failed', 'blocked'].includes(record.status) ? sampleQuestion(record) : null,
    submit: async (answer) => { await new Promise((resolve) => setTimeout(resolve, 250)); return { status: 'preview', question_id: answer.question_id, revision: answer.revision }; } },
  onVisibility: (visible) => { overview.hidden = visible; },
});
function render() {
  document.querySelector('#preview-cards').replaceChildren(...filterHistory(records, { search: document.querySelector('#preview-search').value }).map((record) => {
    const item = node('li', '', 'dh-card'), open = button('', () => detail.open(record), 'dh-card-open');
    const foot = node('span', '', 'dh-card-foot'); foot.append(stateBadge(record), node('span', '›', 'dh-arrow'));
    open.append(node('strong', record.app), node('small', time(record.created_at)), foot); item.append(open); return item;
  }));
}
render(); document.querySelector('#preview-search').addEventListener('input', render); document.querySelector('#preview-list').addEventListener('click', detail.close);
window.addEventListener('pagehide', detail.dispose);
