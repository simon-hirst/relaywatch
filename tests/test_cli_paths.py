"""Regression: WSL watchdog has no NVM PATH but managed interactive Codex does."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
from cli_paths import find_cli, guidance, cli_environment
from engine import atomic_write_json
from watchdog import queue_codex_if_safe


def executable(path, content='#!/bin/sh\nexit 0\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)
    return path


class CodexPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'sample'
        self.home.mkdir()
        self.no_path = str(self.home / 'empty-bin')

    def test_locates_nvm_binary_when_watchdog_path_does_not_include_nvm(self):
        wanted = executable(self.home / '.nvm/versions/node/v24.13.0/bin/codex')
        with patch('cli_paths.shutil.which', return_value=None):
            self.assertEqual(find_cli('codex', home=self.home, path=self.no_path), str(wanted))

    def test_prefers_newer_nvm_version_and_skips_nonexecutables(self):
        old = executable(self.home / '.nvm/versions/node/v9.0.0/bin/codex')
        newest = executable(self.home / '.nvm/versions/node/v24.13.0/bin/codex')
        executable(self.home / '.nvm/versions/node/v22.2.0/bin/codex').chmod(0o644)
        with patch('cli_paths.shutil.which', return_value=None):
            self.assertEqual(find_cli('codex', home=self.home, path=self.no_path), str(newest))
        self.assertTrue(old.exists())

    def test_respects_explicitly_configured_path_first(self):
        in_path = executable(self.home / 'manual-bin/codex')
        executable(self.home / '.nvm/versions/node/v24.13.0/bin/codex')
        self.assertEqual(find_cli('codex', home=self.home, path=str(in_path.parent)), str(in_path))

    def test_missing_binary_has_diagnostic_not_ambiguous_file_not_found(self):
        with patch('cli_paths.shutil.which', return_value=None):
            self.assertIsNone(find_cli('codex', home=self.home, path=self.no_path))
        self.assertIn('.nvm/versions/node/', guidance('codex'))
        with self.assertRaises(ValueError):
            find_cli('../codex', home=self.home)

    def test_executable_with_env_runtime_uses_nvm_node_directory(self):
        bindir = self.home / '.nvm/versions/node/v24.13.0/bin'
        cli = executable(bindir / 'codex', '#!/usr/bin/env wd31_runtime\n')
        capture = self.home / 'runtime-used.txt'
        executable(bindir / 'wd31_runtime', '#!/bin/sh\nprintf "runtime started\n" > "$WD_CAPTURE"\n')
        environment = cli_environment(str(cli), environ={
            'PATH':'/usr/bin:/bin', 'WD_CAPTURE':str(capture)})
        self.assertEqual(environment['PATH'].split(os.pathsep)[0], str(bindir))
        run = subprocess.run([str(cli)], env=environment, capture_output=True, text=True, timeout=8)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(capture.read_text(), 'runtime started\n')

    def test_queue_uses_nvm_executable_with_real_subprocess(self):
        capture = self.home / 'queued-message.txt'
        script = '#!/bin/sh\nprintf "%s\\n" "$@" > "$WD_CAPTURE"\n'
        cli = executable(self.home / '.nvm/versions/node/v24.13.0/bin/codex', script)
        root = self.home / 'checkout'
        (root / '.agent-watchdog').mkdir(parents=True)
        atomic_write_json(root / '.agent-watchdog' / 'active-codex.json', {'known-thread-id': 1010})
        usage = {'agent': 'codex', 'stage': 'handoff', 'stale': False,
                 'triggered_at': 1020, 'prep_at': 1000}
        managed = {'agent':'codex', 'type':'tmux', 'outcome':'launched', 'session':'managed'}
        with (patch('watchdog.latest_worker', return_value=managed),
              patch('watchdog.tmux_exists', return_value=True),
              patch('cli_paths.Path.home', return_value=self.home),
              patch('cli_paths.shutil.which', return_value=None),
              patch.dict(os.environ, {'WD_CAPTURE': str(capture)})):
            outcome = queue_codex_if_safe(root, {'enable_codex_queue':True,
                'active_session_seconds':120}, usage, now=1050)
        self.assertIn('MESSAGE QUEUED', outcome)
        args = capture.read_text().splitlines()
        self.assertEqual(args[:5], ['queue','--remote','unix://','--thread','known-thread-id'])
        self.assertEqual(args[5], '--message')
        self.assertIn('STOP NEW FEATURE WORK', args[6])
        self.assertTrue(cli.exists())

    def test_interactive_codex_launcher_finds_nvm_install(self):
        # Simulates a tmux shell that does NOT source ~/.bashrc / nvm.sh.
        capture = self.home / 'interactive.txt'
        executable(self.home / '.nvm/versions/node/v24.13.0/bin/codex',
                   '#!/bin/sh\nprintf "%s\\n" "$@" >> "$WD_CAPTURE"\n')
        root = self.home / 'checkout'
        wd = root / '.agent-watchdog'
        wd.mkdir(parents=True)
        (wd / 'model_picker.py').write_text('print("gpt-6.1-sol\\txhigh")\n')
        (wd / 'next-agent-prompt.txt').write_text('Continue from handoff')
        env = {'HOME':str(self.home), 'PATH':'/usr/bin:/bin',
               'WD_CAPTURE':str(capture)}
        proc = subprocess.run(['bash',str(TOOL/'interactive-agent.sh'),'codex',str(root),
            str(wd/'next-agent-prompt.txt')], env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = capture.read_text()
        self.assertIn('remote-control\nstart\n',data)
        self.assertNotIn('--yolo\n',data) # secure default
        self.assertIn('gpt-6.1-sol\n',data)
        self.assertIn('Continue from handoff\n',data)

if __name__ == '__main__':
    unittest.main()
