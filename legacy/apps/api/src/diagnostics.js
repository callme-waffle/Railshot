import { createHash } from 'node:crypto';
import { promisify } from 'node:util';
import yauzl from 'yauzl';
import { readSourceSnapshot } from './source-snapshot.js';

export const diagnosticLimits = Object.freeze({ archive: 2 * 1024 * 1024, case: 1024 * 1024, log: 65560, entries: 65 });
export const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
export const canonical = (value) => JSON.stringify(value, (_key, item) => item && typeof item === 'object' && !Array.isArray(item)
  ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item).replace(/[\x7f-\uffff]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
const hash = /^[a-f0-9]{64}$/, id = /^[A-Za-z0-9_.:-]{1,128}$/;
const outcomes = ['RUNNING', 'PASS', 'FAIL', 'BLOCKED', 'UNKNOWN', 'NOT_RUN', 'INCOMPLETE'];
const phases = ['L0', 'L1', 'Q', 'L2', 'L3', 'L4', 'SOURCE', 'CONFIG', 'EVIDENCE'];
const object = (v) => v && typeof v === 'object' && !Array.isArray(v);
const exact = (v, keys, optional = []) => object(v) && keys.every((k) => Object.hasOwn(v, k)) && Object.keys(v).every((k) => keys.includes(k) || optional.includes(k));
const integer = (v, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(v) && v >= 0 && v <= max;
const require = (ok) => { if (!ok) throw new Error('Diagnostic evidence verification failed'); };
const text = (v, max) => typeof v === 'string' && Buffer.byteLength(v) <= max && !/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(v);
const sourcePath = (v) => text(v, 1024) && !v.startsWith('/') && !v.includes('\\') && v.split('/').every((p) => p && !['.', '..', '__proto__'].includes(p));
export function redactDiagnostic(value) {
  return String(value).replace(/\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|$))/g, '')
    .replace(/-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z]+ )?PRIVATE KEY-----|$)|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{16,}|apikey_[A-Za-z0-9_-]{16,}|xox[bpas]-[A-Za-z0-9-]{10,})\b/gi, '[REDACTED]')
    .replace(/authorization\s*[:=]\s*(?:bearer|basic)\s+[^\s,;]+|(?:password|passwd|api[_-]?key|access[_-]?token|secret|token)\s*[=:]\s*["']?[^\s,"';]+|https?:\/\/[^\s/@]+:[^\s/@]+@[^\s]+|https?:\/\/[^\s?]+\?[^\s]+/gi, '[REDACTED]');
}

// Validate central-directory limits BEFORE opening any entry; never extract to disk.
export async function readDiagnosticArchive(bytes) {
  require(Buffer.isBuffer(bytes) && bytes.length > 0 && bytes.length <= diagnosticLimits.archive);
  const zip = await promisify(yauzl.fromBuffer)(bytes, { lazyEntries: true, decodeStrings: true, validateEntrySizes: true });
  const files = new Map(); let total = 0;
  try {
    require(zip.entryCount > 0 && zip.entryCount <= diagnosticLimits.entries);
    await new Promise((resolve, reject) => {
      zip.once('error', reject); zip.once('end', resolve);
      zip.on('entry', (entry) => {
        try {
          const name = entry.fileName, mode = (entry.externalFileAttributes >>> 16) & 0o170000;
          require((name === 'case.json' || /^process-[1-9]\d?\.log$/.test(name)) && !files.has(name)
            && [0, 0o100000].includes(mode) && !(entry.generalPurposeBitFlag & 1)
            && entry.uncompressedSize <= (name === 'case.json' ? diagnosticLimits.case : diagnosticLimits.log));
          total += entry.uncompressedSize; require(total <= diagnosticLimits.archive);
          zip.openReadStream(entry, async (error, stream) => {
            if (error) { reject(error); zip.close(); return; }
            try {
              const chunks = []; let size = 0;
              for await (const chunk of stream) { size += chunk.length; require(size <= entry.uncompressedSize); chunks.push(chunk); }
              require(size === entry.uncompressedSize); files.set(name, Buffer.concat(chunks)); zip.readEntry();
            } catch (cause) { reject(cause); zip.close(); }
          });
        } catch (error) { reject(error); zip.close(); }
      });
      zip.readEntry();
    });
    require(files.has('case.json')); return files;
  } finally { zip.close(); }
}

export function readDiagnosticCase(files, binding, artifact) {
  const bytes = files.get('case.json'); require(bytes && bytes.length <= diagnosticLimits.case);
  const v = JSON.parse(bytes.toString('utf8'));
  require(exact(v, ['version', 'purpose', 'case_id', 'binding', 'native_run_id', 'attempt_id', 'source', 'failure', 'checks',
    'processes', 'missing_evidence', 'policy', 'policy_sha256', 'classification', 'verification'], ['error'])
    && v.version === 1 && v.purpose === 'diagnostic' && id.test(v.native_run_id) && id.test(v.attempt_id)
    && v.attempt_id.startsWith(v.native_run_id + ':') && hash.test(v.case_id));
  const keys = ['source_commit', 'app', 'tenant', 'target_id', 'run_id', 'producer_attempt'];
  require(exact(v.binding, keys) && keys.every((k) => v.binding[k] === binding[k]));
  require(exact(v.source, ['tested_sha256', 'after_sha256', 'snapshot', 'files']) && hash.test(v.source.tested_sha256)
    && (v.source.after_sha256 === null || hash.test(v.source.after_sha256)) && object(v.source.files));
  require(v.case_id === sha256(JSON.stringify([v.native_run_id, v.attempt_id, v.source.tested_sha256])));
  const fileEntries = Object.entries(v.source.files); require(fileEntries.length <= 2000);
  for (const [path, file] of fileEntries) require(sourcePath(path) && exact(file, ['sha256', 'bytes', 'lines'])
    && hash.test(file.sha256) && integer(file.bytes, 100 * 1024 * 1024) && integer(file.lines, 100 * 1024 * 1024));
  if (v.source.snapshot !== null) require(exact(v.source.snapshot, ['path', 'sha256', 'source_sha256', 'purpose', 'state'])
    && v.source.snapshot.path === 'snapshot.json' && hash.test(v.source.snapshot.sha256)
    && v.source.snapshot.source_sha256 === v.source.tested_sha256 && v.source.snapshot.purpose === 'diagnostic' && v.source.snapshot.state === 'available');
  const f = v.failure;
  require(exact(f, ['layer', 'class', 'fingerprint', 'code', 'excerpt', 'excerpt_omitted_bytes', 'locations'])
    && (f.layer === null || phases.includes(f.layer)) && (f.class === null || /^(F[1-9]|QUALITY)$/.test(f.class))
    && (f.fingerprint === null || text(f.fingerprint, 256)) && (f.code === null || /^[A-Z][A-Z0-9_]{1,30}$/.test(f.code))
    && text(f.excerpt, 16040) && integer(f.excerpt_omitted_bytes) && Array.isArray(f.locations) && f.locations.length <= 12);
  for (const loc of f.locations) require(exact(loc, ['path', 'line', 'column', 'blob_sha256', 'mapping_status'])
    && sourcePath(loc.path) && Object.hasOwn(v.source.files, loc.path) && integer(loc.line) && loc.line > 0
    && loc.line <= v.source.files[loc.path].lines && integer(loc.column, 1000000) && loc.column > 0
    && loc.blob_sha256 === v.source.files[loc.path].sha256 && loc.mapping_status === 'exact');
  require(Array.isArray(v.checks) && v.checks.length <= 10 && new Set(v.checks.map((c) => c.check_id)).size === v.checks.length);
  for (const c of v.checks) require(exact(c, ['check_id', 'outcome', 'required'], ['duration_ms', 'reason']) && phases.includes(c.check_id)
    && outcomes.includes(c.outcome) && typeof c.required === 'boolean' && (c.duration_ms === undefined || integer(c.duration_ms, 86400000))
    && (c.reason === undefined || ['prior_check_stopped', 'profile_disabled'].includes(c.reason)));
  require(Array.isArray(v.processes) && v.processes.length <= 64);
  const referenced = new Set(['case.json']), logs = [];
  for (const [i, p] of v.processes.entries()) {
    require(exact(p, ['id', 'layer', 'command_kind', 'duration_ms', 'exit_code', 'outcome', 'stdout_bytes', 'stderr_bytes'], ['log', 'error_code'])
      && p.id === `process-${i + 1}` && (p.layer === null || phases.includes(p.layer))
      && ['docker.build', 'docker.logs', 'docker.inspect', 'native.dependencies', 'process'].includes(p.command_kind) && integer(p.duration_ms, 86400000)
      && (p.exit_code === null || Number.isSafeInteger(p.exit_code)) && outcomes.includes(p.outcome)
      && ['stdout_bytes', 'stderr_bytes'].every((k) => p[k] === null || integer(p[k]))
      && (p.error_code === undefined || ['PROCESS_TIMEOUT', 'PROCESS_START_FAILED', 'PROCESS_FAILED'].includes(p.error_code)));
    if (p.log) {
      const log = p.log, content = files.get(log.path);
      require(exact(log, ['path', 'sha256', 'redaction_version', 'capture_complete', 'omitted_bytes'])
        && log.path === p.id + '.log' && content && sha256(content) === log.sha256 && log.redaction_version === 1
        && typeof log.capture_complete === 'boolean' && integer(log.omitted_bytes));
      referenced.add(log.path); logs.push({ ...log, process_id: p.id, text: redactDiagnostic(content.toString('utf8')) });
    }
  }
  require(files.size === referenced.size && [...files.keys()].every((name) => referenced.has(name)));
  require(Array.isArray(v.missing_evidence) && v.missing_evidence.length <= 16
    && v.missing_evidence.every((s) => typeof s === 'string' && /^[a-z_]{1,64}$/.test(s)));
  const policy = v.policy;
  require(exact(policy, ['gate_order', 'repair_scope', 'max_files', 'max_full_content_bytes', 'allow_publish', 'protected_policy_sha256'])
    && Array.isArray(policy.gate_order) && policy.gate_order.length > 0 && policy.gate_order.length <= 6
    && new Set(policy.gate_order).size === policy.gate_order.length && policy.gate_order.every((p) => phases.includes(p))
    && ['packaging', 'source'].includes(policy.repair_scope) && policy.max_files === 8 && policy.max_full_content_bytes === 20000
    && policy.allow_publish === false && hash.test(policy.protected_policy_sha256) && sha256(canonical(policy)) === v.policy_sha256);
  require(exact(v.classification, ['source', 'revision', 'responsibility', 'is_hypothesis', 'grants_write_authority'])
    && v.classification.source === 'rules' && v.classification.revision === 1
    && ['platform', 'application', 'environment'].includes(v.classification.responsibility)
    && v.classification.is_hypothesis === true && v.classification.grants_write_authority === false);
  require(exact(v.verification, ['release_eligible', 'gate_outcome', 'target_check_passed', 'regression_verified', 'runtime_recovered'])
    && typeof v.verification.release_eligible === 'boolean' && outcomes.includes(v.verification.gate_outcome)
    && ['target_check_passed', 'regression_verified', 'runtime_recovered'].every((k) => v.verification[k] === 'NOT_RUN'));
  // Raw/native error strings are never reflected. This projection contains only fixed facts.
  const error = v.error ? readDiagnosticError(v.error) : null;
  return { state: 'ready', checked_at: new Date().toISOString(), artifact_id: artifact.id,
    artifact_sha256: artifact.sha256, case_sha256: sha256(bytes), ...v, error,
    failure: { ...f, excerpt: redactDiagnostic(f.excerpt) }, logs };
}

export function readDiagnosticError(e) {
  require(exact(e, ['code', 'component', 'phase', 'outcome', 'retry_policy', 'side_effect', 'causes'])
    && /^[A-Z][A-Z0-9_]{1,80}$/.test(e.code) && /^[A-Za-z0-9_.:-]{1,96}$/.test(e.component)
    && /^[A-Za-z0-9_.:-]{1,96}$/.test(e.phase) && ['FAIL', 'BLOCKED', 'UNKNOWN'].includes(e.outcome)
    && ['never', 'after_reconcile', 'after_configuration', 'safe'].includes(e.retry_policy)
    && ['none', 'possible', 'completed', 'unknown'].includes(e.side_effect) && Array.isArray(e.causes) && e.causes.length <= 4);
  for (const cause of e.causes) {
    require(exact(cause, ['type'], ['errno', 'sqlite_errorcode', 'returncode', 'frames']) && /^[A-Za-z0-9_.<>-]{1,180}$/.test(cause.type));
    for (const k of ['errno', 'sqlite_errorcode', 'returncode']) if (k in cause) require(Number.isSafeInteger(cause[k]));
    if (cause.frames) {
      require(Array.isArray(cause.frames) && cause.frames.length <= 6);
      for (const f of cause.frames) require(exact(f, ['file', 'function', 'line']) && /^[A-Za-z0-9_.<>-]{1,128}$/.test(f.file)
        && /^[A-Za-z0-9_.<>-]{1,128}$/.test(f.function) && integer(f.line) && f.line > 0);
    }
  }
  return structuredClone(e);
}

export function verifyDiagnosticSource(bytes, diagnostic) {
  require(diagnostic.state === 'ready' && diagnostic.source.snapshot && sha256(bytes) === diagnostic.source.snapshot.sha256
    && diagnostic.source.tested_sha256 === diagnostic.source.after_sha256);
  return readSourceSnapshot(bytes, diagnostic.binding, diagnostic.source.tested_sha256);
}
