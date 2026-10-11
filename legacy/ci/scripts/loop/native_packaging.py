"""Package complete static sites, Vite apps and single-port Docker apps without AI."""
import json
import re
from pathlib import Path

import yaml


def write_packaging(ws, app_id, profile, dockerfile=None, port=8080):
    spec = {'apiVersion': 'railshot/v0', 'app': app_id, 'services': [
        {'name': 'web', 'build': {'dockerfile': 'Dockerfile'}, 'port': port, 'health': '/', 'route': '/'}]}
    (ws / '.railshot').mkdir(exist_ok=True)
    (ws / '.railshot/railshot.yaml').write_text(yaml.safe_dump(spec, sort_keys=False))
    written = ['.dockerignore', '.railshot/railshot.yaml']
    if dockerfile is not None:
        (ws / 'Dockerfile').write_text(dockerfile)
        written.insert(0, 'Dockerfile')
    ignore = ws / '.dockerignore'
    original = ignore.read_text() if ignore.exists() else ''
    ignore.write_text(original + '\n.git\n.env*\nnode_modules\n'
                      + ('dist\n' if profile == 'vite-static' else '')
                      + ('**/.*\nDockerfile\n' if profile == 'static-html' else ''))
    return {'status': 'prepared', 'profile': profile, 'written': written}


def plain_static_site(ws):
    if not (ws / 'index.html').is_file():
        return False
    assets = {'.html', '.htm', '.css', '.js', '.mjs', '.json', '.map', '.svg', '.png', '.jpg', '.jpeg',
              '.gif', '.webp', '.avif', '.ico', '.woff', '.woff2', '.ttf', '.otf', '.eot', '.mp3', '.wav',
              '.ogg', '.mp4', '.webm', '.pdf', '.txt', '.xml', '.webmanifest', '.md'}
    # Do not serve a backend or a source framework as if it were a finished website.
    for path in ws.rglob('*'):
        if any(part.startswith('.') for part in path.relative_to(ws).parts) or not path.is_file():
            continue
        if path.name in {'package.json', 'requirements.txt', 'CMakeLists.txt', 'Dockerfile'}:
            return False
        if path.suffix.lower() not in assets and path.name.upper() not in {'LICENSE', 'LICENCE', 'NOTICE'}:
            return False
    return True


def prepare_packaging(workspace, app_id):
    ws = Path(workspace)
    if not app_id or any((ws / name).exists() for name in (
            '.railshot/railshot.yaml', '.jasmin/jasmin.yaml')):
        return {'status': 'unchanged'}
    if (ws / 'Dockerfile').is_file():
        # A multi-stage build's final stage determines its public listener.
        stages = re.split(r'(?im)^\s*FROM\s+', (ws / 'Dockerfile').read_text())
        exposed = re.findall(r'(?im)^\s*EXPOSE\s+([^\n#]+)', stages[-1])
        ports = {token.removesuffix('/tcp') for line in exposed for token in line.split()}
        if len(ports) != 1 or not all(p.isdigit() and 1 <= int(p) <= 65535 for p in ports):
            return {'status': 'unsupported', 'reason': 'Dockerfile must declare exactly one HTTP port with EXPOSE'}
        return write_packaging(ws, app_id, 'dockerfile', port=int(ports.pop()))
    if (ws / 'go.mod').exists():
        return {'status': 'unchanged'}
    if plain_static_site(ws):
        dockerfile = ('FROM nginxinc/nginx-unprivileged:stable-alpine\n'
                      'COPY . /usr/share/nginx/html\n'
                      'USER 65532\nEXPOSE 8080\nCMD ["nginx", "-g", "daemon off;"]\n')
        return write_packaging(ws, app_id, 'static-html', dockerfile)
    manifest = ws / 'package.json'
    if not manifest.is_file() or not (ws / 'index.html').is_file():
        return {'status': 'unsupported'}
    try:
        package = json.loads(manifest.read_text())
    except ValueError:
        return {'status': 'unsupported'}
    if (not isinstance(package, dict) or any(not isinstance(package.get(key, {}), dict)
            for key in ('dependencies', 'devDependencies', 'scripts'))
            or not isinstance(package.get('packageManager', ''), str)):
        return {'status': 'unsupported'}
    dependencies = {**package.get('dependencies', {}), **package.get('devDependencies', {})}
    build = package.get('scripts', {}).get('build', '')
    # ponytail: only default dist output; custom/SSR builds keep the existing adapter path.
    if ('vite' not in dependencies or package.get('workspaces')
            or build not in ('vite build', 'tsc && vite build', 'tsc -b && vite build')):
        return {'status': 'unsupported'}
    for config in ws.glob('vite.config.*'):
        if re.search(r'\b(outDir|ssr|lib|rollupOptions)\s*:', config.read_text()):
            return {'status': 'unsupported'}
    locks = [name for name in ('package-lock.json', 'yarn.lock', 'pnpm-lock.yaml') if (ws / name).is_file()]
    if len(locks) != 1:
        return {'status': 'unsupported'}
    manager = package.get('packageManager', '')
    if locks == ['package-lock.json'] and (not manager or manager.startswith('npm@')):
        setup, install, command = '', 'npm ci', 'npm run build'
    elif locks == ['yarn.lock'] and (not manager or re.fullmatch(r'yarn@1\.[0-9]+\.[0-9]+', manager)):
        if '# yarn lockfile v1' not in (ws / 'yarn.lock').read_text()[:200]:
            return {'status': 'unsupported'}
        setup, install, command = 'RUN npm install --global --force yarn@1.22.22\n', 'yarn install --frozen-lockfile', 'yarn build'
    elif locks == ['pnpm-lock.yaml'] and re.fullmatch(r'pnpm@[0-9]+\.[0-9]+\.[0-9]+', manager):
        setup, install, command = f'RUN npm install --global {manager}\n', 'pnpm install --frozen-lockfile', 'pnpm run build'
    else:
        return {'status': 'unsupported'}
    dockerfile = (f'FROM node:24-slim AS build\nWORKDIR /app\n{setup}'
                  f'COPY package.json {locks[0]} ./\nRUN {install}\nCOPY . .\nRUN {command}\n'
                  'FROM nginxinc/nginx-unprivileged:stable-alpine\n'
                  'COPY --from=build /app/dist /usr/share/nginx/html\n'
                  'USER 65532\nEXPOSE 8080\nCMD ["nginx", "-g", "daemon off;"]\n')
    return write_packaging(ws, app_id, 'vite-static', dockerfile)
