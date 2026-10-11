export const requests = new Set();

function retryDelay(signal) {
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, 500);
    signal.addEventListener('abort', abort, { once: true });
    if (signal.aborted) abort();
  });
}

export async function request(path, options = {}, controller = new AbortController()) {
  requests.add(controller);
  const headers = new Headers(options.headers || {});
  if (/^\/api\/v1\/(?:owners(?:[/?]|$)|recoveries(?:[/?]|$)|operations(?:[/?]|$)|targets(?:[/?]|$))/.test(path)) {
    headers.set('X-Railshot-Request', 'dashboard');
  }
  const asynchronousPlan = path.endsWith('/plans') && options.headers?.Prefer === 'respond-async';
  const timeout = setTimeout(() => controller.abort(), asynchronousPlan ? 15000 : options.method === 'POST' ? (path.endsWith('/plans') ? 600000 : 120000) : path.endsWith('/logs') ? 60000 : 15000);
  const safeReplay = !options.method || options.method === 'GET' || asynchronousPlan
    || /^\/api\/v1\/applications\/[^/]+\/operations$/.test(path) && options.method === 'POST'
      && Boolean(new Headers(options.headers).get('Idempotency-Key'));
  try {
    for (let attempt = 0; ; attempt++) {
      let response;
      try { response = await fetch(path, { credentials: 'same-origin', ...options, headers, signal: controller.signal, redirect: 'error' }); }
      catch (error) {
        if (!safeReplay || attempt >= 20 || controller.signal.aborted) throw error;
        await retryDelay(controller.signal); continue;
      }
      if (safeReplay && [502, 503, 504].includes(response.status) && attempt < 20) {
        // Only transient transport responses are repeated, with identical body/key.
        // A structured application error is handled below unless it is the release fence.
        const envelope = await response.clone().json().catch(() => null);
        if ([502, 504].includes(response.status) || !envelope || envelope.error?.code === 'PLATFORM_UPDATING') {
          await response.body?.cancel(); await retryDelay(controller.signal); continue;
        }
      }
      let data;
      try { data = response.status === 204 ? null : await response.json(); }
      catch (cause) {
        if (controller.signal.aborted) throw cause;
        // A proxy HTML error does not prove that a submitted operation was rejected.
        throw Object.assign(new Error(`서버 응답을 확인하지 못했습니다 (HTTP ${response.status}). 잠시 후 상태를 다시 확인하세요.`),
          { status: response.status, code: 'RESPONSE_UNREADABLE' });
      }
      if (response.status === 503 && data?.error?.code === 'PLATFORM_UPDATING'
          && data.error.outcome_unknown === false && attempt < 20) {
        await retryDelay(controller.signal); continue;
      }
      if (!response.ok) {
        const failure = new Error(data?.error?.message || (typeof data?.error === 'string' ? data.error : `요청 실패 (HTTP ${response.status})`));
        Object.assign(failure, { status: response.status, code: data?.error?.code,
          outcomeUnknown: data?.error?.outcome_unknown, admission: data?.error?.admission });
        throw failure;
      }
      return { data, location: response.headers.get('location'), status: response.status };

    }
  } finally { clearTimeout(timeout); requests.delete(controller); }
}
