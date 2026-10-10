import { classificationInput } from './classifier.js';
import { appendEvent, telemetryContract } from './telemetry.js';

const error = (code, status = 409) => Object.assign(new Error('진단 자료의 실행 식별자와 무결성을 확인해야 합니다.'), { status, code });
const fresh = (v) => v?.state === 'ready' && Date.now() - Date.parse(v.checked_at) < telemetryContract.retention_days * 86400000;
export function createDeploymentDiagnostics({ store, find, service, classifier, signal }) {
  const collecting = new Map(), tasks = new Set();
  function identity(record) { return JSON.stringify([record.session_id, record.id, record.app, record.target_id, record.source_commit, String(record.ci?.run_id)]); }
  function binding(record) {
    const state = store.read(), runId = record.ci?.run_id ? String(record.ci.run_id) : null;
    const bound = runId && Object.hasOwn(state.bindings, runId) ? state.bindings[runId] : null;
    if (!bound || bound.operation_id !== record.id || bound.app !== record.app || bound.target_id !== record.target_id
        || bound.source_commit !== record.source_commit) throw error('DIAGNOSTIC_BINDING_MISMATCH');
    return { runId, source_commit: record.source_commit, app: record.app, target_id: record.target_id };
  }
  function current(id, sessionId, expected) {
    const record = find('deployments', id, sessionId);
    if (identity(record) !== expected) throw error('DIAGNOSTIC_BINDING_MISMATCH');
    binding(record); return record;
  }
  function track(promise) {
    tasks.add(promise); promise.finally(() => tasks.delete(promise)).catch(() => {}); return promise;
  }
  async function collect(id, sessionId) {
    const record = find('deployments', id, sessionId), expected = identity(record), bound = binding(record);
    if (!['failed', 'blocked', 'unknown'].includes(record.status)) return { state: 'not_failed' };
    if (fresh(record.diagnostic_evidence)) {
      if (service?.diagnosticCurrent) {
        try { await service.diagnosticCurrent(record.diagnostic_evidence); }
        catch { return { state: 'unavailable', reason: 'attempt_changed_or_unavailable', checked_at: new Date().toISOString() }; }
        current(id, sessionId, expected);
      }
      return record.diagnostic_evidence;
    }
    if (!service?.diagnostics) return { state: 'unavailable', reason: 'not_configured' };
    if (collecting.has(id)) { await collecting.get(id); return current(id, sessionId, expected).diagnostic_evidence || { state: 'unavailable' }; }
    const work = (async () => {
      let diagnostic;
      try { diagnostic = await service.diagnostics(bound.runId, bound); }
      catch { diagnostic = { state: 'unavailable', reason: 'evidence_not_verified', checked_at: new Date().toISOString() }; }
      current(id, sessionId, expected);
      await store.transaction((state) => {
        const row = state.operations[id]; if (identity(row) !== expected) throw error('DIAGNOSTIC_BINDING_MISMATCH');
        row.diagnostic_evidence = diagnostic;
        appendEvent(row, 'diagnostics.collected', 'diagnostics', diagnostic.state === 'ready' ? 'PASS' : 'UNKNOWN',
          { attributes: { state: diagnostic.state, case_id: diagnostic.case_id || null },
            evidence_refs: diagnostic.state === 'ready' ? [{ artifact_id: diagnostic.artifact_id, path: 'case.json', sha256: diagnostic.case_sha256 }] : [] });
      });
      return diagnostic;
    })();
    collecting.set(id, work);
    try { return await work; } finally { collecting.delete(id); }
  }
  async function classify(id, sessionId = null) {
    const record = find('deployments', id, sessionId), expected = identity(record);
    binding(record);
    const diagnostic = await collect(id, sessionId); current(id, sessionId, expected);
    if (!fresh(diagnostic)) return { state: 'unavailable', reason: 'evidence_not_verified' };
    if (!classifier) return { state: 'not_configured' };
    if (service.diagnosticCurrent) {
      try { await service.diagnosticCurrent(diagnostic); }
      catch { return { state: 'unavailable', reason: 'attempt_changed_or_unavailable' }; }
      current(id, sessionId, expected);
    }
    if (diagnostic.verification.release_eligible || !['FAIL', 'BLOCKED', 'UNKNOWN'].includes(diagnostic.verification.gate_outcome)) return { state: 'not_failed' };
    const input = classificationInput(diagnostic), key = input.input_sha256;
    const reserved = await store.transaction((state) => {
      const row = state.operations[id]; if (identity(row) !== expected) throw error('DIAGNOSTIC_BINDING_MISMATCH');
      row.classifications ||= {};
      if (Object.hasOwn(row.classifications, key)) { row.classification = row.classifications[key]; return { created: false, record: row.classification }; }
      if (Object.keys(row.classifications).length >= 8) throw error('CLASSIFICATION_LIMIT');
      const result = { state: 'running', input_sha256: key, rubric_version: input.rubric_version, rubric_sha256: input.rubric_sha256,
        model: input.model, case_sha256: diagnostic.case_sha256, source_sha256: diagnostic.source.tested_sha256,
        binding: diagnostic.binding, started_at: new Date().toISOString(), completed_at: null, attempts: 0,
        is_hypothesis: true, grants_write_authority: false };
      row.classifications[key] = result; row.classification = result;
      return { created: true, record: result };
    });
    if (reserved.created) track((async () => {
      let result;
      try { result = await classifier(input, { signal }); }
      catch { result = { state: 'failed', code: 'CLASSIFICATION_OUTCOME_UNKNOWN', outcome_unknown: true }; }
      await store.transaction((state) => {
        const row = state.operations[id]; if (!row || identity(row) !== expected) return;
        const item = { ...row.classifications[key], ...result, completed_at: new Date().toISOString() };
        row.classifications[key] = item;
        if (row.classification?.input_sha256 === key) row.classification = item;
        appendEvent(row, 'classification.completed', 'classification', item.state === 'succeeded' ? 'PASS' : 'UNKNOWN',
          { attributes: { model: input.model, input_sha256: key, state: item.state, is_hypothesis: true }, identity: `classification:${key}` });
      });
    })());
    return structuredClone(reserved.record);
  }
  return {
    async get(id, sessionId = null) {
      const record = find('deployments', id, sessionId);
      if (!record.ci?.run_id) return { deployment_id: id, state: 'not_started', classification: null };
      const value = await collect(id, sessionId);
      const row = find('deployments', id, sessionId);
      return { deployment_id: id, ...value, classification: row.classification || { state: classifier ? 'not_requested' : 'not_configured' } };
    },
    classify,
    schedule(id) {
      if (!collecting.has(id)) track(classify(id).catch(() => {}));
    },
    async source(id, sessionId = null) {
      const row = find('deployments', id, sessionId), expected = identity(row);
      const diagnostic = await collect(id, sessionId); current(id, sessionId, expected);
      if (!fresh(diagnostic) || !service?.diagnosticSource) throw error('DIAGNOSTIC_SOURCE_UNAVAILABLE');
      const files = await service.diagnosticSource(diagnostic); current(id, sessionId, expected); return files;
    },
    async close() { while (tasks.size || collecting.size) await Promise.allSettled([...tasks, ...collecting.values()]); },
  };
}
