import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import test from 'node:test';

test('SSH proxy authenticates CONNECT and carries only the selected peer stream', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-proxy-'));
  const target = 'personal-00000000-0000-4000-8000-000000000001';
  const token = 'a'.repeat(48);
  const server = createServer();
  let observed;
  server.on('connect', (request, socket) => {
    observed = { path: request.url, authorization: request.headers.authorization };
    socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    socket.once('data', (data) => { assert.equal(data.toString(), 'ping'); socket.end('pong'); });
  });
  try {
    await new Promise((done) => server.listen(0, '127.0.0.1', done));
    const tokenFile = join(directory, 'token');
    const configFile = join(directory, 'personal.json');
    await writeFile(tokenFile, token, { mode: 0o600 });
    await writeFile(configFile, JSON.stringify({ gateway: { service_host: '127.0.0.1',
      service_port: server.address().port, token_file: tokenFile } }), { mode: 0o600 });
    const proxy = resolve('apps/api/src/personal-gateway-proxy.js');
    const child = spawn(process.execPath, [proxy, target, '1'], { env: { ...process.env, RAILSHOT_PERSONAL_CONFIG: configFile } });
    let output = '';
    child.stdout.on('data', (data) => { output += data.toString(); });
    child.stdin.end('ping');
    const code = await new Promise((done, reject) => { child.once('error', reject); child.once('exit', done); });
    assert.equal(code, 0);
    assert.equal(output, 'pong');
    assert.deepEqual(observed, { path: `/streams/${target}/1`, authorization: `Bearer ${token}` });
  } finally {
    server.close();
    await rm(directory, { recursive: true, force: true });
  }
});
