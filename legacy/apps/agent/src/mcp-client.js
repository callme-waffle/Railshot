import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const command = fileURLToPath(new URL('./mcp.js', import.meta.url));

export async function connectTools({ path = command, env = process.env } = {}) {
  // The model credential never enters the tool subprocess.
  const { OPENAI_API_KEY, RAILSHOT_AGENT_TOKEN, ...toolEnv } = env;
  const child = spawn(process.execPath, [path], { stdio: ['pipe', 'pipe', 'ignore'], env: toolEnv });
  let nextId = 1, buffer = '';
  const pending = new Map();
  const failAll = (error) => {
    for (const { reject, timer } of pending.values()) { clearTimeout(timer); reject(error); }
    pending.clear();
  };
  child.on('error', failAll);
  child.on('exit', () => failAll(new Error('MCP 도구 프로세스가 종료되었습니다.')));
  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
    buffer += chunk;
    if (buffer.length > 2_000_000) { child.kill(); failAll(new Error('MCP 응답이 너무 큽니다.')); return; }
    let end;
    while ((end = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
      let message;
      try { message = JSON.parse(line); } catch { continue; }
      if (!pending.has(message.id)) continue;
      const { resolve, reject, timer } = pending.get(message.id);
      pending.delete(message.id); clearTimeout(timer);
      if (message.error) reject(new Error(message.error.message || 'MCP 오류'));
      else resolve(message.result);
    }
  });
  function request(method, params = {}) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error('MCP 응답 시간이 초과되었습니다.')); child.kill(); }, 35000);
      pending.set(id, { resolve, reject, timer });
      child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', id, method, params })}\n`);
    });
  }
  try {
    await request('initialize', { protocolVersion: '2025-11-25', capabilities: {}, clientInfo: { name: 'railshot-agent', version: '0.1.0' } });
    child.stdin.write(`${JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' })}\n`);
    return {
      async list() { return (await request('tools/list')).tools; },
      async call(name, args) {
        const result = await request('tools/call', { name, arguments: args });
        if (result.isError) {
          const value = result.structuredContent || JSON.parse(result.content?.find((item) => item.type === 'text')?.text || '{}');
          const error = new Error(value.message || '도구 호출에 실패했습니다.');
          error.code = value.code || 'TOOL_ERROR';
          error.outcomeUnknown = value.outcome_unknown === true;
          throw error;
        }
        return result.structuredContent || JSON.parse(result.content?.find((item) => item.type === 'text')?.text || '{}');
      },
      close() { child.kill(); },
    };
  } catch (error) { child.kill(); throw error; }
}
