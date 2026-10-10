#!/usr/bin/env node
import { deploySource, getRun } from './client.js';

async function main() {
  const [command, first, ...rest] = process.argv.slice(2);
  if (command === 'deploy') {
    const appIndex = rest.indexOf('--app');
    const targetIndex = rest.indexOf('--target');
    const targetId = targetIndex >= 0 ? rest[targetIndex + 1] : undefined;
    if (targetIndex >= 0 && (!targetId || targetId.startsWith('--'))) throw new Error('--target 다음에 대상 ID를 지정하세요.');
    const app = appIndex >= 0 ? rest[appIndex + 1] : null;
    if (!first) throw new Error('사용법: npm run cli -- deploy <폴더|ZIP|공개 GitHub URL> [--app 앱 이름] [--target 대상 ID]');
    console.log(JSON.stringify(await deploySource({ source: first, targetId, ...(app ? { app } : {}) }), null, 2));
  } else if (command === 'status') {
    if (!first) throw new Error('사용법: npm run cli -- status <run_id>');
    console.log(JSON.stringify(await getRun(first), null, 2));
  } else {
    throw new Error('사용법: npm run cli -- deploy <폴더|ZIP|공개 GitHub URL> [--app 앱 이름] [--target 대상 ID] | status <run_id>');
  }
}

main().catch((error) => { console.error(error.message); process.exitCode = 1; });
