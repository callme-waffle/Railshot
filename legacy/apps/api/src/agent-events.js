import { redactDiagnostic } from './diagnostics.js';
import { APP_NAME, TENANT_NAME, TARGET_ID, SOURCE_COMMIT } from './contract.js';

export const agentEventLimits = Object.freeze({ items: 60, textBytes: 60_000, responseBytes: 2 * 1024 * 1024, cacheMs: 15_000 });
export const agentEventCheckName = 'Railshot agent events';
export const progressItems = Object.freeze(['userMessage', 'hookPrompt', 'agentMessage', 'functionCallOutput', 'plan', 'reasoning',
  'commandExecution', 'fileChange', 'mcpToolCall', 'dynamicToolCall', 'collabAgentToolCall', 'subAgentActivity', 'webSearch',
  'imageView', 'sleep', 'imageGeneration', 'enteredReviewMode', 'exitedReviewMode', 'contextCompaction', 'other']);
export const progressStatuses = Object.freeze(['inProgress', 'completed', 'failed', 'declined', 'interrupted', 'unknown']);
export const progressTokens = Object.freeze(['input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens']);
const outcomes = ['RUNNING', 'PASS', 'FAIL', 'BLOCKED', 'UNKNOWN', 'NOT_RUN', 'INCOMPLETE'];
const safeId = /^[A-Za-z0-9_.:-]{1,128}$/;
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const record = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const exact = (value, required, optional = []) => record(value) && required.every((key) => Object.hasOwn(value, key))
  && Object.keys(value).every((key) => required.includes(key) || optional.includes(key));
const timestamp = (value) => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/.test(value) && Number.isFinite(Date.parse(value));
export class AgentEventError extends Error {
  constructor(reason) { super('Agent event observation unavailable'); this.reason = reason; }
}
const requireValid = (valid, reason = 'invalid_payload') => { if (!valid) throw new AgentEventError(reason); };

export function validateEventBinding(runId, binding) {
  requireValid(typeof runId === 'string' && /^[1-9]\d{0,15}$/.test(runId) && Number.isSafeInteger(Number(runId))
    && record(binding) && typeof binding.app === 'string' && APP_NAME.test(binding.app)
    && typeof binding.tenant === 'string' && TENANT_NAME.test(binding.tenant)
    && typeof binding.target_id === 'string' && TARGET_ID.test(binding.target_id)
    && typeof binding.source_commit === 'string' && SOURCE_COMMIT.test(binding.source_commit), 'binding_mismatch');
}

export function validateEventRun(run, { runId, source_commit, owner, repo, ref, workflow }) {
  requireValid(run?.id === Number(runId) && run.path === `.github/workflows/${workflow}`
    && run.head_sha === source_commit && run.head_branch === ref && run.event === 'workflow_dispatch'
    && run.repository?.full_name === `${owner}/${repo}`
    && run.html_url === `https://github.com/${owner}/${repo}/actions/runs/${runId}`
    && Number.isInteger(run.run_attempt) && run.run_attempt >= 1 && run.run_attempt <= 100
    && ['queued', 'in_progress', 'completed', 'waiting', 'pending', 'requested'].includes(run.status), 'binding_mismatch');
  return run.run_attempt;
}

export function validateProgress(value) {
  requireValid(exact(value, ['elapsed_ms', 'sdk_event_count', 'last_sdk_event_at_ms', 'item_counts'], ['last_item', 'token_usage'])
    && integer(value.elapsed_ms) && integer(value.sdk_event_count) && integer(value.last_sdk_event_at_ms));
  for (const [key, allowed] of [['item_counts', progressItems], ['token_usage', progressTokens]]) {
    if (!Object.hasOwn(value, key)) continue;
    requireValid(record(value[key]) && Object.entries(value[key]).every(([name, count]) => allowed.includes(name) && integer(count)));
  }
  if (Object.hasOwn(value, 'last_item')) requireValid(exact(value.last_item, ['kind', 'status'])
    && progressItems.includes(value.last_item.kind) && progressStatuses.includes(value.last_item.status));
}

function validateItem(item) {
  const common = ['sequence', 'occurred_at', 'event_name', 'native_run_id'];
  requireValid(record(item) && integer(item.sequence) && item.sequence > 0 && timestamp(item.occurred_at)
    && typeof item.native_run_id === 'string' && safeId.test(item.native_run_id));
  if (['loop.started', 'loop.completed'].includes(item.event_name)) {
    requireValid(exact(item, [...common, 'phase', 'outcome', 'sdk_invocations'], ['agent_budget']) && item.phase === 'loop'
      && outcomes.includes(item.outcome) && (item.sdk_invocations === null || integer(item.sdk_invocations)));
    if (Object.hasOwn(item, 'agent_budget')) requireValid(exact(item.agent_budget, ['enabled', 'max_invocations'])
      && Number.isInteger(item.agent_budget.max_invocations) && item.agent_budget.max_invocations >= 0 && item.agent_budget.max_invocations <= 16
      && item.agent_budget.enabled === (item.agent_budget.max_invocations > 0));
  } else if (item.event_name === 'agent.repair') {
    const repair = item.repair;
    requireValid(exact(item, [...common, 'attempt_id', 'repair']) && typeof item.attempt_id === 'string' && safeId.test(item.attempt_id)
      && exact(repair, ['state', 'role', 'changes', 'omitted_changes', 'failure_layer'])
      && ['verifying', 'succeeded', 'failed', 'unknown'].includes(repair.state)
      && ['adapter', 'fixer'].includes(repair.role) && [null, 'L0', 'L1', 'Q', 'L2', 'L3', 'L4'].includes(repair.failure_layer)
      && integer(repair.omitted_changes) && Array.isArray(repair.changes) && repair.changes.length <= 24);
    for (const change of repair.changes) {
      requireValid(exact(change, ['path', 'summary', 'status']) && typeof change.path === 'string' && change.path.length <= 240
        && /^[A-Za-z0-9_./@-]+$/.test(change.path) && !change.path.startsWith('/')
        && !change.path.split('/').some(part => ['', '.', '..'].includes(part))
        && redactDiagnostic(change.path) === change.path && change.status === 'applied'
        && typeof change.summary === 'string' && change.summary.length > 0 && change.summary.length <= 300);
      change.summary = redactDiagnostic(change.summary);
    }
  } else if (['gate.layer.started', 'gate.layer.completed', 'gate.layer.heartbeat'].includes(item.event_name)) {
    requireValid(exact(item, [...common, 'attempt_id', 'phase', 'outcome', 'completed_steps', 'total_steps', 'duration_s'])
      && typeof item.attempt_id === 'string' && safeId.test(item.attempt_id)
      && ['L0', 'L1', 'Q', 'L2', 'L4', 'L3'].includes(item.phase) && outcomes.includes(item.outcome)
      && integer(item.completed_steps) && integer(item.total_steps) && item.total_steps >= 1 && item.total_steps <= 6
      && item.completed_steps <= item.total_steps && Number.isFinite(item.duration_s) && item.duration_s >= 0 && item.duration_s <= 86400);
  } else {
    requireValid(['agent.heartbeat', 'agent.observation'].includes(item.event_name)
      && exact(item, [...common, 'attempt_id', 'role', 'provider', 'elapsed_ms', 'process_running', 'snapshot_state',
        'sdk_activity_since_previous', 'last_sdk_event_age_ms'], ['progress'])
      && typeof item.attempt_id === 'string' && safeId.test(item.attempt_id)
      && ['adapter', 'fixer'].includes(item.role) && ['codex', 'claude'].includes(item.provider)
      && integer(item.elapsed_ms) && typeof item.process_running === 'boolean'
      && ['current', 'unavailable'].includes(item.snapshot_state) && typeof item.sdk_activity_since_previous === 'boolean'
      && (item.last_sdk_event_age_ms === null || integer(item.last_sdk_event_age_ms)));
    if (Object.hasOwn(item, 'progress')) validateProgress(item.progress);
  }
}

// A projection for dashboard readers, never a scheduler or an inferred model-call count.
// Older producers have no budget. A heartbeat alone cannot prove an SDK invocation.
export function summarizeAgentEvents(envelope, timeline = { items: [] }) {
  const items = envelope.items || [], latest = items.at(-1) || null;
  const prior = (timeline.items || []).filter((event) => String(event.correlation?.github_run_id) === String(envelope.run_id)
    && event.correlation?.github_run_attempt === envelope.run_attempt);
  const budget = items.findLast((event) => event.agent_budget)?.agent_budget
    || prior.findLast((event) => event.attributes?.agent_budget)?.attributes.agent_budget || null;
  let count = null;
  for (const event of items) {
    if (event.event_name.startsWith('loop.')) count = event.sdk_invocations;
    else if (event.event_name.startsWith('agent.')) count = null;
  }
  return { poll_after_ms: agentEventLimits.cacheMs, stale_after_seconds: 60,
    latest: latest ? structuredClone(latest) : null, agent_budget: budget ? structuredClone(budget) : null,
    sdk_invocations: Number.isSafeInteger(count) ? count : null };
}

export function readAgentEventCheck(check, binding) {
  const { runId, attempt, source_commit, owner, repo } = binding;
  requireValid(check?.name === agentEventCheckName && check.external_id === `railshot-events:${runId}:${attempt}`
    && check.head_sha === source_commit
    // GitHub Actions replaces details_url with this check's native URL.
    && [`https://github.com/${owner}/${repo}/actions/runs/${runId}`, `https://github.com/${owner}/${repo}/runs/${check.id}`].includes(check.details_url)
    && check.app?.slug === 'github-actions' && check.app?.id === 15368 && Number.isSafeInteger(check.id) && check.id > 0
    && ['in_progress', 'completed'].includes(check.status)
    && (check.status === 'completed' ? check.conclusion === 'neutral' : check.conclusion === null), 'producer_mismatch');
  const text = check.output?.text;
  requireValid(typeof text === 'string' && Buffer.byteLength(text, 'utf8') < agentEventLimits.textBytes, 'too_large');
  let value;
  try { value = JSON.parse(text); } catch { throw new AgentEventError('invalid_payload'); }
  requireValid(exact(value, ['version', 'run_id', 'run_attempt', 'source_commit', 'app', 'tenant', 'target_id', 'updated_at', 'status', 'truncated', 'items'])
    && value.version === 1 && value.run_id === runId && value.run_attempt === attempt && value.source_commit === source_commit
    && value.app === binding.app && value.tenant === binding.tenant && value.target_id === binding.target_id, 'binding_mismatch');
  requireValid(timestamp(value.updated_at) && Date.parse(value.updated_at) <= Date.now() + 60_000
    && ['running', 'completed'].includes(value.status) && typeof value.truncated === 'boolean'
    && Array.isArray(value.items) && value.items.length <= agentEventLimits.items
    && (value.status === 'completed') === (check.status === 'completed'));
  let sequence = 0;
  const nativeRunId = value.items[0]?.native_run_id;
  for (const item of value.items) {
    validateItem(item);
    requireValid(item.sequence > sequence && Date.parse(item.occurred_at) <= Date.parse(value.updated_at)
      && item.native_run_id === nativeRunId && (!item.attempt_id || item.attempt_id.startsWith(`${nativeRunId}:`)));
    sequence = item.sequence;
  }
  // Only allowlisted metadata and bounded, redacted repair descriptions leave this module.
  return structuredClone(value);
}

export function emptyAgentEvents(binding, state, reason) {
  return { version: 1, state, reason, checked_at: new Date().toISOString(), updated_at: null, stale: false,
    run_id: binding.runId || null, run_attempt: binding.attempt || null, source_commit: binding.source_commit || null,
    app: binding.app, tenant: binding.tenant || null, target_id: binding.target_id,
    status: null, truncated: false, items: [], next_marker: null };
}
