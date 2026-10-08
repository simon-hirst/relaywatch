"""Codex Daybreak recovery is safe, explicit and does not type into the TUI."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from daybreak_watch import (detect, visible_refusal, path, current, note, check,
                            proceed, restored, completed_work, description, user_prompt)
from engine import atomic_write_json, load_json
from hook import hook
from install import install
from console_ui import DashboardUI

REFUSAL = '''⚠ This content can’t be shown
We take extra care with some cybersecurity requests. If you’re doing authorized
security work, apply for Daybreak to get broader access.
Learn more: https://help.openai.com/en/articles/20001326
› Ask Codex to do something
'''

class DaybreakRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):install(self.root)
        self.config = load_json(self.root/'.agent-watchdog'/'config.json')
        self.config['alerts']['phone']['enabled'] = True
        atomic_write_json(self.root/'.agent-watchdog'/'worker.json',
                          {'agent':'codex','type':'tmux','outcome':'launched',
                           'session':'owned', 'window':'@2'})
        self.desktop=[];self.phone=[]

    def fake_capture(self, screen):
        def runner(argv, **kw):
            if argv[1] == 'list-panes':
                return SimpleNamespace(returncode=0,stdout='%2\n')
            if argv[1] == 'capture-pane':
                return SimpleNamespace(returncode=0,stdout=screen)
            raise AssertionError(argv)
        return runner

    def poll(self, now, screen=REFUSAL):
        with patch('daybreak_watch.session_name',return_value='owned'),patch('daybreak_watch.tmux_exists',return_value=True):
            return check(self.root,self.config,now=now,run=self.fake_capture(screen),
                         desktop_sender=lambda *args:(self.desktop.append(args) or 'sent'),
                         phone_sender=lambda *args:(self.phone.append(args) or 'sent'))

    def test_matches_actual_refusal_and_not_generic_security_words(self):
        self.assertTrue(detect(REFUSAL))
        self.assertTrue(detect(REFUSAL.replace('can’t','can\'t')))
        self.assertFalse(detect('Daybreak is ON, model gpt-6-sol'))
        self.assertTrue(detect('Daybreak is currently off. If you are doing authorized security work, turn it on for your next request.'))
        self.assertFalse(detect('This content cannot be shown because the file is empty'))
        self.assertFalse(visible_refusal(REFUSAL+'\n'*30+'new unrelated activity'))

    def test_detect_and_notify_once_without_touching_cli(self):
        msg=self.poll(1000)
        self.assertIn('CODEX DAYBREAK ACTION',msg)
        self.assertEqual(len(self.desktop),1)
        self.assertEqual(len(self.phone),1)
        self.assertEqual(current(self.root)['phase'],'needs_switch')
        self.assertEqual(current(self.root)['source'],'codex_refusal')
        self.assertIsNone(self.poll(1020))
        self.assertEqual(len(self.desktop),1)
        self.assertIn('No desktop acknowledgment needed',description(self.root))
        # No tmux send-keys, model switch, or access change was attempted.

    def test_hook_catches_refusal_even_if_pane_scrolled(self):
        with patch('hook.current',return_value={'stage':'normal','agent':'codex','used_percent':30}):
            result=hook(self.root,'codex','Stop','s1',{'last_assistant_message':REFUSAL})
            self.assertEqual(result,{})
            self.assertEqual(current(self.root)['phase'],'needs_switch')
            # Should NOT generate an automatic next-turn prompt while user handles Daybreak.
            self.assertEqual(hook(self.root,'codex','Stop','s1',{'last_assistant_message':'Unable to continue right now.'}),{})

    def test_ack_switch_task_complete_restore_user_confirmed(self):
        note(self.root,now=1000)
        self.assertIn('ACKNOWLEDGED',proceed(self.root,now=1020))
        self.assertEqual(current(self.root)['phase'],'in_progress')
        self.assertFalse(completed_work(self.root,'I ran a test.',now=1022))
        self.assertTrue(completed_work(self.root,'DAYBREAK_TASK_COMPLETE: tests passed',now=1030))
        self.assertEqual(current(self.root)['phase'],'needs_restore')
        self.assertIn('restore',description(self.root).lower())
        # Restoration reminder gets its own notification episode.
        msg=self.poll(1040,screen='now working; no refusal')
        self.assertIn('DAYBREAK ACTION',msg)
        self.assertEqual(len(self.desktop),1)
        self.assertIn('RECORDED USER CONFIRMATION',restored(self.root,now=1100))
        self.assertEqual(current(self.root),{})
        self.assertFalse(note(self.root,now=1101)) # old refusal has to disappear first
        self.poll(1110,screen='new work')
        self.poll(1130,screen='new work')
        self.assertTrue(note(self.root,now=1150)) # new episode can be detected

    def test_codex_phone_message_acknowledges_without_powershell(self):
        note(self.root,now=1000)
        self.assertIsNone(user_prompt(self.root, 'continue', now=1001))
        self.assertIsNone(user_prompt(self.root, 'USER-REQUESTED RESUME. Read docs/HANDOFF.md', now=1001))
        self.assertEqual(current(self.root)['phase'], 'needs_switch')
        msg=user_prompt(self.root, 'I switched to Daybreak, retry the authorised code review',now=1002)
        self.assertIn('does NOT confirm Daybreak access', msg)
        self.assertEqual(current(self.root)['phase'], 'in_progress')
        self.assertEqual(current(self.root)['acknowledged_by'], 'genuine_codex_user_prompt')
        self.assertFalse(note(self.root,now=1003)) # old visible refusal cannot rearm
        self.assertTrue(note(self.root,now=1004,origin='Codex Stop hook',fresh_stop=True))
        self.assertEqual(current(self.root)['phase'], 'needs_switch')
        self.assertEqual(current(self.root)['alerts'], {})

    def test_restoration_requires_explicit_human_confirmation_from_phone(self):
        note(self.root,now=1000)
        user_prompt(self.root,'Retry authorised task',now=1001)
        self.assertTrue(completed_work(self.root,'DAYBREAK_TASK_COMPLETE: done',now=1002))
        self.assertIsNone(user_prompt(self.root,'continue',now=1003))
        self.assertIsNone(user_prompt(self.root,'Restore the model when you can',now=1004))
        self.assertIsNone(user_prompt(self.root,'Daybreak is off',now=1005))
        self.assertIsNone(user_prompt(self.root,'What does Daybreak is off and previous model/effort restored mean?',now=1005))
        self.assertEqual(current(self.root)['phase'],'needs_restore')
        msg=user_prompt(self.root,'Daybreak is off and previous model/effort restored',now=1006)
        self.assertIn('Recorded the user',msg)
        self.assertEqual(current(self.root), {})

    def test_runtime_hook_allows_actual_codex_user_message_and_not_automated_continue(self):
        note(self.root,now=1000)
        with patch('hook.current',return_value={'stage':'normal','agent':'codex','used_percent':32,'stale':False}), \
             patch('hook.latest_worker',return_value={'agent':'codex','type':'tmux','outcome':'launched'}):
            result=hook(self.root,'codex','UserPromptSubmit','s1',{'prompt':'continue'})
            self.assertEqual(result,{})
            self.assertEqual(current(self.root)['phase'],'needs_switch')
            result=hook(self.root,'codex','UserPromptSubmit','s1',{'prompt':'Please retry the in-scope review'})
            self.assertIn('without a desktop command',result['hookSpecificOutput']['additionalContext'])
            self.assertEqual(current(self.root)['phase'],'in_progress')

    def test_watchdog_generated_handoff_prompt_does_not_acknowledge(self):
        note(self.root,now=1000)
        self.assertIsNone(user_prompt(self.root,
            'CONTINUE WITH NEXT TASK. AUTOMATIC WORKFLOW CONTINUATION. A milestone has finished, not the quota window.',
            now=1001))
        self.assertEqual(current(self.root)['phase'],'needs_switch')

    def test_no_worker_no_foreign_tui_reads(self):
        atomic_write_json(self.root/'.agent-watchdog'/'worker.json',
                          {'agent':'claude','type':'tmux','outcome':'launched','session':'owned','window':'@2'})
        with patch('daybreak_watch.tmux_exists',side_effect=AssertionError('must not read unrelated tmux')):
            self.assertIsNone(check(self.root,self.config,now=1000))
        self.assertEqual(current(self.root),{})

    def test_installer_installs_module_hook_protocol_and_ignore(self):
        self.assertTrue((self.root/'.agent-watchdog/daybreak_watch.py').is_file())
        docs=(self.root/'docs/WORKFLOW.md').read_text()
        self.assertIn('DAYBREAK_TASK_COMPLETE',docs)
        self.assertIn('daybreak-recovery.json',(self.root/'.gitignore').read_text())
        self.assertIn('daybreak-continue', (self.root/'.agent-watchdog/watchdog-windows.ps1').read_text() if (self.root/'.agent-watchdog/watchdog-windows.ps1').exists() else Path(__file__).resolve().parents[1].joinpath('watchdog-windows.ps1').read_text())

    def test_real_quota_handoff_priority(self):
        note(self.root,now=1000)
        with patch('hook.current',return_value={'stage':'handoff','agent':'codex','used_percent':91,'stale':False,'prep_at':1000}):
            result=hook(self.root,'codex','Stop','s1',{'last_assistant_message':REFUSAL})
            self.assertEqual(result.get('decision'),'block')

    def test_no_repeated_alert_after_restore_while_old_error_still_visible(self):
        self.poll(1000)
        proceed(self.root,now=1020)
        restored(self.root,now=1030)
        self.poll(1040)
        self.assertEqual(len(self.desktop),1)
        self.poll(1060,screen='nothing to see')
        self.poll(1080,screen='nothing to see')
        self.poll(1100)
        self.assertEqual(len(self.desktop),2)

if __name__=='__main__':unittest.main()
