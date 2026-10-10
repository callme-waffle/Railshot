export function createModel({ apiKey = process.env.OPENAI_API_KEY, model = process.env.RAILSHOT_AGENT_MODEL, fetchImpl = fetch } = {}) {
  if (!apiKey || !model) throw new Error('OPENAI_API_KEY와 RAILSHOT_AGENT_MODEL을 설정하세요.');
  return async ({ input, tools, instructions }) => {
    const response = await fetchImpl('https://api.openai.com/v1/responses', {
      method: 'POST',
      headers: { authorization: `Bearer ${apiKey}`, 'content-type': 'application/json' },
      body: JSON.stringify({ model, store: false, instructions, input, tools, tool_choice: 'auto' }),
      signal: AbortSignal.timeout(90000),
    });
    if (!response.ok) throw new Error(`모델 요청에 실패했습니다 (${response.status}).`);
    const value = await response.json();
    if (value.status !== 'completed' || !Array.isArray(value.output)) throw new Error('모델 응답이 완료되지 않았습니다.');
    return value.output;
  };
}
