import { chmodSync, existsSync, lstatSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';

const config = '/var/lib/railshot/config';
// The single API worker shares providers; downloads must not fill the 128Mi /tmp volume.
for (const directory of [config, '/var/lib/railshot/tmp', '/var/lib/railshot/provider-cache']) {
  if (existsSync(directory) && !lstatSync(directory).isDirectory()) throw new Error('Private state must be a directory');
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  chmodSync(directory, 0o700);
}
for (const source of ['/run/config', '/run/dns', '/run/personal-config'].filter(existsSync)) {
  for (const name of readdirSync(source).filter((value) => !value.startsWith('.'))) {
    // After handoff the registrar owns the live target list; a legacy Secret must not restore it.
    if (name === 'observer.json' && process.env.RAILSHOT_OBSERVER_PRODUCT_FILE) continue;
    const destination = `${config}/${name}`;
    if (existsSync(destination) && !lstatSync(destination).isFile()) throw new Error('Private config must be a regular file');
    writeFileSync(destination, readFileSync(`${source}/${name}`), { mode: 0o600 });
    chmodSync(destination, 0o600);
  }
}
const gatewayToken = '/run/personal-gateway-ipc/ipc-token';
if (existsSync(gatewayToken)) {
  const destination = `${config}/personal-gateway-token`;
  writeFileSync(destination, readFileSync(gatewayToken), { mode: 0o600 });
  chmodSync(destination, 0o600);
}
const repository = '/var/lib/railshot/repository';
const git = (...args) => execFileSync('git', args, { stdio: ['ignore', 'pipe', 'pipe'], timeout: 120_000 }).toString().trim();
if (!existsSync(repository)) {
  git('clone', '--single-branch', '--branch', 'deployment/apps', '--no-tags',
    'https://github.com/Jasmin-Softbank/Railshot.git', repository);
  chmodSync(repository, 0o700);
}
if (!lstatSync(repository).isDirectory() || git('-C', repository, 'branch', '--show-current') !== 'deployment/apps'
    || git('-C', repository, 'remote', 'get-url', 'origin') !== 'https://github.com/Jasmin-Softbank/Railshot.git') {
  throw new Error('Registered config checkout differs');
}
git('-C', repository, 'config', 'user.name', 'Railshot product API');
git('-C', repository, 'config', 'user.email', 'railshot@users.noreply.github.com');
