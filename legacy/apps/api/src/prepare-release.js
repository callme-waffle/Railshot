// Runs as an Argo PreSync Job using the incoming API image on the current API node.
// Starting this process proves image pull finished before the live Pod is touched.
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { setTimeout as pause } from 'node:timers/promises';
import { readApiToken } from './access.js';

export async function prepareRelease({ token, templateId, url = 'http://railshot-api:4173/internal/releases/prepare',
  timeoutMs = 840_000, pollMs = 2000, request = fetch, onProgress = () => {} } = {}) {
  if (!token || !/^[a-f0-9]{64}$/.test(templateId || '')) throw new Error('RELEASE_CONFIGURATION_REQUIRED');
  const release_id = randomUUID(), started = Date.now(), deadline = started + timeoutMs;
  let lastState, lastLog = 0;
  while (Date.now() < deadline) {
    let response;
    try { response = await request(url, { method: 'POST', redirect: 'error',
      headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json', 'x-railshot-desired-template': templateId },
      body: JSON.stringify({ release_id }), signal: AbortSignal.timeout(Math.min(5000, timeoutMs)),
    }); } catch { throw new Error('API_HANDOVER_CONNECTION_FAILED'); }
    if ([401, 403].includes(response.status)) throw new Error('API_HANDOVER_AUTH_REJECTED');
    let result;
    try { result = await response.json(); } catch { throw new Error('API_HANDOVER_RESPONSE_INVALID'); }
    if (response.status === 200 && result.status === 'current' && result.release_id === release_id) return;
    if (response.status === 200 && result.status === 'prepared' && result.release_id === release_id
        && Number.isSafeInteger(result.lease_ms) && result.lease_ms >= 30_000) return;
    const state = response.status === 409 ? 'previous_release_lease'
      : response.status === 202 && result.status === 'busy'
        ? ['active_worker', 'active_request'].includes(result.waiting_for) ? result.waiting_for : 'active_work'
        : null;
    if (!state) throw new Error('API_HANDOVER_RESPONSE_INVALID');
    if (state !== lastState || Date.now() - lastLog >= 30_000) {
      onProgress({ status: 'waiting', reason: state, waited_ms: Date.now() - started });
      lastState = state; lastLog = Date.now();
    }
    await pause(pollMs);
  }
  throw new Error('API_HANDOVER_BUSY');
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const started = Date.now();
  console.log(JSON.stringify({ status: 'image_prepared', at: new Date(started).toISOString() }));
  try {
    await prepareRelease({ token: readApiToken(), templateId: process.env.RAILSHOT_DESIRED_TEMPLATE_ID,
      onProgress: (progress) => console.log(JSON.stringify(progress)) });
    console.log(JSON.stringify({ status: 'handover_prepared', waited_ms: Date.now() - started }));
  } catch (error) {
    // Never print token, response body or connection details from the live service.
    const allowed = new Set(['RELEASE_CONFIGURATION_REQUIRED', 'API_HANDOVER_CONNECTION_FAILED',
      'API_HANDOVER_AUTH_REJECTED', 'API_HANDOVER_RESPONSE_INVALID', 'API_HANDOVER_BUSY']);
    console.error(JSON.stringify({ status: 'failed', code: allowed.has(error.message) ? error.message : 'RELEASE_CONFIGURATION_REQUIRED' }));
    process.exitCode = 1;
  }
}
