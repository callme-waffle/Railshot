// Frontend contract. A backend adapter must explicitly supply read/submit capabilities.
// No inferred endpoints, shell execution, or browser-persisted answers.
export function node(tag, text = '', className = '') {
  const value = document.createElement(tag);
  value.textContent = text; value.className = className; return value;
}
export function button(text, action, className = 'secondary-button') {
  const value = node('button', text, className); value.type = 'button';
  value.addEventListener('click', action); return value;
}
const id = (value) => typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,159}$/.test(value);
const text = (value, limit = 2000) => typeof value === 'string' && value.trim().length > 0 && value.length <= limit;
const unique = (items) => new Set(items.map((item) => item.id)).size === items.length;
export function validateQuestion(question, deploymentId) {
  if (!question || !id(question.id) || question.deployment_id !== deploymentId || !id(question.revision)
      || !text(question.summary) || !text(question.prompt) || !Number.isFinite(Date.parse(question.expires_at))
      || (question.stage !== undefined && !['build', 'environment', 'deploy'].includes(question.stage))
      || !Array.isArray(question.evidence) || !question.evidence.length || question.evidence.length > 20
      || question.evidence.some((item) => !text(item.label, 200) || !text(item.text, 12000))
      || !Array.isArray(question.options) || !question.options.length || question.options.length > 10 || !unique(question.options)) {
    throw new Error('해결 방법의 형식을 확인할 수 없습니다. 상태를 다시 조회해 주세요.');
  }
  for (const option of question.options) {
    if (!id(option.id) || !text(option.label, 200) || !Array.isArray(option.fields) || option.fields.length > 20 || !unique(option.fields)) throw new Error('선택지 형식이 올바르지 않습니다.');
    for (const field of option.fields) {
      if (!id(field.id) || !text(field.label, 200) || !['text', 'text_list', 'single_select', 'multi_select'].includes(field.type)
          || typeof field.required !== 'boolean' || (field.sensitive && !['text', 'text_list'].includes(field.type))) throw new Error('추가 입력 형식을 지원하지 않습니다.');
      if (field.type.endsWith('select') && (!Array.isArray(field.choices) || !field.choices.length || field.choices.length > 30 || !unique(field.choices)
          || field.choices.some((item) => !id(item.id) || !text(item.label, 200)))) throw new Error('추가 선택 항목이 올바르지 않습니다.');
    }
  }
  return question;
}
export function validateAnswer(question, answer, now = Date.now()) {
  validateQuestion(question, question.deployment_id);
  if (Date.parse(question.expires_at) <= now) throw new Error('질문이 만료되었습니다. 상태를 다시 조회해 주세요.');
  if (answer.question_id !== question.id || answer.revision !== question.revision || answer.deployment_id !== question.deployment_id) throw new Error('질문이 변경되었습니다. 상태를 다시 조회해 주세요.');
  const option = question.options.find((item) => item.id === answer.option_id);
  if (!option) throw new Error('해결 방법을 하나 선택해 주세요.');
  if (!answer.values || Object.keys(answer.values).some((key) => !option.fields.some((field) => field.id === key))) throw new Error('선택한 방법에 해당하는 입력만 제출할 수 있습니다.');
  for (const field of option.fields) {
    const value = answer.values[field.id];
    if (['text_list', 'multi_select'].includes(field.type)) {
      if (!Array.isArray(value) || value.length > 20 || value.some((item) => !text(item, 4000)) || (field.required && !value.length)) throw new Error(`${field.label} 항목을 확인해 주세요.`);
    } else if (typeof value !== 'string' || value.length > 4000 || (field.required && !value.trim())) throw new Error(`${field.label} 항목을 입력해 주세요.`);
    if (field.type.endsWith('select')) {
      const selected = Array.isArray(value) ? value : value ? [value] : [];
      if (new Set(selected).size !== selected.length || selected.some((item) => !field.choices.some((choice) => choice.id === item))) throw new Error(`${field.label} 선택 항목을 확인해 주세요.`);
    }
  }
  return answer;
}

export function renderRecovery(host, { question, deploymentId, submit, onRefresh, preview = false, submissionState = null }) {
  host.replaceChildren();
  if (!question) {
    host.append(node('p', '아직 제공된 해결 방법이 없습니다. 확인된 로그를 검토해 주세요.', 'dh-note')); return () => {};
  }
  try { validateQuestion(question, deploymentId); } catch (error) { host.append(node('p', error.message, 'dh-error')); return () => {}; }
  const form = node('form', '', 'dh-recovery'), choices = node('fieldset', '', 'dh-choices');
  const message = node('p', '', 'dh-note'); message.setAttribute('role', 'status');
  const inputs = node('div', '', 'dh-inputs');
  let selected = null, readers = [], pending = false, settled = Boolean(submissionState), disposed = false;
  if (submissionState) message.textContent = submissionState === 'accepted' ? '응답이 접수되었습니다. 상태를 다시 확인해 주세요.'
    : submissionState === 'pending' ? '응답 제출을 처리하고 있습니다. 상태를 다시 확인해 주세요.' : '접수 여부를 확인하지 못했습니다. 상태를 다시 조회해 주세요.';
  const submitButton = node('button', preview ? '선택 내용 확인' : '선택한 방법 제출', 'primary-button'); submitButton.type = 'submit';
  const refresh = button('상태 다시 확인', () => onRefresh?.()); refresh.hidden = !onRefresh;
  const expiry = node('p', `응답 기한 · ${new Date(question.expires_at).toLocaleString('ko-KR')}`, 'dh-note');
  choices.append(node('legend', question.prompt));
  function fields(option) {
    inputs.replaceChildren(); readers = [];
    for (const field of option.fields) {
      const group = node('fieldset', '', 'dh-field'); group.append(node('legend', `${field.label}${field.required ? ' *' : ' (선택)'}`));
      const read = { id: field.id };
      if (field.type.endsWith('select')) {
        for (const choice of field.choices) {
          const label = node('label', '', 'dh-inline-choice'), input = node('input');
          input.type = field.type === 'multi_select' ? 'checkbox' : 'radio'; input.name = `field-${field.id}`; input.value = choice.id;
          label.append(input, node('span', choice.label)); group.append(label);
        }
        read.value = () => { const values = [...group.querySelectorAll('input:checked')].map((input) => input.value); return field.type === 'multi_select' ? values : values[0] || ''; };
      } else if (field.type === 'text_list') {
        const list = node('div', '', 'dh-text-list');
        const add = button('＋ 항목 추가', addRow, 'text-button');
        function addRow() {
          if (list.children.length >= 20) return;
          const row = node('div', '', 'dh-input-row'), input = node('input'); input.type = field.sensitive ? 'password' : 'text';
          input.maxLength = 4000; input.autocomplete = 'off'; input.setAttribute('aria-label', `${field.label} 항목`);
          const remove = button('×', () => { row.remove(); add.disabled = false; add.focus(); }, 'dh-remove'); remove.setAttribute('aria-label', `${field.label} 항목 삭제`);
          row.append(input, remove); list.append(row); add.disabled = list.children.length >= 20;
          if (list.children.length > 1) input.focus();
        }
        addRow(); group.append(list, add); read.value = () => [...list.querySelectorAll('input')].map((input) => input.value).filter((value) => value.trim());
      } else {
        const input = node(field.sensitive ? 'input' : 'textarea');
        if (field.sensitive) input.type = 'password'; else input.rows = 2;
        input.maxLength = 4000; input.autocomplete = 'off'; input.spellcheck = false;
        input.setAttribute('aria-label', field.label); input.required = field.required;
        group.append(input); read.value = () => input.value;
      }
      readers.push(read); inputs.append(group);
    }
  }
  for (const option of question.options) {
    const label = node('label', '', 'dh-choice'), input = node('input'); input.type = 'radio'; input.name = `resolution-${question.id}`; input.value = option.id;
    const copy = node('span'); copy.append(node('strong', option.label));
    if (option.description) copy.append(node('small', option.description));
    input.addEventListener('change', () => { selected = option; fields(option); message.textContent = ''; update(); });
    label.append(input, copy); choices.append(label);
  }
  function update() {
    const expired = Date.parse(question.expires_at) <= Date.now();
    submitButton.disabled = !selected || !submit || expired || pending || settled;
    choices.disabled = inputs.inert = expired || pending || settled;
    if (expired && !settled && !pending) message.textContent = '질문이 만료되었습니다. 상태를 다시 조회해 주세요.';
  }
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); if (pending || settled || !submit) return;
    let answer;
    try { answer = validateAnswer(question, { deployment_id: deploymentId, question_id: question.id, revision: question.revision,
      option_id: selected?.id, values: Object.fromEntries(readers.map((item) => [item.id, item.value()])) }); }
    catch (error) { message.textContent = error.message; return; }
    pending = true; message.textContent = '선택 내용을 확인하고 있습니다.'; update();
    try {
      const result = await submit(answer);
      if (disposed) return;
      if (result?.question_id !== question.id || result?.revision !== question.revision || !['accepted', 'preview'].includes(result.status)) throw new Error('응답 접수 여부를 확인하지 못했습니다. 상태를 다시 조회해 주세요.');
      if (result.status === 'preview' && !preview) throw new Error('응답 접수 여부를 확인하지 못했습니다.');
      settled = true; inputs.replaceChildren(); readers = [];
      message.textContent = result.status === 'preview' ? '입력 형식을 확인했습니다. 미리보기에서는 응답을 전송하거나 배포하지 않습니다.' : '응답이 접수되었습니다. 실행 재개 여부는 상태에서 확인해 주세요.';
    } catch (error) {
      if (disposed) return;
      // An ambiguous submission must not be blindly replayed.
      settled = true; inputs.replaceChildren(); readers = [];
      message.textContent = error.message || '접수 여부를 확인하지 못했습니다. 상태를 다시 조회해 주세요.';
    } finally { pending = false; if (!disposed) update(); }
  });
  form.append(choices, inputs, expiry, message, submitButton, refresh);
  if (!submit) form.append(node('p', '응답 제출 기능이 아직 연결되지 않았습니다.', 'dh-note'));
  host.append(form); update(); const timer = setInterval(update, 1000);
  return () => { disposed = true; clearInterval(timer); inputs.replaceChildren(); readers = []; };
}

// Keep this card separate from the question form so polling never discards answers.
export function createAgentActivityCard(host) {
  const labels = { analyzing: 'AI 처리 중', applying: '수정 적용 중', verifying: '재검증 중', succeeded: '복구 완료', failed: '이번 시도 실패', unknown: '처리 상태 확인 필요', awaiting_input: '사용자 확인 필요' };
  const checks = { succeeded: '성공', failed: '실패', running: '진행 중', not_run: '미실행', unknown: '확인 필요' };
  const card = node('section', '', 'dh-agent-card'); card.setAttribute('aria-label', 'AI 자동 복구');
  const title = node('h4', 'AI 자동 복구'), badge = node('span', '', 'dh-status'), heading = node('div', '', 'dh-title-row');
  heading.append(title, badge);
  const summary = node('p'), action = node('p'), freshness = node('p', '', 'dh-note'), timing = node('p', '', 'dh-note');
  action.setAttribute('role', 'status');
  const changes = node('details'), files = node('ul'); changes.append(node('summary', '변경 내용 보기'), files);
  const verification = node('ul', '', 'dh-agent-checks'); verification.setAttribute('aria-label', '재검증 결과');
  const history = node('details'), attempts = node('ul'); history.append(node('summary', '이전 시도'), attempts);
  card.append(heading, summary, action, freshness, timing, changes, verification, history); host.append(card);
  return {
    update(activity, unavailable = false) {
      card.hidden = !activity;
      if (!activity) return;
      badge.textContent = labels[activity.state] || labels.unknown;
      badge.className = `dh-status ${activity.state === 'succeeded' ? 'ok' : activity.state === 'failed' ? 'fail' : 'run'}`;
      summary.textContent = activity.summary || '';
      action.textContent = activity.current_action || '';
      const state = unavailable ? 'unavailable' : activity.observation?.state;
      freshness.textContent = state === 'unavailable' ? '최신 상태를 조회하지 못했습니다. 마지막 확인 내용을 표시합니다.'
        : state === 'stale' ? '최근 진행 정보가 지연되고 있습니다. 마지막 확인 내용을 표시합니다.' : '';
      const end = Date.parse(activity.finished_at || activity.updated_at), start = Date.parse(activity.started_at);
      timing.textContent = `${activity.attempt}차 시도${Number.isFinite(end - start) ? ` · 확인된 경과 ${Math.max(0, Math.floor((end - start) / 1000))}초` : ''}`;
      files.replaceChildren();
      for (const change of activity.changes || []) files.append(node('li', `${change.path} — ${change.summary} (${change.status === 'applied' ? '적용 완료 · AI 수정 설명' : '제안'})`));
      if (!files.children.length) files.append(node('li', '확인된 파일 변경이 없습니다.'));
      if (activity.omitted?.changes) files.append(node('li', `추가 ${activity.omitted.changes}개 파일은 작업 로그에서 확인하세요.`));
      verification.replaceChildren();
      for (const check of activity.verification || []) verification.append(node('li', `${check.label} · ${checks[check.state] || '확인 필요'}`));
      attempts.replaceChildren();
      for (const prior of activity.previous_attempts || []) {
        const item = node('li', `${prior.attempt}차 시도 · ${labels[prior.state] || labels.unknown}`);
        const list = node('ul');
        for (const change of prior.changes || []) list.append(node('li', `${change.path} — ${change.summary}`));
        for (const check of prior.verification || []) list.append(node('li', `${check.label} · ${checks[check.state] || '확인 필요'}`));
        item.append(list); attempts.append(item);
      }
      history.hidden = !attempts.children.length;
    },
  };
}
