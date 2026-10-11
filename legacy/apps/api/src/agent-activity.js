// Presentation only: host observations decide state; this module never runs work.
const labels = { L0: '수정 정책 검사', L1: '배포 설정 검사', Q: '소스 품질 검사', L2: '이미지 빌드', L3: '컨테이너 실행 검사', L4: '이미지 보안 검사' };
const phases = { L0: 'package.policy', L1: 'package.spec', Q: 'source.quality', L2: 'image.build', L3: 'image.runtime', L4: 'image.scan' };
const outcomes = { PASS: 'succeeded', FAIL: 'failed', BLOCKED: 'failed', RUNNING: 'running', UNKNOWN: 'unknown', NOT_RUN: 'not_run', INCOMPLETE: 'unknown' };
const actions = { analyzing: '에이전트가 실행 중입니다. 처리 결과를 기다리고 있습니다.', verifying: '수정 후 공식 검사를 진행하고 있습니다.',
  succeeded: '필수 검사를 통과했습니다. 배포 상태는 별도로 확인하세요.', failed: '이번 시도의 자동 처리를 완료하지 못했습니다.', unknown: '처리 결과를 확인할 수 없습니다.' };

export function agentActivity(record, timeline, observation = {}) {
  const bound = (timeline?.items || []).filter(e => String(e.correlation?.github_run_id) === String(record.ci?.run_id)
    && e.correlation?.source_commit === record.source_commit && e.attributes?.native_run_id);
  const runAttempt = Math.max(observation.run_attempt || 0, record.ci?.producer_attempt || 0, ...bound.map(e => e.correlation?.github_run_attempt || 0));
  const events = bound.filter(e => e.correlation.github_run_attempt === runAttempt)
    .sort((a, b) => (a.attributes.producer_sequence ?? a.sequence) - (b.attributes.producer_sequence ?? b.sequence));
  const latestNative = events.at(-1)?.attributes.native_run_id;
  const current = events.filter(e => e.attributes.native_run_id === latestNative);
  const attempts = new Map();
  for (const event of current) {
    const a = event.attributes, isAgent = event.event_name.startsWith('agent.');
    // Attempt 0 is the deterministic baseline, not an AI repair.
    const suffix = a.attempt_id?.slice(latestNative.length + 1);
    if (!/^[1-9]\d*$/.test(suffix || '')) continue;
    const number = Number(suffix);
    if (isAgent && !attempts.has(number)) attempts.set(number, { attempt: number, state: 'analyzing', role: a.role || a.repair?.role,
      started_at: Number.isFinite(a.elapsed_ms) ? new Date(Date.parse(event.occurred_at) - a.elapsed_ms).toISOString() : null, updated_at: event.occurred_at, finished_at: null, changes: [], verification: [], failure_layer: null, omitted_changes: 0 });
    const attempt = attempts.get(number);
    if (!attempt) continue;
    attempt.updated_at = event.occurred_at;
    if (event.event_name === 'agent.repair') {
      Object.assign(attempt, a.repair);
      attempt.finished_at = ['succeeded', 'failed', 'unknown'].includes(attempt.state) ? event.occurred_at : null;
    } else if (isAgent && a.process_running === false && !['succeeded', 'failed', 'verifying'].includes(attempt.state)) {
      attempt.state = 'unknown';
    } else if (event.event_name.startsWith('gate.layer.') && labels[event.phase]) {
      const check = { key: phases[event.phase], label: labels[event.phase], state: outcomes[event.outcome] || 'unknown' };
      attempt.verification = [...attempt.verification.filter(v => v.key !== check.key), check];
      // A gate event is not sufficient to prove all required checks passed.
      if (!['succeeded', 'failed'].includes(attempt.state)) attempt.state = 'verifying';
    }
  }
  const history = [...attempts.values()].sort((a, b) => a.attempt - b.attempt);
  const latest = history.at(-1);
  if (!latest) return null;
  const loopEnd = current.findLast(e => e.event_name === 'loop.completed');
  if (loopEnd && !['succeeded', 'failed'].includes(latest.state)) {
    latest.state = ['FAIL', 'BLOCKED', 'INCOMPLETE'].includes(loopEnd.outcome) ? 'failed' : 'unknown';
    latest.finished_at = loopEnd.occurred_at;
  }
  const stale = observation.stale ?? timeline.stale;
  const unavailable = observation.state === 'unavailable' || (!observation.state && timeline.state === 'unavailable');
  return { schema_version: 1, id: `${record.id}:${runAttempt}:${latestNative}`, revision: Math.max(...current.map(e => e.attributes.producer_sequence ?? e.sequence)),
    stage: 'build', ...latest, summary: latest.failure_layer ? `${labels[latest.failure_layer] || '필수 검사'} 실패에 대한 자동 복구입니다.`
      : latest.role === 'adapter' ? '컨테이너 구성을 준비하고 있습니다.' : '관측된 실패에 대한 자동 복구입니다.',
    current_action: actions[latest.state], observation: { state: unavailable ? 'unavailable' : stale ? 'stale' : 'current',
      checked_at: observation.checked_at || timeline.checked_at || null },
    previous_attempts: history.slice(-4, -1), question: null,
    omitted: { changes: latest.omitted_changes, previous_attempts: Math.max(0, history.length - 4) } };
}
