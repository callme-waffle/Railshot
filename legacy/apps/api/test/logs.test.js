import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createAppLogsObserver } from '../src/logs.js';

test('logs helper accepts only backend deployment bindings and bounded safe output', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-logs-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const executable = join(directory, 'synthetic-python'), configPath = join(directory, 'config.json');
  await writeFile(executable, `#!${process.execPath}
const fs = require('node:fs');
const config = JSON.parse(fs.readFileSync(process.argv.at(-1)));
let input = ''; process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  const request = JSON.parse(input);
  if (Object.keys(request).sort().join(',') !== 'app,deployment_id,revision,run_id,source_commit,target_id') process.exit(1);
  if (config.hang) return setTimeout(() => {}, 10000);
  process.stderr.write('private diagnostic must never escape');
  process.stdout.write(JSON.stringify(config.output));
});
`, { mode: 0o700 });
  const record = { id: 'deployment-1', kind: 'deployments', app: 'demo', target_id: 'k3s-aws', source_commit: 'd'.repeat(40),
    cd: { deployed: true, state: 'deployed', revision: 'e'.repeat(40) }, ci: { run_id: '1' } };
  assert.equal((await createAppLogsObserver()(record)).state, 'not_configured');
  const observe = createAppLogsObserver({ configPath, python: executable, timeoutMs: 1000 });
  for (const changed of [{ ...record, kind: 'builds' }, { ...record, cd: { deployed: false } }, { ...record, id: '../foreign' }]) {
    assert.equal((await observe(changed)).reason, 'deployment_not_ready');
  }
  const output = { state: 'ready', checked_at: new Date().toISOString(), reason: null,
    entries: [{ pod: 'demo-one', container: 'web', text: 'listening\n', ignored: 'must not publish' }] };
  await writeFile(configPath, JSON.stringify({ output }));
  const result = await observe(record);
  assert.equal(result.state, 'ready'); assert.equal(result.deployment_id, record.id);
  assert.deepEqual(result.entries, [{ pod: 'demo-one', container: 'web', text: 'listening\n' }]);
  await writeFile(configPath, JSON.stringify({ output: { ...output, entries: [] } }));
  assert.equal((await observe(record)).state, 'no_data');
  await writeFile(configPath, JSON.stringify({ output: { ...output, state: 'unavailable', entries: [], reason: 'deployment_not_current' } }));
  assert.equal((await observe(record)).state, 'superseded');
  for (const changed of [{ ...output, reason: 'private diagnostic' },
    { ...output, entries: [{ pod: '../foreign', container: 'web', text: '' }] },
    { ...output, entries: [{ pod: 'demo-one', container: 'web', text: 'a'.repeat(32769) }] },
    { ...output, state: 'unavailable' }]) {
    await writeFile(configPath, JSON.stringify({ output: changed }));
    const denied = await observe(record);
    assert.equal(denied.reason, 'runtime_logs_unavailable'); assert.deepEqual(denied.entries, []);
  }
  await writeFile(configPath, JSON.stringify({ hang: true }));
  assert.equal((await createAppLogsObserver({ configPath, python: executable, timeoutMs: 100 })(record)).state, 'unavailable');
});
