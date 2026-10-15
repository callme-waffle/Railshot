#!/usr/bin/env node
/** Fixed bridge to the existing personal vault/WireGuard/forced-SSH transport. */
import { readFile } from 'node:fs/promises';
import { createPersonalAdapter } from './personal-adapter.js';
import { privateJson } from './environments.js';

async function main() {
  if (process.argv.length !== 3) throw new Error('Invalid bridge configuration');
  const config = await privateJson(process.argv[2]);
  const raw = await readFile('/dev/stdin', 'utf8');
  if (Buffer.byteLength(raw) > 16384) throw new Error('Request too large');
  const request = JSON.parse(raw);
  if (!request || Object.keys(request).sort().join(',') !== 'context,params') throw new Error('Invalid request');
  const target = await privateJson(config.target_file);
  if (request.context.owner_id !== config.owner_id || target.account_id !== config.owner_id
      || request.context.account_id !== config.account_id || target.project_id !== config.project_id
      || !target.vault || target.vault.project_id !== config.project_id) throw new Error('Target ownership mismatch');
  process.env.RAILSHOT_PERSONAL_CONFIG = config.personal_config;
  const adapter = await createPersonalAdapter({ configPath: config.personal_config,
    stateDirectory: config.personal_state_dir });
  const response = await adapter.execute(target, request.params);
  if (!response.ok) throw new Error('Remote command failed');
  process.stdout.write(JSON.stringify({ project_id: target.project_id, result: response.result }));
}
main().catch(() => { process.stdout.write(JSON.stringify({ error: 'ONPREM_REMOTE_EXECUTION_FAILED' })); process.exitCode = 1; });
