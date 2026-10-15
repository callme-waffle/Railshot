/** Common environment management. Credentials remain in the operator account registry. */
export function createEnvironmentManagement(request, onChange) {
  const panel = document.querySelector('#control-environments');
  const message = panel.querySelector('[data-message]'), list = panel.querySelector('[data-environments]');
  const accountSelect = panel.querySelector('[name=account]');
  let accounts = [], records = [];
  async function send(path, { method = 'GET', body, key } = {}) {
    const headers = { 'Content-Type': 'application/json', ...(key ? { 'Idempotency-Key': key } : {}) };
    if (method !== 'GET') {
      try { const { data } = await request('/api/v1/logins'); if (data.csrf_token) headers['X-Railshot-CSRF'] = data.csrf_token; } catch {}
    }
    return request('/api/v1/' + path, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  }
  async function refresh() {
    try {
      const [registered, environments] = await Promise.all([send('tenancies?limit=100'), send('clusters?limit=100')]);
      if (registered.data.next_marker || environments.data.next_marker) throw new Error('환경 목록이 한도를 초과했습니다. 운영자에게 조회 범위 조정을 요청하세요.');
      accounts = registered.data.items; records = environments.data.items;
      panel.querySelector('form button').disabled = accounts.length === 0;
      const selected = accountSelect.value;
      accountSelect.replaceChildren(...accounts.map((account) => new Option(`${account.provider.toUpperCase()} · ${account.id} · ${account.ownership === 'managed' ? 'Railshot 관리 계정' : '내 계정'}`, account.id)));
      if (accounts.some((account) => account.id === selected)) accountSelect.value = selected;
      list.replaceChildren();
      for (const row of records) {
        const item = document.createElement('li');
        const label = document.createElement('p');
        label.textContent = `${row.spec.name} · ${row.spec.provider.toUpperCase()} · ${row.status} · ${row.deployable ? '앱 배포 가능' : '앱 배포 준비 전'}`;
        item.append(label);
        const evidence = document.createElement('p');
        evidence.className = 'field-note';
        evidence.textContent = Object.entries(row.readiness).map(([key, value]) => `${({ infrastructure: '인프라', cluster: '클러스터', storage: '스토리지', database: 'DB', deployment_binding: '배포 연결', public_access: '공개 경로', application_http: '앱 HTTP' })[key] || key}: ${value ? '확인' : '미확인'}`).join(' · ');
        item.append(evidence);
        if (row.plan?.changes?.length) {
          const plan = document.createElement('p'); plan.className = 'field-note';
          plan.textContent = `계획: ${row.plan.changes.map((change) => `${change.kind} ${change.action} ${change.count}개`).join(' · ')} · 만료 ${row.plan.expires_at} · 검토 ID ${row.plan.hash}`;
          item.append(plan);
        }
        for (const [key, title] of [['retained', '보존된 자원'], ['residuals', '미삭제 자원']]) {
          if (row[key]?.length) { const detail = document.createElement('p'); detail.textContent = `${title}: ${row[key].map((resource) => `${resource.kind} ${resource.id}`).join(', ')}`; item.append(detail); }
        }
        if (row.blockers.length) { const detail = document.createElement('p'); detail.textContent = row.blockers.join(', '); item.append(detail); }
        for (const [action, text] of [['reconcile', '실제 상태 확인'], ...(row.status === 'planned' ? [['apply', '계획 실행']] : []), ...(row.status !== 'deleted' ? [['delete', '환경 삭제']] : [])]) {
          const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary-button'; button.textContent = text;
          button.addEventListener('click', async () => {
            if (action === 'delete' && !window.confirm(`${row.spec.name} 환경을 삭제합니다. 앱은 먼저 삭제해야 하며 영속 데이터 보존 정책을 적용합니다.`)) return;
            button.disabled = true; message.textContent = '요청 중입니다.';
            try {
              await send(`clusters/${row.id}${action === 'delete' ? '' : '/operations'}`, { method: action === 'delete' ? 'DELETE' : 'POST', body: action === 'delete' ? undefined : { action }, key: crypto.randomUUID() });
              await refresh();
            } catch (error) { message.textContent = `${error.message} (${error.code || 'ERROR'})`; }
            finally { button.disabled = false; }
          });
          item.append(button);
        }
        list.append(item);
      }
      message.textContent = accounts.length ? '계획을 확인한 후 실행하세요. 실행 중에는 새로고침으로 진행 상태를 확인할 수 있습니다.' : '이 소유자에게 연결된 클라우드 계정이 없습니다. 운영자가 계정 연결과 실행 정책을 등록해야 합니다.';
      onChange(records);
    } catch (error) { message.textContent = `${error.message} (${error.code || 'ERROR'})`; panel.querySelector('form button').disabled = true; }
  }
  panel.querySelector('[data-refresh]').addEventListener('click', refresh);
  panel.querySelector('form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const values = new FormData(event.target), account = accounts.find((item) => item.id === values.get('account'));
    if (!account) { message.textContent = '연결된 계정을 선택하세요.'; return; }
    const spec = { version: 1, name: values.get('name'), provider: account.provider, account_id: account.id,
      region: account.regions[0], scope: values.get('scope'), runtime: { kind: 'kubeadm', control_planes: 1, workers: 2 }, database: { mode: values.get('database') },
      ...(values.get('scope') === 'dedicated' ? { application_name: values.get('application') } : {}) };
    message.textContent = '환경 계획을 확인하고 있습니다.';
    const button = event.target.querySelector('button'); button.disabled = true;
    try { await send('clusters', { method: 'POST', body: spec, key: crypto.randomUUID() }); await refresh(); }
    catch (error) { message.textContent = `${error.message} (${error.code || 'ERROR'})`; }
    finally { button.disabled = false; }
  });
  return { refresh };
}
