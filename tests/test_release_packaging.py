"""Source-release packaging and public launcher regression tests."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_release import build


class TestReleasePackaging(unittest.TestCase):
    def test_rejects_invalid_tag_without_running_git(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                build('v1.2.3; echo unsafe', Path(temp))

    @unittest.skipUnless(shutil.which('git'), 'Git is required for archive testing')
    def test_archives_tracked_source_only_and_checksums(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'repo'
            root.mkdir()
            (root / 'README.md').write_text('Portable source\n', encoding='utf-8')
            (root / '.env').write_text('DUMMY_PRIVATE_VALUE\n', encoding='utf-8')
            subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
            subprocess.run(['git', 'add', 'README.md'], cwd=root, check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                            'commit', '-qm', 'Initial source'], cwd=root, check=True)
            assets = build('v1.0.1', Path(temp) / 'release-assets', root=root)
            self.assertEqual(len(assets), 3)
            self.assertEqual(assets[0].name, 'relaywatch-v1.0.1.zip')
            self.assertEqual(assets[1].name, 'relaywatch-v1.0.1.tar.gz')
            with zipfile.ZipFile(assets[0]) as archive:
                self.assertIn('relaywatch-v1.0.1/README.md', archive.namelist())
                self.assertNotIn('relaywatch-v1.0.1/.env', archive.namelist())
            with tarfile.open(assets[1], 'r:gz') as archive:
                self.assertIn('relaywatch-v1.0.1/README.md', archive.getnames())
            lines = assets[2].read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 2)
            for file, line in zip(assets[:2], lines):
                self.assertEqual(line, f'{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}')

    def test_version_is_documented_in_changelog(self):
        version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
        notes = (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8')
        self.assertIn(f'## v{version}', notes)

    def test_platform_launchers_exist(self):
        for name in ('start-windows.ps1', 'start-linux.sh', 'start-macos.sh'):
            with self.subTest(name=name):
                self.assertTrue((ROOT / name).is_file())


if __name__ == '__main__':
    unittest.main()
