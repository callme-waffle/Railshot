import { open, mkdir, readFile, rename, unlink, lstat, realpath, readdir, stat } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { DashboardError, createDashboardData } from './sessions.js';
import { createRegistrations } from './openstack/registrations.js';
import { validateFiles } from './archive.js';

const run = promisify(execFile);
async function processIdentity(pid) {
  if (process.platform === 'linux') {
    try {
      const value = await readFile(`/proc/${pid}/stat`, 'utf8');
      return `${(await readFile('/proc/sys/kernel/random/boot_id', 'utf8')).trim()}:${value.slice(value.lastIndexOf(')') + 2).split(' ')[19]}`;
    } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }
  try { return (await run('ps', ['-o', 'lstart=', '-p', String(pid)])).stdout.trim() || null; }
  catch (error) { if (error.code === 1) return null; throw error; }
}

// One process owns one private workspace. Committed records precede external writes.
export async function createProductStore(directory) {
  let root = resolve(directory);
  await mkdir(root, { recursive: true, mode: 0o700 });
  const info = await lstat(root);
  if (!info.isDirectory() || info.uid !== process.getuid() || (info.mode & 0o077)) throw new Error('A private owned workspace directory is required');
  root = await realpath(root);
  async function readPrivate(path) {
    const file = await lstat(path);
    if (!file.isFile() || file.uid !== process.getuid() || (file.mode & 0o077)) throw new Error('Invalid private state file');
    return JSON.parse(await readFile(path, 'utf8'));
  }
  const lockPath = join(root, 'owner.json');
  const owner = { pid: process.pid, identity: await processIdentity(process.pid), nonce: randomUUID() };
  // ponytail: local PID ownership is sufficient for this single-host, single-replica demo; no distributed queue/lease.
  for (let attempt = 0; ; attempt++) {
    try {
      const lock = await open(lockPath, 'wx', 0o600);
      try { await lock.writeFile(JSON.stringify(owner)); await lock.sync(); } finally { await lock.close(); }
      break;
    } catch (error) {
      if (error.code !== 'EEXIST' || attempt > 0) throw error;
      const previous = await readPrivate(lockPath);
      if (!Number.isSafeInteger(previous.pid) || previous.pid < 1) throw new Error('Invalid workspace owner');
      const identity = await processIdentity(previous.pid);
      if (identity && (!previous.identity || identity === previous.identity)) throw new Error('Workspace is already open');
      await unlink(lockPath);
    }
  }
  let closed = false, poisoned = false, closePromise;
  let snapshotBytes = 0;
  for (const name of await readdir(root)) if (name.endsWith('.source.json')) snapshotBytes += (await stat(join(root, name))).size;
  async function atomic(name, data) {
    const destination = join(root, name), temporary = `${destination}.${randomUUID()}.tmp`;
    const file = await open(temporary, 'wx', 0o600);
    try { await file.writeFile(JSON.stringify(data)); await file.sync(); }
    catch (error) { await unlink(temporary).catch(() => {}); throw error; }
    finally { await file.close(); }
    await rename(temporary, destination);
    const directoryHandle = await open(root, 'r');
    try { await directoryHandle.sync(); } finally { await directoryHandle.close(); }
  }
  let state, db, dashboard, registrations;
  try {
    const path = join(root, 'dashboard.sqlite3');
    try { const file = await open(path, 'wx', 0o600); await file.close(); }
    catch (error) { if (error.code !== 'EEXIST') throw error; }
    for (const name of ['dashboard.sqlite3', 'dashboard.sqlite3-wal', 'dashboard.sqlite3-shm']) {
      try {
        const file = await lstat(join(root, name));
        if (!file.isFile() || file.uid !== process.getuid() || (file.mode & 0o077)) throw new Error('Invalid private database file');
      } catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    db = new DatabaseSync(path);
    db.exec('PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA busy_timeout=5000;');
    const version = db.prepare('PRAGMA user_version').get().user_version;
    if (![0, 1, 2].includes(version)) throw new Error('Unsupported database schema');
    db.exec(await readFile(new URL('./dashboard-schema.sql', import.meta.url), 'utf8'));
    // Remove registration fields that are no longer used for token issue or claim.
    const registrationColumns = new Set(db.prepare('PRAGMA table_info(registrations)').all().map((column) => column.name));
    for (const name of ['auth_type', 'project_id', 'user_id', 'key_salt', 'key_hash']) {
      if (registrationColumns.has(name)) db.exec(`ALTER TABLE registrations DROP COLUMN ${name}`);
    }
    if (version === 0) {
      try { state = await readPrivate(join(root, 'state.json')); }
      catch (error) { if (error.code !== 'ENOENT') throw error; state = { version: 1, operations: {}, keys: {}, bindings: {}, plans: {} }; }
      // Legacy shared records stay operator-owned; never assign them to the first visitor.
      for (const value of Object.values(state.operations || {})) delete value.session_id;
      for (const value of Object.values(state.plans || {})) delete value.session_id;
    } else {
      state = { version: 1, operations: {}, keys: {}, bindings: {}, plans: {} };
      state.applications = {};
      for (const table of ['operations', 'plans', 'applications']) for (const row of db.prepare(`SELECT id, record FROM ${table}`).all()) state[table][row.id] = JSON.parse(row.record);
      for (const row of db.prepare('SELECT run_id, record FROM bindings').all()) state.bindings[row.run_id] = JSON.parse(row.record);
      for (const row of db.prepare('SELECT key, operation_id FROM idempotency').all()) state.keys[row.key] = row.operation_id;
    }
    if (state.version !== 1 || !state.operations || !state.keys || !state.bindings || !state.plans) throw new Error('Invalid workspace state');
    state.applications ||= {};
    state.personal = JSON.parse(db.prepare("SELECT record FROM personal_state WHERE id = 'personal'").get()?.record || '{"targets":{},"enrollments":{}}');
    for (const app of Object.values(state.applications)) if (['registering', 'stopping', 'starting', 'deleting'].includes(app.status)) app.status = 'unknown';
    for (const operation of Object.values(state.operations)) {
      for (const item of Object.values(operation.classifications || {})) if (item.state === 'running') {
        Object.assign(item, { state: 'failed', code: 'CLASSIFICATION_INTERRUPTED', outcome_unknown: true, completed_at: new Date().toISOString() });
      }
      if (operation.classification?.state === 'running') operation.classification = operation.classifications?.[operation.classification.input_sha256]
        || { ...operation.classification, state: 'failed', code: 'CLASSIFICATION_INTERRUPTED', outcome_unknown: true };
      if (operation.diagnostic_evidence && Date.now() - Date.parse(operation.diagnostic_evidence.checked_at) > 7 * 86400000) delete operation.diagnostic_evidence;
      const unclaimed = operation.kind === 'deployments' && operation.status === 'queued'
        && Number.isSafeInteger(operation.queue?.sequence) && operation.queue.sequence > 0
        && operation.queue.enqueued_at && !operation.queue.started_at;
      if (['queued', 'running'].includes(operation.status) && !unclaimed) {
        if (operation.stage === 'ci' && operation.dispatch?.state === 'preparing' && !operation.ci?.run_id) {
          // Protocol 2 persists the commit before any ref write or CI dispatch.
          // A killed pre-checkpoint worker can safely rebuild from its saved input.
          if (operation.dispatch.version === 2 && operation.kind === 'deployments' && operation.queue?.enqueued_at
              && !operation.source_commit && !operation.deletion_requested) {
            operation.status = 'queued'; operation.error = null;
            delete operation.queue.started_at;
            continue;
          }
          operation.status = 'failed';
          operation.error = { code: 'CI_DISPATCH_NOT_SENT', request_id: randomUUID(), message: '소스 준비 중 서버가 재시작되었습니다. GitHub 실행 요청은 보내지 않았습니다.', retryable: false, outcome_unknown: false };
          continue;
        }
        operation.status = 'unknown';
        operation.unknown_since = new Date().toISOString();
        operation.error = { code: 'INTERRUPTED', request_id: randomUUID(), message: '실행이 중단되어 결과를 다시 확인해야 합니다.', retryable: false, outcome_unknown: true };
        if (operation.kind === 'application-lifecycle') {
          operation.residuals = state.plans[operation.plan_id]?.public?.resources || [];
          if (state.applications[operation.application_id]) state.applications[operation.application_id].status = 'unknown';
        }
      }
    }
    for (const target of Object.values(state.personal.targets)) {
      if (target.status === 'deleting') target.status = 'attention';
      if (target.status === 'ready') target.status = 'connecting';
    }
    persist(state);
    dashboard = await createDashboardData(db, root);
    registrations = createRegistrations(db);
  } catch (error) { db?.close(); await unlink(lockPath); throw error; }
  function persist(next) {
    // ponytail: rewrite the existing bounded 100-operation snapshot in one transaction;
    // use row-level updates when the workspace retention limit grows.
    db.exec('BEGIN IMMEDIATE');
    try {
      db.exec('DELETE FROM bindings; DELETE FROM idempotency; DELETE FROM operations; DELETE FROM plans; DELETE FROM applications;');
      const operation = db.prepare('INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)');
      for (const [id, value] of Object.entries(next.operations)) operation.run(id, value.session_id ?? null, value.kind, value.status, value.created_at ?? null, JSON.stringify(value));
      const plan = db.prepare('INSERT INTO plans VALUES (?, ?, ?)');
      for (const [id, value] of Object.entries(next.plans)) plan.run(id, value.session_id ?? null, JSON.stringify(value));
      const application = db.prepare('INSERT INTO applications VALUES (?, ?, ?, ?, ?)');
      for (const [id, value] of Object.entries(next.applications || {})) application.run(id, value.session_id ?? null, value.environment_target_id, value.app, JSON.stringify(value));
      const binding = db.prepare('INSERT INTO bindings VALUES (?, ?, ?)');
      for (const [id, value] of Object.entries(next.bindings)) binding.run(id, value.operation_id, JSON.stringify(value));
      const key = db.prepare('INSERT INTO idempotency VALUES (?, ?)');
      for (const [id, value] of Object.entries(next.keys)) key.run(id, value);
      db.prepare("INSERT INTO personal_state VALUES ('personal', ?) ON CONFLICT(id) DO UPDATE SET record=excluded.record").run(JSON.stringify(next.personal || { targets: {}, enrollments: {} }));
      db.exec('PRAGMA user_version=2; COMMIT');
    } catch (error) { db.exec('ROLLBACK'); throw error; }
  }
  let tail = Promise.resolve();
  return {
    dashboard,
    registrations,
    read: () => structuredClone(state),
    operationPage(kind, sessionId, { limit, marker }) {
      if (!['builds', 'deployments', 'environments'].includes(kind)) throw new Error('Invalid operation kind');
      const where = `kind = ?${sessionId ? ' AND session_id = ?' : ' AND (session_id IS NULL OR session_id NOT IN (SELECT session_id FROM owners))'}${kind === 'builds' ? " AND json_extract(record, '$.ci.run_id') IS NOT NULL" : ''}`;
      const args = sessionId ? [kind, sessionId] : [kind];
      const publicId = kind === 'builds' ? "CAST(json_extract(record, '$.ci.run_id') AS TEXT)" : 'id';
      const anchor = marker === null ? null : db.prepare(`SELECT id, COALESCE(created_at, '') AS created FROM operations WHERE ${where} AND ${publicId} = ?`).get(...args, marker);
      if (marker !== null && !anchor) throw new DashboardError('marker가 잘못되었습니다.');
      const total = db.prepare(`SELECT count(*) AS total FROM operations WHERE ${where}`).get(...args).total;
      const rows = db.prepare(`SELECT record FROM operations WHERE ${where}
        ${anchor ? "AND (COALESCE(created_at, ''), id) < (?, ?)" : ''}
        ORDER BY COALESCE(created_at, '') DESC, id DESC LIMIT ?`)
        .all(...args, ...(anchor ? [anchor.created, anchor.id] : []), limit + 1);
      return { records: rows.slice(0, limit).map((row) => JSON.parse(row.record)), hasMore: rows.length > limit, total };
    },
    snapshotBytes: () => snapshotBytes,
    async readSnapshot(id) {
      if (!/^[a-f0-9-]{36}$/.test(id)) throw new Error('Invalid snapshot identifier');
      const value = await readPrivate(join(root, `${id}.source.json`));
      if (!Array.isArray(value)) throw new Error('Invalid source snapshot');
      return validateFiles(value.map((file) => {
        if (!file || typeof file.content !== 'string') throw new Error('Invalid source snapshot');
        const content = Buffer.from(file.content, 'base64');
        if (content.toString('base64') !== file.content) throw new Error('Invalid source encoding');
        return { path: file.path, content };
      }));
    },
    transaction(update) {
      const pending = tail.then(async () => {
        if (closed || poisoned) throw new Error('Workspace requires recovery');
        const next = structuredClone(state);
        const result = await update(next);
        try { persist(next); } catch (error) { poisoned = true; throw error; }
        state = next;
        return result;
      });
      tail = pending.catch(() => {});
      return pending;
    },
    async snapshot(id, files) {
      if (!/^[a-f0-9-]{36}$/.test(id)) throw new Error('Invalid snapshot identifier');
      try {
        await atomic(`${id}.source.json`, files.map(({ path, content }) => ({ path, content: content.toString('base64') })));
        snapshotBytes += (await stat(join(root, `${id}.source.json`))).size;
      } catch (error) { poisoned = true; throw error; }
    },
    close() {
      // Server shutdown and explicit callers must all wait until the owner lock is released.
      return closePromise ||= (async () => {
        await tail;
        closed = true;
        db.exec('PRAGMA wal_checkpoint(TRUNCATE)');
        db.close();
        const current = await readPrivate(lockPath);
        if (current.nonce === owner.nonce) await unlink(lockPath);
      })();
    },
  };
}
