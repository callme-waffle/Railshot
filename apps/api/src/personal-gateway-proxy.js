#!/usr/bin/env node
/** SSH ProxyCommand: carry an SSH byte stream through the isolated gateway. */
import { request } from 'node:http';
import { lstat, readFile } from 'node:fs/promises';
import { privateJson } from './environments.js';

const targetId = process.argv[2];
const generation = process.argv[3];
if (!/^personal-[a-f0-9-]{36}$/.test(targetId || '') || !/^[1-9][0-9]{0,9}$/.test(generation || '')) process.exit(2);

async function main() {
  const config = await privateJson(process.env.RAILSHOT_PERSONAL_CONFIG || '');
  const gateway = config.gateway;
  if (!gateway || !/^[a-z0-9.-]+$/i.test(gateway.service_host || '')
      || !Number.isSafeInteger(gateway.service_port) || gateway.service_port < 1 || gateway.service_port > 65535
      || typeof gateway.token_file !== 'string' || !gateway.token_file.startsWith('/')) throw new Error('Gateway configuration invalid');
  const info = await lstat(gateway.token_file);
  if (!info.isFile() || info.uid !== process.getuid() || info.mode & 0o077 || info.size > 513) throw new Error('Gateway token unavailable');
  const token = (await readFile(gateway.token_file, 'utf8')).trim();
  if (!/^[A-Za-z0-9._~-]{32,512}$/.test(token)) throw new Error('Gateway token invalid');
  const req = request({ host: gateway.service_host, port: gateway.service_port,
    method: 'CONNECT', path: `/streams/${targetId}/${generation}`,
    headers: { Authorization: `Bearer ${token}` } });
  req.setTimeout(10000, () => req.destroy(new Error('Gateway connect timed out')));
  req.on('connect', (response, socket, head) => {
    if (response.statusCode !== 200) { socket.destroy(); process.exitCode = 1; return; }
    req.setTimeout(0);
    if (head.length) process.stdout.write(head);
    process.stdin.pipe(socket);
    socket.pipe(process.stdout);
    socket.on('error', () => { process.exitCode = 1; });
    socket.on('close', () => { process.stdin.unpipe(socket); });
  });
  req.on('response', () => { process.exitCode = 1; });
  req.on('error', () => { process.exitCode = 1; });
  req.end();
}

main().catch(() => { process.exitCode = 1; });
