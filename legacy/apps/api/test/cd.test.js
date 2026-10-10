import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout } from 'node:timers/promises';
import { test } from 'node:test';
import { createCdAdapter } from '../src/cd.js';
import { createProductService } from '../src/product.js';

test('removing all app registrations keeps the API available without accepting an upload or inventing a default app', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-empty-cd-'));
  let product;
  t.after(async () => { await product?.close(); await rm(directory, { recursive: true, force: true }); });
  const configPath = join(directory, 'config.json');
  await writeFile(configPath, JSON.stringify({ version: 1, targets: {} }), { mode: 0o600 });
  const deployPublished = createCdAdapter({ configPath, loadPublished: async () => assert.fail('No publication for an unregistered app') });
  product = await createProductService({ directory: join(directory, 'product'),
    service: { targetId: 'k3s-aws', targetIds: ['k3s-aws'], deploy: async () => assert.fail('No CI dispatch') },
    target: { id: 'k3s-aws', provider: 'aws' }, deployPublished });
  assert.deepEqual(deployPublished.targets, {});
  assert.ok(product.deploymentOptions().every(({ available }) => !available));
  assert.equal(product.targets()[0].capabilities.application_deployment, false);
  assert.equal(product.targets()[0].application_name, undefined);
  await assert.rejects(product.createDeployment({ source_name: 'calculator',
    deployment_selection: { environment: 'cloud', provider: 'aws' } }, 'new-upload'), { code: 'CAPABILITY_UNAVAILABLE' });
  await assert.rejects(deployPublished({ app: 'calculator', targetId: 'k3s-aws', sourceCommit: 'a'.repeat(40),
    publication: { app: 'calculator', tenant: 'team', target_id: 'k3s-aws', source_commit: 'a'.repeat(40) } }),
  /Registered deployment application differs/);
});

test('CD adapter validates output and terminates the whole process group on abort or timeout', async (t) => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-cd-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const configPath = join(directory, 'config.json');
  await writeFile(configPath, JSON.stringify({ version: 1, targets: {
    'k3s-aws': { app: 'demo', tenant: 'team', target: { id: 'k3s-aws' } },
  } }), { mode: 0o600 });
  const request = { deploymentId: 'deployment-1', app: 'demo', targetId: 'k3s-aws', sourceCommit: 'a'.repeat(40),
    publication: { app: 'demo', tenant: 'team', target_id: 'k3s-aws', source_commit: 'a'.repeat(40) } };
  const python = join(directory, 'fake-python');
  const adapter = (timeoutMs = 10_000) => createCdAdapter({ configPath, python, timeoutMs,
    loadPublished: async () => [{ path: 'handoff.json', content: Buffer.from('{}') }] });
  await writeFile(python, '#!/usr/bin/env node\nprocess.stdin.resume();process.stdin.on("end",()=>process.stdout.write("sensitive-invalid-json"));\n', { mode: 0o700 });
  assert.equal((await adapter()(request)).cd.state, 'unknown');
  for (const mode of ['abort', 'deadline']) {
    const heartbeat = join(directory, mode + '-heartbeat');
    const script = `#!/usr/bin/env node
const {spawn}=require('node:child_process');
const {writeFileSync}=require('node:fs');
const child=spawn(process.execPath,['-e',${JSON.stringify(`process.on('SIGTERM',()=>{});setInterval(()=>require('node:fs').writeFileSync(${JSON.stringify(heartbeat)},String(Date.now())),20);`)}],{stdio:'inherit'});
process.on('SIGTERM',()=>{});process.stdin.resume();setInterval(()=>{},1000);
`;
    await writeFile(python, script, { mode: 0o700 });
    const controller = new AbortController();
    const resultPromise = adapter(mode === 'deadline' ? 700 : 10_000)({ ...request, signal: controller.signal });
    let observed;
    for (let attempt = 0; attempt < 100; attempt++) {
      try { observed = await readFile(heartbeat, 'utf8'); break; } catch { await setTimeout(10); }
    }
    assert.ok(observed, 'the native grandchild started before cancellation');
    if (mode === 'abort') controller.abort();
    const result = await resultPromise;
    assert.equal(result.cd.state, 'unknown');
    assert.equal(result.error.outcome_unknown, true);
    const stopped = await readFile(heartbeat, 'utf8');
    await setTimeout(100);
    assert.equal(await readFile(heartbeat, 'utf8'), stopped, 'no native child continues after callback completion');
  }
});
