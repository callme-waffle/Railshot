import test from 'node:test';
import assert from 'node:assert/strict';
import { agentActivity } from '../src/agent-activity.js';
import { ingestCiEvents, publicTelemetry } from '../src/telemetry.js';
const time = '2026-10-04T03:00:00Z';
const record = () => ({ id: 'deployment-1', source_commit: 'a'.repeat(40), ci: { run_id: '123', producer_attempt: 1 } });
const event = (sequence, name, attrs = {}, attempt = 1) => ({ sequence, occurred_at: time, ingested_at: new Date().toISOString(),
  event_name: name, phase: 'agent', outcome: 'RUNNING', correlation: { github_run_id: '123', github_run_attempt: 1, source_commit: 'a'.repeat(40) },
  attributes: { native_run_id: 'native', producer_sequence: sequence, attempt_id: `native:${attempt}`, ...attrs } });
const repair = state => ({ state, role: 'fixer', changes: [{ path: 'Dockerfile', summary: '시작 명령 수정', status: 'applied' }], omitted_changes: 0, failure_layer: 'L3' });

test('SDK completion and individual successful gates do not claim repair success', () => {
  const events = [event(1, 'agent.heartbeat', { process_running: true }), event(2, 'agent.observation', { process_running: false })];
  assert.equal(agentActivity(record(), { items: events }).state, 'unknown');
  events.push(event(3, 'agent.repair', { repair: repair('verifying') }));
  events.push({ ...event(4, 'gate.layer.completed'), phase: 'L2', outcome: 'PASS' });
  let activity = agentActivity(record(), { items: events });
  assert.equal(activity.state, 'verifying'); assert.equal(activity.verification[0].state, 'succeeded');
  events.push(event(5, 'agent.repair', { repair: repair('succeeded') }));
  activity = agentActivity(record(), { items: events });
  assert.equal(activity.state, 'succeeded'); assert.equal(activity.changes[0].path, 'Dockerfile');
  assert.equal(activity.finished_at, time);
});

test('producer order, retries, foreign runs and unavailable observations stay distinct', () => {
  const events = [event(3, 'agent.repair', { repair: repair('failed') }), event(1, 'agent.heartbeat', { process_running: true }),
    event(4, 'agent.heartbeat', { process_running: true }, 2), event(2, 'agent.repair', { repair: repair('verifying') })];
  const activity = agentActivity(record(), { items: events }, { state: 'unavailable' });
  assert.equal(activity.attempt, 2); assert.equal(activity.state, 'analyzing'); assert.equal(activity.observation.state, 'unavailable');
  assert.equal(activity.previous_attempts[0].state, 'failed');
  assert.equal(agentActivity({ ...record(), ci: { run_id: '456' } }, { items: events }), null);
  assert.equal(agentActivity({ ...record(), ci: { run_id: '123', producer_attempt: 2 } }, { items: events }), null);
});

test('bounded telemetry preserves repair receipts after noisy heartbeats and deduplicates delivery', () => {
  const r = record();
  const envelope = { run_id: '123', run_attempt: 1, checked_at: time, state: 'complete', items: [
    { sequence: 1, occurred_at: time, event_name: 'agent.repair', native_run_id: 'native', attempt_id: 'native:1', repair: repair('succeeded') },
    ...Array.from({ length: 250 }, (_, i) => ({ sequence: i + 2, occurred_at: time, event_name: 'agent.heartbeat', native_run_id: 'native', attempt_id: 'native:1', process_running: false }))] };
  ingestCiEvents(r, envelope);
  assert.equal(r.telemetry.items.length, 240);
  assert.equal(agentActivity(r, publicTelemetry(r)).state, 'succeeded');
  const last = r.telemetry.sequence;
  ingestCiEvents(r, { ...envelope, items: envelope.items.slice(-1) });
  assert.equal(r.telemetry.sequence, last);
});
