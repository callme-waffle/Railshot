import { request } from './api.js';

export function initializeOpenStackInstaller() {
  const result = document.querySelector('#openstack-installer-result');
  const status = document.querySelector('#openstack-install-status');
  const registrationSelect = document.querySelector('#openstack-registration-select');
  const pendingRegistration = document.querySelector('#openstack-pending-registration');

  async function installerDetails() {
    const { data } = await request('/api/v1/installers/openstack');
    if (typeof data.install_sh !== 'string' || !data.install_sh.startsWith('#!/usr/bin/env bash')
        || data.script_url !== '/api/v1/installers/openstack/scripts'
        || data.bundle_url !== '/api/v1/installers/openstack/bundles'
        || data.token_client_url !== '/api/v1/installers/openstack/client'
        || !/^[a-f0-9]{64}$/.test(data.bundle_sha256)) {
      throw new Error('설치 파일 응답을 확인하지 못했습니다.');
    }
    return data;
  }

  function showInstaller(installer, token, expiresAt) {
    if (!/^rsl_[A-Za-z0-9_-]{43}$/.test(token || '') || !Number.isFinite(Date.parse(expiresAt))) {
      throw new Error('연계 토큰 응답을 확인하지 못했습니다.');
    }
    const downloadUrl = new URL('/onpremise/install.sh', window.location.origin);
    downloadUrl.searchParams.set('token', token);
    document.querySelector('#openstack-install-command').value = `curl -fsSL '${downloadUrl}' -o install.sh`;
    document.querySelector('#openstack-linkage-token').value = token;
    document.querySelector('#openstack-token-expires').textContent = `토큰 만료: ${new Date(expiresAt).toLocaleString()}. 다운로드 명령에도 토큰이 포함되므로 공유하지 마세요.`;
    document.querySelector('#openstack-install-script').value = installer.install_sh;
    document.querySelector('#openstack-script-download').href = installer.script_url;
    document.querySelector('#openstack-bundle-download').href = installer.bundle_url;
    document.querySelector('#openstack-token-client-download').href = installer.token_client_url;
    result.hidden = false;
    status.textContent = '등록 요청을 저장하고 일회성 연계 토큰을 발급했습니다.';
  }

  async function refreshPending() {
    const { data } = await request('/api/v1/registrations');
    const pending = data.items.filter((item) => item.provider === 'openstack' && item.status === 'pending');
    registrationSelect.replaceChildren(...pending.map((item) => {
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = `${new Date(item.created_at).toLocaleString()} · ${item.id.slice(0, 8)}`;
      return option;
    }));
    pendingRegistration.hidden = pending.length === 0;
  }

  async function run(button, action) {
    status.textContent = '';
    button.disabled = true;
    try {
      const installer = await installerDetails();
      const issued = await action();
      showInstaller(installer, issued.token, issued.expiresAt);
      await refreshPending().catch(() => {});
    } catch (cause) {
      status.textContent = cause.name === 'AbortError' ? '요청 시간이 초과되었습니다. 등록 상태를 확인하세요.' : cause.message;
    } finally { button.disabled = false; }
  }

  const prepare = document.querySelector('#prepare-openstack-install');
  prepare.addEventListener('click', () => run(prepare, async () => {
    const { data } = await request('/api/v1/registrations', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: 'openstack' }) });
    if (!/^[a-f0-9-]{36}$/.test(data.id || '')) throw new Error('등록 결과를 확인하지 못했습니다.');
    return { token: data.linkage_token, expiresAt: data.token_expires_at };
  }));

  const reissue = document.querySelector('#reissue-openstack-token');
  reissue.addEventListener('click', () => run(reissue, async () => {
    const id = registrationSelect.value;
    if (!/^[a-f0-9-]{36}$/.test(id)) throw new Error('재발급할 등록을 선택하세요.');
    const { data } = await request(`/api/v1/registrations/${id}/tokens`, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (data.registration_id !== id) throw new Error('재발급 결과를 확인하지 못했습니다.');
    return { token: data.token, expiresAt: data.expires_at };
  }));

  for (const [button, source] of [['#copy-openstack-command', '#openstack-install-command'],
    ['#copy-openstack-token', '#openstack-linkage-token'], ['#copy-openstack-script', '#openstack-install-script']]) {
    document.querySelector(button).addEventListener('click', async () => {
      const field = document.querySelector(source);
      try { await navigator.clipboard.writeText(field.value); status.textContent = '복사했습니다.'; }
      catch { field.focus(); field.select(); status.textContent = '내용을 선택했습니다. 직접 복사하세요.'; }
    });
  }
  refreshPending().catch(() => {});
}
