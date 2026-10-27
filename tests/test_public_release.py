"""Public installation defaults and platform notification smoke tests."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from alerts import _windows_desktop
from install import install


class TestPublicRelease(unittest.TestCase):
    def test_safe_defaults_and_random_topic(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            install(project)
            config = json.loads((project / '.agent-watchdog/config.json').read_text())
            self.assertFalse(config['auto_launch_next_agent'])
            self.assertFalse(config['unrestricted_mode'])
            self.assertFalse(config['alerts']['phone']['enabled'])
            self.assertTrue(config['alerts']['phone']['topic'].startswith('relaywatch-'))
            self.assertTrue((project / '.agent-watchdog/open-agent-window.sh').exists())
            self.assertTrue((project / 'docs/WORKFLOW.md').exists())

    def test_macos_alert_does_not_interpolate_message(self):
        commands = []

        def run(argv, **kwargs):
            commands.append(argv)
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        payload = 'bad "; do shell script "'
        with mock.patch('alerts.platform.system', return_value='Darwin'):
            self.assertEqual(_windows_desktop(ROOT, 'hello', payload, run=run), 'sent')
        self.assertEqual(commands[0][-1], payload)
        self.assertEqual(commands[0][0], 'osascript')

    def test_linux_notify_send_uses_argv(self):
        with mock.patch('alerts.platform.system', return_value='Linux'), \
             mock.patch('alerts.shutil.which', return_value='/usr/bin/notify-send'), \
             mock.patch('alerts.os.uname') as uname:
            uname.return_value.release = '6.6.1'
            commands = []

            def run(argv, **kwargs):
                commands.append(argv)
                return SimpleNamespace(returncode=0, stderr='', stdout='')

            self.assertEqual(_windows_desktop(ROOT, 'heading', 'message', run=run), 'sent')
            self.assertEqual(commands[0][0], 'notify-send')


if __name__ == '__main__':
    unittest.main()
