"""The standard calculator path needs no generated application code."""
import json
import tempfile
import unittest
from pathlib import Path

from native_packaging import prepare_packaging
import yaml


class NativePackagingTest(unittest.TestCase):
    def test_plain_html_preserves_nested_assets_without_build_or_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            for name, content in {'index.html': '<meta http-equiv="refresh" content="0; url=src/calculator.html">',
                                  'src/calculator.html': '<script src="app.js"></script>',
                                  'src/app.js': 'document.title = "Calculator"',
                                  'public/sounds/click.wav': 'fixture', 'LICENSE': 'MIT'}.items():
                path = ws / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            original = {p.relative_to(ws): p.read_bytes() for p in ws.rglob('*') if p.is_file()}
            self.assertEqual('static-html', prepare_packaging(ws, 'web-calculator')['profile'])
            self.assertNotIn('RUN ', (ws / 'Dockerfile').read_text())
            for name, content in original.items():
                self.assertEqual(content, (ws / name).read_bytes())
            self.assertEqual(8080, yaml.safe_load((ws / '.railshot/railshot.yaml').read_text())['services'][0]['port'])

    def test_backend_or_unbuilt_sources_are_not_mistaken_for_static(self):
        for name in ['app.py', 'index.php', 'src/App.tsx', 'server/package.json', 'requirements.txt']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                ws = Path(directory)
                (ws / 'index.html').write_text('<div></div>')
                path = ws / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
                self.assertEqual('unsupported', prepare_packaging(ws, 'app')['status'])
                self.assertFalse((ws / 'Dockerfile').exists())

    def test_existing_dockerfile_uses_only_explicit_final_stage_port(self):
        for expose, status in [('EXPOSE 8080/tcp', 'prepared'), ('EXPOSE 80 8080', 'unsupported'),
                               ('EXPOSE $PORT', 'unsupported'), ('EXPOSE 53/udp', 'unsupported'), ('', 'unsupported')]:
            with self.subTest(expose=expose), tempfile.TemporaryDirectory() as directory:
                ws = Path(directory)
                source = f'FROM node:24 AS build\nEXPOSE 3000\nFROM node:24-slim\n{expose}\nUSER 65532\nCMD ["node", "app.js"]\n'
                (ws / 'Dockerfile').write_text(source)
                result = prepare_packaging(ws, 'app')
                self.assertEqual(status, result['status'])
                self.assertEqual(source, (ws / 'Dockerfile').read_text())
                if status == 'prepared':
                    self.assertEqual(8080, yaml.safe_load((ws / '.railshot/railshot.yaml').read_text())['services'][0]['port'])

    def test_static_calculator_and_unsupported_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            ws = Path(directory)
            package = {'scripts': {'build': 'tsc && vite build'}, 'devDependencies': {'vite': '^4.4.5'}}
            (ws / 'package.json').write_text(json.dumps(package))
            (ws / 'index.html').write_text('<div id="root"></div>')
            (ws / 'yarn.lock').write_text('# yarn lockfile v1\n')
            original = {p.name: p.read_bytes() for p in ws.iterdir()}
            for invalid in ([], {'scripts': []}, {'packageManager': []}):
                (ws / 'package.json').write_text(json.dumps(invalid))
                self.assertEqual('unsupported', prepare_packaging(ws, 'calculator')['status'])
            (ws / 'package.json').write_bytes(original['package.json'])
            # Memos has a Go server; never misclassify its Vite frontend as the app.
            (ws / 'go.mod').write_text('module github.com/usememos/memos\n')
            self.assertEqual('unchanged', prepare_packaging(ws, 'memos')['status'])
            (ws / 'go.mod').unlink()
            (ws / 'vite.config.ts').write_text('export default {build: {outDir: "other"}}')
            self.assertEqual('unsupported', prepare_packaging(ws, 'calculator')['status'])
            (ws / 'vite.config.ts').unlink()
            result = prepare_packaging(ws, 'calculator')
            self.assertEqual('prepared', result['status'])
            self.assertIn('yarn install --frozen-lockfile', (ws / 'Dockerfile').read_text())
            self.assertIn('USER 65532', (ws / 'Dockerfile').read_text())
            for name, content in original.items():
                self.assertEqual(content, (ws / name).read_bytes())
            self.assertEqual('unchanged', prepare_packaging(ws, 'calculator')['status'])


if __name__ == '__main__':
    unittest.main()
