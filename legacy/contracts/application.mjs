// App identity is shared by dashboard review, API admission, CLI and MCP.
export const APP_NAME = /^[a-z][a-z0-9-]{1,28}[a-z0-9]$/;
export const APP_NAME_MESSAGE = '앱 이름은 영문 소문자로 시작하고 영문 소문자나 숫자로 끝나는 3~30자여야 합니다. 중간에 하이픈을 사용할 수 있습니다.';

export function sourceAppName(raw) {
  const name = (typeof raw === 'string' ? raw : '').normalize('NFKD').toLowerCase().replace(/\.zip$/i, '')
    .replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 30).replace(/-+$/g, '');
  if (!APP_NAME.test(name)) throw new Error(APP_NAME_MESSAGE);
  return name;
}
