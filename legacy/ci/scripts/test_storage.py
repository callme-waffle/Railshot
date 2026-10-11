"""Local durable file contract, no cloud/SDK or repository mutation."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from storage import durable_write


class StorageTest(unittest.TestCase):
    def test_replaces_bytes_with_private_mode_and_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            durable_write(path, b'old'); durable_write(path, b'new')
            self.assertEqual(path.read_bytes(), b'new')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_symlink_target_and_writable_shared_parent_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); original = root / 'original'
            original.write_bytes(b'preserve')
            link = root / 'link'; link.symlink_to(original)
            with self.assertRaises(PermissionError):
                durable_write(link, b'changed')
            self.assertEqual(original.read_bytes(), b'preserve')
            shared = root / 'shared'; shared.mkdir(); shared.chmod(0o777)
            with self.assertRaises(PermissionError):
                durable_write(shared / 'file', b'no')

    def test_parent_symlink_and_foreign_owner_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); real = root / 'real'; real.mkdir()
            alias = root / 'alias'; alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(OSError):
                durable_write(alias / 'file', b'no')
            with patch('storage.os.geteuid', return_value=os.geteuid() + 1):
                with self.assertRaises(PermissionError):
                    durable_write(real / 'file', b'no')

    def test_fsync_failure_preserves_old_file_before_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state'; durable_write(path, b'old')
            with patch('storage.os.fsync', side_effect=OSError(28, 'full')):
                with self.assertRaises(OSError):
                    durable_write(path, b'new')
            self.assertEqual(path.read_bytes(), b'old')
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
