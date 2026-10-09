import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, link, mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { accountMailSender } from '../src/accounts.js';

const token = 'account-mail-test-fixture-0123456789abcdef';
const message = { to: 'account@example.test', purpose: 'verify', token: 'verification-test-fixture' };

async function fixture(t) {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-account-mail-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, 'token');
  await writeFile(path, `${token}\n`, { mode: 0o600 });
  return { directory, path, env: { RAILSHOT_ACCOUNT_MAIL_URL: 'https://mail.example.test/messages', RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE: path } };
}

test('account mail sends a private token file through the sender closure', async (t) => {
  const f = await fixture(t), received = [];
  const server = createServer(async (request, response) => {
    let body = '';
    for await (const chunk of request) body += chunk;
    received.push({ authorization: request.headers.authorization, body: JSON.parse(body) });
    response.writeHead(204); response.end();
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise((resolve) => server.close(resolve)));
  f.env.RAILSHOT_ACCOUNT_MAIL_URL = `http://127.0.0.1:${server.address().port}/messages`;
  f.env.RAILSHOT_ACCOUNT_TEST_ALLOW_HTTP = '1';
  const sender = accountMailSender(f.env);
  assert.equal(f.env.RAILSHOT_ACCOUNT_MAIL_TOKEN, undefined);
  // The sender uses its startup snapshot; rotating an operator file requires a restart.
  await writeFile(f.path, 'replacement-test-fixture');
  await sender(message);
  assert.deepEqual(received, [{ authorization: `Bearer ${token}`, body: message }]);
});

test('account mail rejects unsafe private files and malformed credentials', async (t) => {
  const f = await fixture(t), alias = join(f.directory, 'alias'), hard = join(f.directory, 'hard');
  const reject = (path = f.path) => assert.throws(() => accountMailSender({ ...f.env, RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE: path }), /private operator-owned/);
  await symlink(f.path, alias); reject(alias);
  await link(f.path, hard); reject(); await rm(hard);
  for (const mode of [0o400, 0o640, 0o644, 0o700]) { await chmod(f.path, mode); reject(); }
  await chmod(f.path, 0o600);
  for (const content of ['', `${token}\n\n`, ` ${token}`, `${token} `, `${token}\r\n`, 'x'.repeat(4097)]) {
    await writeFile(f.path, content); reject();
  }
  await mkdir(join(f.directory, 'directory')); reject(join(f.directory, 'directory'));
  reject('relative-token'); reject(join(f.directory, 'missing'));
  await writeFile(f.path, `${'x'.repeat(4096)}\n`);
  assert.equal(typeof accountMailSender(f.env), 'function');
  const getuid = process.getuid;
  try { process.getuid = () => getuid() + 1; reject(); }
  finally { process.getuid = getuid; }
});

test('account mail consumes inline tokens before child processes can inherit them', async (t) => {
  const previous = process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN;
  const previousUrl = process.env.RAILSHOT_ACCOUNT_MAIL_URL;
  const previousFile = process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE;
  delete process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE;
  process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN = token;
  process.env.RAILSHOT_ACCOUNT_MAIL_URL = 'https://mail.example.test/messages';
  t.after(() => {
    if (previous === undefined) delete process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN; else process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN = previous;
    if (previousUrl === undefined) delete process.env.RAILSHOT_ACCOUNT_MAIL_URL; else process.env.RAILSHOT_ACCOUNT_MAIL_URL = previousUrl;
    if (previousFile === undefined) delete process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE; else process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE = previousFile;
  });
  assert.equal(typeof accountMailSender(), 'function');
  assert.equal(process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN, undefined);
  const child = spawnSync(process.execPath, ['-e', 'process.stdout.write(String(process.env.RAILSHOT_ACCOUNT_MAIL_TOKEN === undefined))'], { encoding: 'utf8' });
  assert.equal(child.status, 0); assert.equal(child.stdout, 'true');
});

test('account mail requires one credential source and explicit test HTTP opt-in', async (t) => {
  const f = await fixture(t);
  const simultaneous = { ...f.env, RAILSHOT_ACCOUNT_MAIL_TOKEN: token };
  assert.throws(() => accountMailSender(simultaneous), /only one/);
  assert.equal(simultaneous.RAILSHOT_ACCOUNT_MAIL_TOKEN, undefined);
  assert.equal(accountMailSender({}), null);
  assert.equal(accountMailSender({ RAILSHOT_ACCOUNT_MAIL_URL: 'https://mail.example.test/messages' }), null);
  assert.throws(() => accountMailSender({ RAILSHOT_ACCOUNT_MAIL_URL: 'https://mail.example.test/messages', RAILSHOT_ACCOUNT_MAIL_TOKEN: 'invalid token' }), /non-whitespace ASCII/);
  assert.throws(() => accountMailSender({ ...f.env, RAILSHOT_ACCOUNT_MAIL_URL: 'http://mail.example.test/messages', RAILSHOT_ACCOUNT_TEST_ALLOW_HTTP: '0' }), /HTTPS/);
  assert.throws(() => accountMailSender({ ...f.env, RAILSHOT_ACCOUNT_MAIL_URL: 'file:///messages', RAILSHOT_ACCOUNT_TEST_ALLOW_HTTP: '1' }), /HTTPS/);
});
