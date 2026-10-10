import test from 'node:test';
import assert from 'node:assert/strict';
import { apiAccessConfig, allowsHost, allowsOrigin } from '../src/access.js';

test('public demo is explicit and uses exact origins without browser credentials', () => {
  const env = { RAILSHOT_PUBLIC_DEMO: '1', RAILSHOT_BIND_HOST: '0.0.0.0',
    RAILSHOT_ALLOWED_HOSTS: 'railshot.io,api', RAILSHOT_ALLOWED_ORIGINS: 'https://railshot.io' };
  const access = apiAccessConfig(env);
  assert.equal(access.publicDemo, true);
  assert.equal(access.token, undefined);
  assert.equal(allowsHost('railshot.io', access), true);
  assert.equal(allowsHost('railshot.io.evil.test', access), false);
  assert.equal(allowsOrigin('https://railshot.io', access), true);
  assert.equal(allowsOrigin('https://evil.test', access), false);
  assert.equal(allowsOrigin('null', access), false);
  for (const key of ['RAILSHOT_ALLOWED_HOSTS', 'RAILSHOT_ALLOWED_ORIGINS']) {
    const invalid = { ...env }; delete invalid[key];
    assert.throws(() => apiAccessConfig(invalid));
  }
  assert.throws(() => apiAccessConfig({ ...env, RAILSHOT_PUBLIC_DEMO: 'true' }));
  assert.throws(() => apiAccessConfig({ ...env, RAILSHOT_PUBLIC_DEMO: '0' }), /token/);
  assert.equal(apiAccessConfig({}).publicDemo, false);
});
