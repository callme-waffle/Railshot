import { createHash, randomBytes, randomUUID, scrypt as scryptCallback, timingSafeEqual } from 'node:crypto';
import { closeSync, constants, fstatSync, lstatSync, openSync, readSync } from 'node:fs';
import { isAbsolute } from 'node:path';
import { promisify } from 'node:util';
import { DashboardError } from './sessions.js';

const scrypt = promisify(scryptCallback);
const digest = (value) => createHash('sha256').update(value).digest('hex');
const randomToken = () => randomBytes(32).toString('base64url');
const invalid = () => { throw new DashboardError('입력 또는 자격 정보를 확인하세요.', 422, 'INVALID_INPUT'); };
const unauthenticated = () => { throw new DashboardError('인증 정보를 확인하세요.', 401, 'UNAUTHENTICATED'); };
const emailPattern = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const tokenPattern = /^[A-Za-z0-9_-]{43}$/;
const sessionSeconds = 12 * 3600;
export const ACCOUNT_COOKIE = 'railshot_account';

export function accountCookie(token, secure) {
  return `${ACCOUNT_COOKIE}=${token || ''}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${token ? sessionSeconds : 0}${secure ? '; Secure' : ''}`;
}
export function accountCookieToken(header) {
  const found = (header || '').split(';').map((part) => part.trim()).filter((part) => part.startsWith(`${ACCOUNT_COOKIE}=`));
  const value = found.length === 1 ? found[0].slice(ACCOUNT_COOKIE.length + 1) : '';
  return tokenPattern.test(value) ? value : null;
}

function accountMailTokenFile(path) {
  let fd;
  try {
    if (typeof path !== 'string' || !isAbsolute(path)) throw new Error();
    const validFile = (info) => info.isFile() && info.uid === process.getuid() && info.nlink === 1
      && (info.mode & 0o7777) === 0o600 && info.size >= 1 && info.size <= 4097;
    const info = lstatSync(path);
    if (!validFile(info) || info.isSymbolicLink()) throw new Error();
    fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    const opened = fstatSync(fd);
    if (!validFile(opened) || opened.dev !== info.dev || opened.ino !== info.ino) throw new Error();
    // Bound the read even if the operator file changes after the size check.
    const bytes = Buffer.alloc(4098);
    let length = 0, count;
    while (length < bytes.length && (count = readSync(fd, bytes, length, bytes.length - length, null)) !== 0) length += count;
    const raw = bytes.subarray(0, length).toString('utf8');
    const token = raw.endsWith('\n') ? raw.slice(0, -1) : raw;
    if (!/^[\x21-\x7e]{1,4096}$/.test(token)) throw new Error();
    return token;
  } catch {
    throw new Error('RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE must be a private operator-owned 0600 regular file');
  } finally { if (fd !== undefined) closeSync(fd); }
}

export function accountMailSender(env = process.env) {
  const url = env.RAILSHOT_ACCOUNT_MAIL_URL;
  const inline = env.RAILSHOT_ACCOUNT_MAIL_TOKEN;
  // Only this sender closure retains an inline credential. CI/CD child tools
  // must not inherit it through their default process environment.
  delete env.RAILSHOT_ACCOUNT_MAIL_TOKEN;
  if (inline !== undefined && env.RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE !== undefined) {
    throw new Error('Set only one of RAILSHOT_ACCOUNT_MAIL_TOKEN and RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE');
  }
  const bearer = env.RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE === undefined ? inline : accountMailTokenFile(env.RAILSHOT_ACCOUNT_MAIL_TOKEN_FILE);
  if (bearer !== undefined && (typeof bearer !== 'string' || !/^[\x21-\x7e]{1,4096}$/.test(bearer))) throw new Error('Account mail token must contain 1-4096 non-whitespace ASCII characters');
  if (!url || !bearer) return null;
  const protocol = new URL(url).protocol;
  if (protocol !== 'https:' && !(protocol === 'http:' && env.RAILSHOT_ACCOUNT_TEST_ALLOW_HTTP === '1')) throw new Error('Account mail endpoint must be HTTPS');
  return async (message) => {
    const response = await fetch(url, { method: 'POST', headers: { Authorization: `Bearer ${bearer}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(message), signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error('Account mail delivery failed');
  };
}

async function passwordHash(password, salt = randomBytes(32)) {
  return `scrypt:${salt.toString('base64url')}:${(await scrypt(password, salt, 64, { N: 32768, r: 8, p: 1, maxmem: 64 * 1024 * 1024 })).toString('base64url')}`;
}
async function verifyPassword(password, stored) {
  const [, saltText, hashText] = stored.split(':');
  const actual = await passwordHash(password, Buffer.from(saltText, 'base64url'));
  const left = Buffer.from(actual), right = Buffer.from(stored);
  return left.length === right.length && timingSafeEqual(left, right);
}
function validPassword(password) {
  if (typeof password !== 'string' || password.length < 12 || password.length > 1024 || password.includes('\0')) invalid();
}
function validEmail(email) {
  if (typeof email !== 'string' || email.length > 254 || !emailPattern.test(email)) invalid();
  return email.trim().toLowerCase();
}
function exact(input, required) {
  return input && typeof input === 'object' && !Array.isArray(input) && Object.keys(input).length === required.length
    && required.every((key) => Object.hasOwn(input, key));
}

export function createAccountData(db, mailer = accountMailSender()) {
  async function send(email, purpose, token) {
    if (!mailer) throw new DashboardError('계정 메일 발송이 설정되지 않았습니다.', 503, 'MAIL_UNAVAILABLE');
    try { await mailer({ to: email, purpose, token }); }
    catch { throw new DashboardError('계정 메일을 보내지 못했습니다.', 503, 'MAIL_UNAVAILABLE'); }
  }
  function newToken(id, purpose, lifetime) {
    const token = randomToken(), now = Date.now();
    db.prepare('UPDATE account_tokens SET used_at = ? WHERE account_id = ? AND purpose = ? AND used_at IS NULL').run(now, id, purpose);
    db.prepare('INSERT INTO account_tokens VALUES (?, ?, ?, ?, NULL)').run(digest(token), id, purpose, now + lifetime);
    return token;
  }
  function consume(token, purpose) {
    if (typeof token !== 'string' || !tokenPattern.test(token)) unauthenticated();
    const now = Date.now();
    const row = db.prepare('SELECT account_id FROM account_tokens WHERE token_hash = ? AND purpose = ? AND expires_at > ? AND used_at IS NULL')
      .get(digest(token), purpose, now);
    if (!row || !db.prepare('UPDATE account_tokens SET used_at = ? WHERE token_hash = ? AND used_at IS NULL').run(now, digest(token)).changes) unauthenticated();
    return row.account_id;
  }
  return {
    session(token) {
      if (!token) return null;
      const row = db.prepare(`SELECT s.account_id, s.csrf_token, s.expires_at, a.email, a.verified_at
        FROM account_sessions s JOIN accounts a ON a.id = s.account_id WHERE s.token_hash = ? AND s.expires_at > ?`)
        .get(digest(token), Date.now());
      return row && row.verified_at ? { id: row.account_id, email: row.email, csrf_token: row.csrf_token,
        expires_at: new Date(row.expires_at).toISOString() } : null;
    },
    async register(input) {
      if (!exact(input, ['email', 'password'])) invalid();
      const email = validEmail(input.email); validPassword(input.password);
      if (!mailer) throw new DashboardError('계정 메일 발송이 설정되지 않았습니다.', 503, 'MAIL_UNAVAILABLE');
      const password_hash = await passwordHash(input.password), id = randomUUID(), now = Date.now();
      db.exec('BEGIN IMMEDIATE');
      let token;
      try {
        const existing = db.prepare('SELECT id, verified_at FROM accounts WHERE email = ?').get(email);
        if (!existing) {
          db.prepare('INSERT INTO accounts VALUES (?, ?, ?, NULL, ?)').run(id, email, password_hash, now);
          token = newToken(id, 'verify', 30 * 60 * 1000);
        } else if (!existing.verified_at) token = newToken(existing.id, 'verify', 30 * 60 * 1000);
        db.exec('COMMIT');
      } catch (error) { db.exec('ROLLBACK'); throw error; }
      if (token) await send(email, 'verify', token);
      return { status: 'accepted' };
    },
    verify(input) {
      if (!exact(input, ['token'])) invalid();
      db.exec('BEGIN IMMEDIATE');
      try {
        const id = consume(input.token, 'verify');
        db.prepare('UPDATE accounts SET verified_at = COALESCE(verified_at, ?) WHERE id = ?').run(Date.now(), id);
        db.exec('COMMIT');
      } catch (error) { db.exec('ROLLBACK'); throw error; }
      return { status: 'verified' };
    },
    async login(input) {
      if (!exact(input, ['email', 'password'])) invalid();
      const email = validEmail(input.email); validPassword(input.password);
      const row = db.prepare('SELECT * FROM accounts WHERE email = ?').get(email);
      // Equal-cost work also hides whether an email exists.
      const dummy = 'scrypt:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
      const valid = await verifyPassword(input.password, row?.password_hash || dummy);
      if (!row || !valid || !row.verified_at) unauthenticated();
      const token = randomToken(), csrf = randomToken(), now = Date.now();
      db.prepare('INSERT INTO account_sessions VALUES (?, ?, ?, ?, ?)').run(digest(token), row.id, csrf, now + sessionSeconds * 1000, now);
      return { token, account_id: row.id, email, csrf_token: csrf, expires_at: new Date(now + sessionSeconds * 1000).toISOString() };
    },
    logout(token) { if (token) db.prepare('DELETE FROM account_sessions WHERE token_hash = ?').run(digest(token)); return { status: 'signed_out' }; },
    async requestReset(input) {
      if (!exact(input, ['email'])) invalid();
      if (!mailer) throw new DashboardError('계정 메일 발송이 설정되지 않았습니다.', 503, 'MAIL_UNAVAILABLE');
      const email = validEmail(input.email), row = db.prepare('SELECT id FROM accounts WHERE email = ? AND verified_at IS NOT NULL').get(email);
      if (row && mailer) {
        db.exec('BEGIN IMMEDIATE');
        let token;
        try { token = newToken(row.id, 'reset', 30 * 60 * 1000); db.exec('COMMIT'); }
        catch (error) { db.exec('ROLLBACK'); throw error; }
        await send(email, 'reset', token);
      }
      return { status: 'accepted' };
    },
    async resetPassword(input) {
      if (!exact(input, ['token', 'password'])) invalid();
      validPassword(input.password);
      const hashed = await passwordHash(input.password);
      db.exec('BEGIN IMMEDIATE');
      try {
        const id = consume(input.token, 'reset');
        db.prepare('UPDATE accounts SET password_hash = ? WHERE id = ?').run(hashed, id);
        db.prepare('DELETE FROM account_sessions WHERE account_id = ?').run(id);
        db.exec('COMMIT');
      } catch (error) { db.exec('ROLLBACK'); throw error; }
      return { status: 'password_updated' };
    },
  };
}
