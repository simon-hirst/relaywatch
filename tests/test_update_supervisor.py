"""Safe automatic release installation and monitor-only restarts."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import update_supervisor as updater


class ReleaseTests(unittest.TestCase):
    def test_versions_sorted_numerically(self):
        self.assertGreater(updater.version('v3.18.10'), updater.version('3.18.9'))
        self.assertGreater(updater.version('v3.19'), updater.version('v3.18.99'))
        with self.assertRaises(ValueError):
            updater.version('latest')

    def test_latest_release_requires_publish_and_matching_assets(self):
        tag = 'v3.18.2'
        release = {'tag_name':tag, 'draft':False, 'prerelease':False, 'assets':[
            {'name': f'relaywatch-{tag}.zip', 'browser_download_url':updater.ASSET_ROOT+tag+f'/relaywatch-{tag}.zip'},
            {'name':'SHA256SUMS.txt','browser_download_url':updater.ASSET_ROOT+tag+'/SHA256SUMS.txt'}]}
        result = updater.latest_release('3.18.1', get=lambda url,limit:json.dumps(release).encode())
        self.assertEqual(result['tag'], tag)
        self.assertIsNone(updater.latest_release('3.18.2',get=lambda url,limit:json.dumps(release).encode()))
        release['assets'][0]['browser_download_url'] = 'https://evil.example/test.zip'
        with self.assertRaisesRegex(ValueError, 'official'):
            updater.latest_release('3.18.1', get=lambda url,limit:json.dumps(release).encode())

    def archive(self, tag='v3.18.2', *, malicious=False):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as file:
            prefix = f'relaywatch-{tag}/'
            for name, content in {'VERSION':'3.18.2\n', 'install.py':'print(1)\n',
                                  'watchdog.py':'print(1)\n', 'update_supervisor.py':'print(1)\n'}.items():
                file.writestr(prefix+name, content)
            if malicious:
                file.writestr(prefix+'../../oops.txt','bad')
        return stream.getvalue()

    def test_valid_checksum_and_safe_extract(self):
        data = self.archive()
        digest = hashlib.sha256(data).hexdigest()
        meta = {'tag':'v3.18.2', 'name':'relaywatch-v3.18.2.zip', 'zip_url':'zip', 'checksum_url':'manifest'}
        def read(url,limit):
            return data if url=='zip' else (digest+'  relaywatch-v3.18.2.zip\n').encode()
        with tempfile.TemporaryDirectory() as tmp:
            source = updater.stage_release(meta, Path(tmp), get=read)
            self.assertEqual((source/'VERSION').read_text().strip(), '3.18.2')

    def test_bad_checksum_and_traversal_rejected(self):
        meta = {'tag':'v3.18.2', 'name':'relaywatch-v3.18.2.zip', 'zip_url':'zip', 'checksum_url':'manifest'}
        data = self.archive()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'checksum mismatch'):
                updater.stage_release(meta,Path(tmp),get=lambda url,limit: data if url=='zip' else (('0'*64)+'  relaywatch-v3.18.2.zip').encode())
            malicious = self.archive(malicious=True)
            digest = hashlib.sha256(malicious).hexdigest()
            with self.assertRaisesRegex(ValueError,'unsafe'):
                updater.stage_release(meta,Path(tmp),get=lambda url,limit: malicious if url=='zip' else (digest+'  relaywatch-v3.18.2.zip').encode())
            self.assertFalse((Path(tmp)/'oops.txt').exists())

    def test_modified_managed_file_blocks_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'.agent-watchdog'
            target.mkdir()
            names = {f'file{i}.py':'ok' for i in range(12)}
            for name, value in names.items():
                (target/name).write_text(value)
            (target/'managed-files.json').write_text(json.dumps({name:hashlib.sha256(value.encode()).hexdigest() for name,value in names.items()}))
            self.assertEqual(len(updater.verify_managed_files(Path(tmp))),12)
            (target/'file1.py').write_text('modified')
            with self.assertRaisesRegex(ValueError,'locally modified'):
                updater.verify_managed_files(Path(tmp))

    def test_stop_only_supervised_child(self):
        child=mock.Mock()
        child.poll.return_value=None
        child.wait.return_value=0
        updater.stop_watcher(child)
        child.send_signal.assert_called_once_with(updater.signal.SIGINT)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_install_failure_restores_managed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'project'; (root/'.agent-watchdog').mkdir(parents=True)
            src=Path(tmp)/'update';src.mkdir()
            names=[f'module{i}.py' for i in range(12)]
            for name in names:
                (root/'.agent-watchdog'/name).write_text('original')
            manifest={name:hashlib.sha256(b'original').hexdigest() for name in names}
            (root/'.agent-watchdog'/'managed-files.json').write_text(json.dumps(manifest))
            def broken(*args,**kwargs):
                (root/'.agent-watchdog'/'module1.py').write_text('bad update')
                return subprocess.CompletedProcess(args[0],3)
            with self.assertRaisesRegex(RuntimeError,'installer returned'):
                updater.apply_release(src,root,run=broken)
            self.assertEqual((root/'.agent-watchdog'/'module1.py').read_text(),'original')
            self.assertEqual(json.loads((root/'.agent-watchdog'/'managed-files.json').read_text()),manifest)

    def test_source_launchers_offer_explicit_opt_in(self):
        for name in ('start-watchdog-window.ps1','start-windows.ps1','start-linux.sh','start-macos.sh'):
            self.assertIn('AutoUpdate' if name.endswith('.ps1') else '--auto-update', (ROOT/name).read_text())
        self.assertIn('auto-update)',(ROOT/'relaywatch.sh').read_text())


if __name__ == '__main__':
    unittest.main()
