import { openInsights } from './insights.js';
import { node, button, renderRecovery, validateQuestion, createAgentActivityCard } from './recovery.js';

export const time = (value) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ko-KR') : '시각 미제공';
const states = { succeeded: ['배포 완료', 'ok'], published: ['이미지 게시 완료', 'ok'], running: ['배포 중', 'run'], queued: ['실행 대기', 'run'],
  failed: ['배포 실패', 'fail'], blocked: ['조치 필요', 'wait'], unknown: ['결과 확인 필요', 'wait'], awaiting_input: ['응답 대기', 'wait'],
  preview: ['변경 검토 대기', 'wait'], expired: ['변경 검토 만료', 'wait'], unchanged: ['변경 없음', 'ok'] };
export function stateBadge(record) {
  const [label, tone] = states[record.status] || ['상태 확인 필요', 'wait'];
  return node('span', record.kind === 'builds' && record.status === 'succeeded' ? '이미지 게시 완료' : label, `dh-status ${tone}`);
}
export function filterHistory(records, { search = '', status = '', days = '', now = Date.now() } = {}) {
  const query = search.trim().toLocaleLowerCase();
  return records.filter((record) => (!query || (record.app || '').toLocaleLowerCase().includes(query))
    && (!status || record.status === status)
    && (!days || (Date.parse(record.created_at) >= now - Number(days) * 86400000 && Date.parse(record.created_at) <= now)));
}
export function relatedRecords(record, records) {
  return records.filter((item) => item.kind !== 'builds' && (record.application_id
    ? item.application_id === record.application_id
    : item.app === record.app && item.target_id === record.target_id));
}
export function hasDeploymentIssue(record) {
  if (['succeeded', 'published', 'unchanged'].includes(record.status)) return false;
  return Boolean(record.error) || ['failed', 'blocked', 'unknown', 'awaiting_input', 'publication_unverified'].includes(record.status)
    || [record.ci?.state, record.environment?.status, record.cd?.state].some((state) => ['failed', 'blocked', 'unknown'].includes(state));
}
// An agent can be repairing a running build, or have repaired a now-successful deployment.
export function hasProcessingDetail(record) {
  return hasDeploymentIssue(record) || Boolean(record.agent_activity_summary || record.agent_activity)
    || (record.kind !== 'builds' && record.status === 'running' && record.stage === 'ci')
    || Boolean(record.telemetry?.items?.some(event => event.event_name?.startsWith('agent.')));
}
export function deploymentStages(record) {
  const describe = (id, label, value) => ({ id, label, state: value || 'not_started' });
  const failure = ['failed', 'blocked', 'unknown'].includes(record.status) ? record.status : undefined;
  const ci = record.ci?.state || (record.kind === 'builds' ? record.status : record.stage === 'ci' ? failure : undefined);
  const stages = [describe('build', '빌드', ci === 'published' ? 'succeeded' : ci),
    describe('environment', '배포환경 준비', record.environment?.status || (record.stage === 'environment' ? failure : undefined)),
    describe('deploy', '배포', record.cd?.state === 'deployed' ? 'succeeded' : record.cd?.state || (record.stage === 'cd' ? failure : undefined))];
  if (record.kind === 'builds') return stages.slice(0, 1);
  return stages;
}
const stageLabel = (state) => ({ succeeded: '성공', failed: '실패', blocked: '조치 필요', running: '진행 중', progressing: '진행 중',
  queued: '대기', unknown: '결과 확인 필요', not_started: '기록 없음', awaiting_input: '응답 대기' }[state] || '결과 확인 필요');
const tone = (state) => state === 'succeeded' ? 'ok' : state === 'failed' ? 'fail' : ['running', 'progressing'].includes(state) ? 'run' : 'wait';
function imageText(record) {
  const images = record.ci?.images;
  if (!images || typeof images !== 'object') return '이미지 정보 미제공';
  const values = Object.entries(images).map(([name, value]) => `${name}: ${typeof value === 'string' ? value : value?.digest || '미제공'}`);
  return values.join('\n') || '이미지 정보 미제공';
}
function logText(record, diagnostic, stage) {
  if (stage === 'build') {
    const logs = diagnostic?.state === 'ready' ? diagnostic.logs : null;
    if (Array.isArray(logs) && logs.length) return logs.map((log) => log.text).filter((value) => typeof value === 'string').join('\n\n');
    if (diagnostic?.state === 'ready' && diagnostic.failure?.excerpt) return diagnostic.failure.excerpt;
    return JSON.stringify(record.ci?.steps || [], null, 2) === '[]' ? '아직 수집된 빌드 로그가 없습니다.' : JSON.stringify(record.ci.steps, null, 2);
  }
  const events = record.telemetry?.items?.filter((event) => stage === 'deploy' ? ['cd', 'controller', 'http', 'gitops'].includes(event.phase) : event.phase === 'environment');
  if (events?.length) return events.map((event) => `${time(event.occurred_at)}  ${event.event_name} · ${event.outcome}`).join('\n');
  return '이 단계의 상세 로그가 아직 제공되지 않았습니다. 로그 전체 보기에서 수집 상태를 확인하세요.';
}

export function createHistoryDetail({ host, request, getRecords, getApplications, serviceUrl, onLogs, onMonitor, downloads,
  recovery = { load: null, submit: null }, preview = false, onVisibility = () => {} }) {
  let generation = 0, controller, cleanup = () => {}, selectedStage = null, currentRecord = null;
  const submissions = new Map();
  const submissionKey = (value) => JSON.stringify([value.deployment_id, value.question_id || value.id, value.revision]);
  async function submitOnce(answer) {
    const key = submissionKey(answer);
    if (submissions.has(key)) throw new Error('이미 제출한 응답입니다. 상태를 다시 확인해 주세요.');
    submissions.set(key, 'pending');
    try {
      const result = await recovery.submit(answer);
      if (preview && result?.status === 'preview') submissions.delete(key);
      else submissions.set(key, result?.status === 'accepted' && result.question_id === answer.question_id && result.revision === answer.revision ? 'accepted' : 'unknown');
      return result;
    } catch (error) { submissions.set(key, 'unknown'); throw error; }
  }
  const close = () => { generation++; controller?.abort(); cleanup(); host.replaceChildren(); host.hidden = true; onVisibility(false); };
  function draw(record) {
    cleanup(); host.replaceChildren(); host.hidden = false; currentRecord = record;
    const application = getApplications().find((item) => item.id === record.application_id);
    const back = button('‹ 배포 내역', close, 'dh-back');
    const header = node('header', '', 'dh-head'), title = node('div', '', 'dh-title-row'), h1 = node('h2', record.app || '배포 상세');
    h1.tabIndex = -1; title.append(h1, stateBadge(record));
    const metadata = node('div', '', 'dh-meta');
    for (const [label, value] of [['배포일시', time(record.created_at)], ['소스', record.source_commit?.slice(0, 12)], ['배포환경', record.environment_target_id || record.target_id]]) {
      if (value) { const chip = node('span', '', 'dh-chip'); chip.append(node('span', label), node('b', value)); metadata.append(chip); }
    }
    const heading = node('div'); heading.append(title, metadata);
    let disposeInsights = () => {};
    const actions = node('div', '', 'dh-actions');
    if (record.kind !== 'builds') actions.append(button('운영 인사이트', () => { disposeInsights(); disposeInsights = openInsights(record, request); })); actions.append(button('모니터링', () => onMonitor(record)), button('작업 로그', () => onLogs(record)), button('새로고침', () => open(record)));
    const url = serviceUrl(application);
    if (url) { const link = node('a', '서비스 접속 ↗', 'primary-button'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; actions.append(link); }
    header.append(heading, actions); host.append(back, header);
    const viewport = node('div', '', 'dh-detail-viewport'); viewport.dataset.pane = 'history';
    const overview = node('section', '', 'dh-history-pane'), issuePane = node('section', '', 'dh-issue-pane');
    overview.setAttribute('aria-label', '접속정보 및 배포내역'); issuePane.setAttribute('aria-label', '선택한 배포의 오류 상세');
    issuePane.inert = true; issuePane.setAttribute('aria-hidden', 'true');
    viewport.append(overview, issuePane); host.append(viewport);
    let issueSequence = 0, issueController, disposePipeline = () => {}, originButton = null, issueRecordId = null;
    const epoch = generation;
    function switchPane(show, event) {
      viewport.classList.toggle('dh-instant', event?.detail === 0);
      viewport.dataset.pane = show ? 'issue' : 'history';
      overview.inert = show; overview.setAttribute('aria-hidden', String(show));
      issuePane.inert = !show; issuePane.setAttribute('aria-hidden', String(!show));
    }
    function hideIssue(event) {
      issueSequence++; issueController?.abort(); disposePipeline();
      switchPane(false, event); originButton?.focus({ preventScroll: true });
    }
    async function showIssue(item, event) {
      if (!hasProcessingDetail(item)) return;
      const sequence = ++issueSequence; issueController?.abort(); disposePipeline(); issueController = new AbortController();
      if (issueRecordId !== item.id) selectedStage = null;
      issueRecordId = item.id;
      originButton = [...body.querySelectorAll('.dh-issue-trigger')].find((trigger) => trigger.dataset.deploymentId === item.id);
      const backToHistory = button('‹ 배포내역으로 돌아가기', hideIssue, 'dh-back');
      const issueHeader = node('div', '', 'dh-issue-heading');
      issueHeader.append(node('h3', '문제 및 처리 내역'), node('p', `${time(item.created_at)} · ${item.id}`, 'dh-note'));
      const content = node('div');
      issuePane.replaceChildren(backToHistory, issueHeader, content);
      switchPane(true, event); backToHistory.focus({ preventScroll: true }); viewport.scrollIntoView({ block: 'nearest' });
      try {
        content.append(node('p', '오류 상세를 불러오고 있습니다.', 'dh-note'));
        const result = await readDetail(item, issueController);
        if (sequence !== issueSequence || generation !== epoch) return;
        content.replaceChildren(); const [selected, evidence, followup, warnings] = result;
        for (const warning of warnings) content.append(node('p', warning, 'dh-note'));
        if (!hasProcessingDetail(selected)) { content.append(node('p', '이 배포에는 현재 확인된 오류가 없습니다. 배포내역을 새로고침해 주세요.', 'dh-note')); return; }
        const pipeline = renderPipeline(selected, evidence, followup, () => showIssue(selected));
        disposePipeline = pipeline.dispose; content.append(pipeline.layout);
      } catch (error) {
        if (sequence !== issueSequence || generation !== epoch) return;
        content.replaceChildren(node('p', `오류 상세 조회 실패: ${error.message}`, 'dh-error'), button('다시 시도', () => showIssue(item)));
      }
    }
    cleanup = () => { disposeInsights(); issueSequence++; issueController?.abort(); disposePipeline(); };
    const connections = node('section', '', 'dh-panel'); connections.append(node('h3', '접속정보'));
    const grid = node('div', '', 'dh-connections');
    for (const [label, value] of [['서비스 주소', url || '현재 검증된 서비스 주소가 없습니다.'], ['모니터링', '환경 모니터링에서 확인'], ['배포환경', record.environment_target_id || record.target_id || '미제공']]) {
      const cell = node('div'); cell.append(node('span', label, 'dh-label'));
      if (label === '모니터링') cell.append(button(value, () => onMonitor(record), 'text-button'));
      else if (label === '서비스 주소' && url) { const link = node('a', value); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; cell.append(link); }
      else cell.append(node('strong', value)); grid.append(cell);
    }
    connections.append(grid);
    const historyPanel = node('section', '', 'dh-panel'); historyPanel.append(node('h3', '배포내역'));
    const records = relatedRecords(record, getRecords()).map((item) => item.id === record.id ? record : item);
    if (!records.some((item) => item.id === record.id)) records.unshift(record);
    const tableWrap = node('div', '', 'dh-table-wrap'), table = node('table'), head = node('thead'), headRow = node('tr'), body = node('tbody');
    for (const title of ['배포일시', '적용 이미지', '배포상태']) headRow.append(node('th', title)); head.append(headRow);
    records.sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
    for (const item of records) {
      const row = node('tr'), date = node('td'), image = node('td'), status = node('td');
      date.append(node('span', time(item.created_at)));
      if (application?.current_deployment_state === 'verified' && application.current_deployment?.id === item.id) { row.className = 'dh-current'; date.append(node('span', '현재', 'dh-tag')); }
      image.append(node('code', imageText(item)));
      if (hasProcessingDetail(item)) {
        row.classList.add('dh-issue-row');
        const trigger = button('', (event) => { event.stopPropagation(); showIssue(item, event); }, 'dh-issue-trigger');
        trigger.dataset.deploymentId = item.id;
        trigger.setAttribute('aria-label', `${time(item.created_at)} ${item.app || record.app} 처리 내역 보기`);
        const arrow = node('span', '→', 'dh-row-arrow'); arrow.setAttribute('aria-hidden', 'true');
        trigger.append(stateBadge(item));
        if (item.agent_activity_summary) trigger.append(node('span', `자동 처리 ${item.agent_activity_summary.attempt}회`, 'dh-tag'));
        trigger.append(arrow); status.append(trigger);
        row.addEventListener('click', (event) => showIssue(item, event));
      } else status.append(stateBadge(item));
      row.append(date, image, status); body.append(row);
    }
    table.append(head, body); tableWrap.append(table); historyPanel.append(tableWrap, node('p', '현재 불러온 페이지의 같은 앱·배포환경 기록입니다. 다른 기록은 목록의 이전·다음 페이지에서 확인하세요.', 'dh-table-note'));
    overview.append(connections, historyPanel);
    h1.focus();
  }
  function renderPipeline(record, diagnostic, question, refresh) {
    const stages = deploymentStages(record);
    if (!stages.some((stage) => stage.id === selectedStage)) selectedStage = null;
    selectedStage ||= (record.agent_activity ? 'build' : null) || stages.find((stage) => ['failed', 'blocked', 'unknown'].includes(stage.state))?.id || (question ? 'deploy' : stages.at(-1).id);
    const logLayout = node('div', '', 'dh-log-layout'), stepList = node('div', '', 'dh-steps'), detail = node('section', '', 'dh-stage-detail');
    stepList.setAttribute('role', 'group'); stepList.setAttribute('aria-label', '배포 단계');
    let disposeForm = () => {}, activity = record.agent_activity || null, card = null, stopped = false, timer, polling = false;
    const activityController = new AbortController();
    let activityRevision = activity?.revision ?? -1, activityId = activity?.id, runAttempt = record.agent_run_attempt || 0;
    const terminal = () => record.agent_events_complete === true;
    async function poll() {
      if (stopped || polling) return;
      polling = true;
      try {
        if (document.hidden) return;
        const { data } = await request(`/api/v1/deployments/${encodeURIComponent(record.id)}/events`, {}, activityController);
        if (stopped) return;
        if (data?.deployment_id !== record.id) throw new Error('배포 식별자 불일치');
        if (data.run_attempt && data.run_attempt < runAttempt) return;
        if (data.run_attempt > runAttempt) {
          activity = null; activityId = null; activityRevision = -1; card?.update(null);
        }
        runAttempt = data.run_attempt || runAttempt;
        const next = data.agent_activity;
        if (next && (next.id !== activityId || next.revision >= activityRevision)) {
          activity = next; activityId = next.id; activityRevision = next.revision;
          card?.update(activity, data.state === 'unavailable');
        } else if (activity) card?.update(activity, data.state === 'unavailable');
        record.agent_events_complete = data.status === 'completed';
        record.agent_poll_ms = Math.max(5000, data.progress?.poll_after_ms || 15000);
      } catch {
        if (!stopped) card?.update(activity, true);
      } finally {
        polling = false;
        if (!stopped && !terminal()) timer = setTimeout(poll, record.agent_poll_ms || 15000);
      }
    }
    function select(stage, focus = false) {
      disposeForm(); card = null; selectedStage = stage.id;
      for (const item of stepList.querySelectorAll('button[data-stage]')) item.setAttribute('aria-pressed', String(item.dataset.stage === stage.id));
      detail.replaceChildren(); const heading = node('h3', `${stage.label} · ${stageLabel(stage.state)}`); heading.tabIndex = -1; detail.append(heading);
      const failureStage = record.stage === 'ci' ? 'build' : record.stage === 'environment' ? 'environment' : 'deploy';
      if (!question && stage.id === failureStage && record.error?.message) { detail.append(node('h4', '원인'), node('p', record.error.message, 'dh-cause')); }
      // The optional question adapter binds the proposed form to one deployment and stage.
      if (question && stage.id === (question.stage || failureStage)) {
        detail.append(node('h4', '원인 요약'), node('p', question.summary.replace(/\s+/g, ' '), 'dh-cause'), node('h4', '근거'));
        for (const evidence of question.evidence || []) { detail.append(node('p', evidence.label, 'dh-label'), node('pre', evidence.text, 'dh-code')); }
      }
      if (stage.id === 'build' && record.kind !== 'builds') {
        const activityHost = node('div'); detail.append(activityHost);
        card = createAgentActivityCard(activityHost); card.update(activity);
      }
      detail.append(node('h4', '로그'), node('pre', logText(record, diagnostic, stage.id), 'dh-code'));
      // Legacy producers have no repair receipt; retain their observed event list.
      if (stage.id === 'build' && !activity) {
        const observed = record.telemetry?.items?.filter(event => event.event_name?.startsWith('agent.')) || [];
        if (observed.length) {
          const disclosure = node('details', '', 'dh-agent'); disclosure.append(node('summary', '에이전트 활동 기록'));
          const list = node('ul');
          for (const event of observed) list.append(node('li', `${time(event.occurred_at)} · ${event.event_name} · ${event.outcome}`));
          disclosure.append(list); detail.append(disclosure);
        }
      }
      if (question && stage.id === (question.stage || failureStage)) {
        const formHost = node('div'); detail.append(formHost); disposeForm = renderRecovery(formHost, { question, deploymentId: record.id,
          submit: recovery.submit ? submitOnce : null, onRefresh: refresh, preview, submissionState: submissions.get(submissionKey(question)) });
      } else if (['failed', 'blocked', 'unknown'].includes(stage.state)) detail.append(node('p', '해결 방법은 준비되는 대로 표시됩니다.', 'dh-note'));
      detail.append(button('로그 전체 보기', () => onLogs(record), 'text-button'));
      if (downloads && record.kind !== 'builds') { const disclosure = node('details'); disclosure.append(node('summary', '배포 소스 다운로드'), downloads(record)); detail.append(disclosure); }
      if (focus) heading.focus({ preventScroll: true });
    }
    for (const stage of stages) {
      const step = button('', () => select(stage, true), 'dh-step'); step.dataset.stage = stage.id;
      const icon = node('span', stage.state === 'succeeded' ? '✓' : stage.state === 'failed' ? '×' : '·', `dh-dot ${tone(stage.state)}`);
      icon.setAttribute('aria-hidden', 'true'); const label = node('span'); label.append(node('strong', stage.label), node('small', stageLabel(stage.state))); step.append(icon, label); stepList.append(step);
    }
    select(stages.find((stage) => stage.id === selectedStage) || stages[0]);
    if (record.kind !== 'builds' && !terminal()) timer = setTimeout(poll, record.agent_poll_ms || 5000);
    logLayout.append(stepList, detail); return { layout: logLayout, dispose: () => {
      stopped = true; clearTimeout(timer); activityController.abort(); disposeForm();
    } };
  }

  async function readRecord(record, activeController) {
    const build = record.kind === 'builds';
    const { data } = await request(`/api/v1/${build ? 'builds' : 'deployments'}/${encodeURIComponent(record.id)}${build ? '' : '?view=record'}`, {}, activeController);
    if (data?.id !== record.id || typeof data.app !== 'string') throw new Error('요청한 배포와 상세 정보가 일치하지 않습니다.');
    return { ...data, kind: record.kind || 'deployments' };
  }
  async function readDetail(record, activeController) {
    const full = await readRecord(record, activeController), notes = [];
    let diagnostic = null, question = null;
    const extras = await Promise.allSettled([
      full.kind === 'deployments' && ['failed', 'blocked', 'unknown'].includes(full.status)
        ? request(`/api/v1/deployments/${encodeURIComponent(full.id)}/diagnostics`, {}, activeController) : Promise.resolve(null),
      recovery.load ? recovery.load(full, { signal: activeController.signal }) : Promise.resolve(null),
      full.kind === 'deployments' ? request(`/api/v1/deployments/${encodeURIComponent(full.id)}/events`, {}, activeController) : Promise.resolve(null),
    ]);
    if (extras[0].status === 'fulfilled') {
      const value = extras[0].value?.data;
      if (value?.deployment_id === full.id && value?.binding?.app === full.app && value?.binding?.target_id === full.target_id && value?.binding?.source_commit === full.source_commit
          && String(value?.binding?.run_id) === String(full.ci?.run_id)) diagnostic = value;
    } else notes.push('진단 자료를 조회하지 못했습니다. 새로고침으로 다시 확인해 주세요.');
    if (extras[1].status === 'fulfilled' && extras[1].value) {
      try { question = validateQuestion(extras[1].value, full.id); }
      catch (error) { notes.push(error.message); }
    }
    else if (extras[1].status === 'rejected') notes.push('해결 방법을 조회하지 못했습니다. 상태를 다시 확인해 주세요.');
    const events = extras[2].status === 'fulfilled' ? extras[2].value?.data : null;
    if (events?.deployment_id === full.id) {
      full.agent_activity = events.agent_activity || null;
      full.agent_events_complete = events.status === 'completed';
      full.agent_poll_ms = Math.max(5000, events.progress?.poll_after_ms || 15000);
      full.agent_run_attempt = events.run_attempt || 0;
      if (events.timeline) full.telemetry = events.timeline;
    }
    return [full, diagnostic, question, notes];
  }
  async function open(record) {
    const call = ++generation; controller?.abort(); cleanup(); controller = new AbortController();
    selectedStage = currentRecord?.id === record.id ? selectedStage : null;
    host.hidden = false; onVisibility(true); host.replaceChildren(node('p', '배포 상세를 불러오고 있습니다.', 'dh-note'));
    try {
      const result = await readRecord(record, controller);
      if (generation !== call) return;
      draw(result);
    } catch (error) {
      if (generation !== call) return;
      host.replaceChildren(button('‹ 배포 내역', close, 'dh-back'), node('p', `배포 상세 조회 실패: ${error.message}`, 'dh-error'), button('다시 시도', () => open(record)));
    }
  }
  return { open, close, dispose: close };
}
