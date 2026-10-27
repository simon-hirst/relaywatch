"""Portable public-release smoke tests."""
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import os,sys,tempfile,json
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from install import install
from runner import show_agent_window
from alerts import _windows_desktop
class PublicReleaseTests(TestCase):
    def test_safe_defaults_and_random_topic(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);install(root)
            conf=json.loads((root/'.agent-watchdog/config.json').read_text())
            self.assertFalse(conf['auto_launch_next_agent'])
            self.assertFalse(conf['unrestricted_mode'])
            self.assertFalse(conf['alerts']['phone']['enabled'])
            self.assertTrue(conf['alerts']['phone']['topic'].startswith('relaywatch-'))
            self.assertTrue((root/'.agent-watchdog/open-agent-window.sh').exists())
            self.assertTrue((root/'docs/WORKFLOW.md').exists())
    def test_macos_alert_does_not_interpolate_message(self):
        from types import SimpleNamespace
        calls=[]
        def run(argv,**kw):
            calls.append(argv)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch('alerts.platform.system',return_value='Darwin'):
            self.assertEqual(_windows_desktop(ROOT,'hello','bad \"; do shell script \"',run=run),'sent')
        self.assertEqual(calls[0][-1],'bad \"; do shell script \"')
        self.assertEqual(calls[0][0],'osascript')
    def test_linux_notify_send_uses_argv(self):
        from types import SimpleNamespace
        with patch('alerts.platform.system',return_value='Linux'), patch('alerts.shutil.which',return_value='/usr/bin/notify-send'), patch('alerts.os.uname') as uname:
            uname.return_value.release='6.6.1'
            seen=[]
            def run(argv,**kw):
                seen.append(argv)
                return SimpleNamespace(returncode=0,stderr='',stdout='')
            self.assertEqual(_windows_desktop(ROOT,'heading','message',run=run),'sent')
            self.assertEqual(seen[0][0],'notify-send')
