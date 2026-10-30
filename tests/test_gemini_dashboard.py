"""Gemini must remain visible in the RelayWatch console before installation.

The model picker controls the NEXT manual launch only; no quota is guessed.
"""
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

import model_picker as picker
import watchdog
from console_ui import DashboardUI
from install import install
from gemini_session import cli_path


class GeminiDashboardTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root=pathlib.Path(self.t.name)
        install(self.root)
        self.pref=self.root/'.agent-watchdog/models.json'

    def test_default_picker_displays_gemini_without_existing_preferences(self):
        with patch.object(picker,'DATA',self.pref):
            choices=picker.saved()
            self.assertEqual(choices['gemini'],
                             {'model':'gemini-4-argon-high','effort':'default'})
            entries,sources=picker.catalogs(refresh=False)
            self.assertEqual([item['id'] for item in entries['gemini']],
                             ['gemini-4-argon-high','gemini-4-argon-medium'])
            self.assertIn('Antigravity',sources['gemini'])

    def test_old_two_provider_saves_preserve_gemini_default(self):
        with patch.object(picker,'DATA',self.pref):
            picker.save({'claude':{'model':'claude-opus-5-5','effort':'high'},
                         'codex':{'model':'gpt-6.1-sol','effort':'xhigh'}})
            self.assertEqual(picker.saved()['gemini']['model'],'gemini-4-argon-high')
            self.assertIn('gemini',self.pref.read_text())
            with self.assertRaises(ValueError):
                picker.save({**picker.saved(),'gemini':{'model':'gemini-5-unknown',
                                                       'effort':'default'}})

    def test_preset_selection_persists_and_survives_installer(self):
        with patch.object(picker,'DATA',self.pref):
            picker.save({**picker.saved(),
                         'gemini':{'model':'gemini-4-argon-medium','effort':'default'}})
            install(self.root)
            self.assertEqual(picker.saved()['gemini']['model'],'gemini-4-argon-medium')

    def test_dashboard_always_shows_missing_cli_without_running_it(self):
        with patch.object(picker,'DATA',self.pref), \
             patch('console_ui.cli_path',return_value=None):
            ui=DashboardUI(self.root)
            lines=[text for text,_ in ui._lines(110)]
        self.assertTrue(any('GEMINI' in line and 'agy missing' in line for line in lines))
        self.assertTrue(any('quota unknown' in line for line in lines))
        self.assertTrue(any('manual launch only' in line for line in lines))

    def test_dashboard_reports_cli_found_but_does_not_claim_authenticated(self):
        with patch.object(picker,'DATA',self.pref), \
             patch('console_ui.cli_path',return_value='/home/user/.local/bin/agy'):
            ui=DashboardUI(self.root)
            lines=[text for text,_ in ui._lines(120)]
        self.assertTrue(any('CLI found, sign-in unverified' in line for line in lines))
        self.assertFalse(any('ready / within limits' in line and 'GEMINI' in line for line in lines))

    def test_dashboard_active_session_keeps_quota_unknown(self):
        with patch.object(picker,'DATA',self.pref), \
             patch('console_ui.cli_path',return_value=None):
            ui=DashboardUI(self.root)
            ui.gemini_active=True
            lines=[text for text,_ in ui._lines(110)]
            self.assertTrue(any('GEMINI' in line and 'session active' in line for line in lines))
            self.assertTrue(any('quota unknown' in line for line in lines))

    def test_manual_launch_uses_saved_medium_by_default(self):
        config=self.root/'.agent-watchdog/config.json'
        with patch.object(picker,'DATA',self.pref):
            picker.save({**picker.saved(),
                         'gemini':{'model':'gemini-4-argon-medium','effort':'default'}})
            with patch.object(watchdog,'ROOT',self.root), \
                 patch('watchdog.launch_gemini',return_value='CREATED manual Gemini') as launcher, \
                 patch.object(sys,'argv',['watchdog.py','launch-gemini']):
                watchdog.main()
            self.assertEqual(launcher.call_args.kwargs['model'],'medium')
            with patch.object(watchdog,'ROOT',self.root), \
                 patch('watchdog.launch_gemini',return_value='CREATED manual Gemini') as launcher, \
                 patch.object(sys,'argv',['watchdog.py','launch-gemini','--gemini-model','high']):
                watchdog.main()
            self.assertEqual(launcher.call_args.kwargs['model'],'high')

    def test_cli_location_checks_standard_wsl_install_path(self):
        args=[]
        def which(name,*,path):
            args.append((name,path))
            return '/home/test/.local/bin/agy'
        self.assertTrue(cli_path(which=which))
        self.assertEqual(args[0][0],'agy')
        self.assertIn('.local/bin',args[0][1])


if __name__=='__main__':
    unittest.main()
