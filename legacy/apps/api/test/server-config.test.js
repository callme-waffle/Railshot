import test from 'node:test';
import assert from 'node:assert/strict';
import { chmod, link, mkdir, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { readGithubToken } from '../src/server.js';

const token = 'github-token-fixture-0123456789abcdef';

test('GitHub deployment token accepts one private operator file without exposing it in configuration', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-github-token-'));
  const path = join(directory, 'token');
  try {
    await writeFile(path, `${token}\n`, { mode: 0o600 });
    assert.equal(readGithubToken({ GITHUB_TOKEN_FILE: path }), token);
    assert.equal(readGithubToken({ GITHUB_TOKEN: token }), token);
    assert.equal(readGithubToken({}), undefined);
    assert.throws(() => readGithubToken({ GITHUB_TOKEN: token, GITHUB_TOKEN_FILE: path }), /only one/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('GitHub deployment token file rejects unsafe ownership surfaces and malformed content', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-github-token-invalid-'));
  const path = join(directory, 'token'), alias = join(directory, 'alias'), hard = join(directory, 'hard');
  try {
    await writeFile(path, token, { mode: 0o600 });
    await symlink(path, alias);
    assert.throws(() => readGithubToken({ GITHUB_TOKEN_FILE: alias }), /private operator-owned/);
    await link(path, hard);
    assert.throws(() => readGithubToken({ GITHUB_TOKEN_FILE: path }), /private operator-owned/);
    await rm(hard);
    await chmod(path, 0o640);
    assert.throws(() => readGithubToken({ GITHUB_TOKEN_FILE: path }), /private operator-owned/);
    await chmod(path, 0o600);
    for (const content of ['', `${token}\n\n`, ` ${token}`, `${token} `, 'x'.repeat(4097)]) {
      await writeFile(path, content, { mode: 0o600 });
      assert.throws(() => readGithubToken({ GITHUB_TOKEN_FILE: path }), /private operator-owned/);
    }
    await mkdir(join(directory, 'directory'));
    assert.throws(() => readGithubToken({ GITHUB_TOKEN_FILE: join(directory, 'directory') }), /private operator-owned/);
    assert.throws(() => readGithubToken({ GITHUB_TOKEN_FILE: 'relative-token' }), /private operator-owned/);
    assert.throws(() => readGithubToken({ GITHUB_TOKEN_FILE: join(directory, 'missing') }), /private operator-owned/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
