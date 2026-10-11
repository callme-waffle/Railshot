from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'deployment/bootstrap'))
from client_setup import preflight, wireguard
from install_payload import install_payload

class ConnectionTests(unittest.TestCase):
    def test_retired_wireguard_cleanup_preserves_foreign_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'jasmin0.conf'
            path.write_text('foreign config')
            path.chmod(0o600)
            runner = Mock()
            with self.assertRaises(RuntimeError):
                wireguard.uninstall_wireguard(path, runner=runner)
            runner.assert_not_called()
            self.assertEqual(path.read_text(), 'foreign config')

    def test_retired_wireguard_cleanup_only_stops_owned_install(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'jasmin0.conf'
            path.write_text(wireguard.MARKER + '[Interface]\n')
            path.chmod(0o600)
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
            self.assertEqual(wireguard.uninstall_wireguard(path, runner=runner), {'removed': True})
            self.assertEqual(runner.call_args.args[0], ['systemctl', 'disable', '--now', 'wg-quick@jasmin0'])
            self.assertFalse(path.exists())
            self.assertEqual(wireguard.uninstall_wireguard(path, runner=runner), {'removed': False})

    def test_cleanup_failure_preserves_owned_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'jasmin0.conf'
            path.write_text(wireguard.MARKER + '[Interface]\n')
            path.chmod(0o600)
            runner = Mock(return_value=subprocess.CompletedProcess([], 1, '', ''))
            with self.assertRaises(RuntimeError):
                wireguard.uninstall_wireguard(path, runner=runner)
            self.assertTrue(path.exists())

    def test_cleanup_symlink_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            actual = Path(directory).resolve() / 'actual'
            actual.mkdir()
            link = Path(directory).resolve() / 'link'
            link.symlink_to(actual, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                wireguard.uninstall_wireguard(link / 'jasmin0.conf')

    def test_payload_install_preserves_existing_and_checks_integrity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / 'source'
            module = source / 'deployment/bootstrap/client_setup/main.py'
            module.parent.mkdir(parents=True)
            module.write_text('# test payload')
            target = root / 'installed'
            install_payload(source, target)
            install_payload(source, target)
            installed = target / 'deployment/bootstrap/client_setup/main.py'
            installed.write_text('# altered')
            with self.assertRaises(RuntimeError):
                install_payload(source, target)
            self.assertEqual(installed.read_text(), '# altered')
            foreign = root / 'foreign'
            foreign.mkdir()
            (foreign / 'existing').write_text('preserve')
            with self.assertRaises(RuntimeError):
                install_payload(source, foreign)
            self.assertEqual((foreign / 'existing').read_text(), 'preserve')

    def test_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'os-release'
            path.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, 'running\n', ''))
            check = preflight.run_preflight(os_release=path, geteuid=lambda: 0,
                                            which=lambda n: None if n in ('wg', 'wg-quick') else n, runner=runner)
            self.assertEqual(check['version'], '24.04')
            self.assertNotIn('wg', check['commands'])
            self.assertNotIn('wg-quick', check['commands'])
            with self.assertRaises(RuntimeError):
                preflight.run_preflight(os_release=path, geteuid=lambda: 1)
            with self.assertRaises(RuntimeError):
                preflight.run_preflight(os_release=path, geteuid=lambda: 0, which=lambda n: None)


if __name__ == '__main__':
    unittest.main()
