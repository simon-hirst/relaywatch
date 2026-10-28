import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from codex_questions import queued_count, visible_decision, visible_path, check, path, live_count, question_open
from engine import atomic_write_json, load_json
from install import install
from hook import hook

SCREEN = '''  • Ran rg -n 'guarded.*build' infra/diagnostics | head -20
  └ 5 lines

• Working (19m 26s • esc to interrupt)

• Queued follow-up inputs
  ? 1 question
    shift+← to answer
'''

class CodexQuestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        install(self.root)
        self.cfg = load_json(self.root/'.agent-watchdog'/'config.json')
        atomic_write_json(self.root/'.agent-watchdog'/'worker.json',
                          {'type': 'tmux', 'agent': 'codex', 'outcome': 'launched',
                           'session': 'test-owned-session', 'window': '@2'})
        self.desktop = []
        self.phone = []
        def desktop(*args):
            self.desktop.append(args)
            return 'sent'
        def phone(*args):
            self.phone.append(args)
            return 'sent'
        self.desktop_sender = desktop
        self.phone_sender = phone
        self.cfg['alerts']['phone']['enabled'] = True

    def fake_tui(self, screen=SCREEN):
        def runner(cmd, **kwargs):
            if cmd[1] == 'list-panes':
                return SimpleNamespace(returncode=0, stdout='%2\n')
            if cmd[1] == 'capture-pane':
                return SimpleNamespace(returncode=0, stdout=screen)
            raise AssertionError(cmd)
        return runner

    def monitor(self, now, screen=SCREEN):
        with patch('codex_questions.session_name', return_value='test-owned-session'), \
             patch('codex_questions.tmux_exists', return_value=True):
            return check(self.root, self.cfg, now=now, run=self.fake_tui(screen),
                         desktop_sender=self.desktop_sender, phone_sender=self.phone_sender)

    def test_extract_live_footer(self):
        self.assertEqual(queued_count(SCREEN), 1)
        self.assertEqual(queued_count(SCREEN.replace('1 question', '2 questions')), 2)
        self.assertEqual(queued_count('\x1b[32m'+SCREEN+'\x1b[0m'), 1)

    def test_does_not_trigger_on_scrollback_or_ordinary_question(self):
        self.assertIsNone(queued_count(SCREEN+'\n'*24+'❯ Ask Codex'))
        self.assertIsNone(queued_count('May I deploy to test-host?\n • Working'))
        self.assertIsNone(queued_count('• Queued follow-up inputs\n ? 0 questions'))
        self.assertIsNone(queued_count('• 1 question\n nothing in queue'))

    def test_plain_idle_question_is_alerted_without_footer_or_stop(self):
        screen = ('• The local tests passed; no live deployment was attempted.\n'
                  '  Should I deploy to production?\n'
                  '\n› Ask Codex\n')
        self.assertEqual(visible_decision(screen)[0], 'Should I deploy to production?')
        event = self.monitor(1000, screen)
        self.assertIn('CODEX VISIBLE QUESTION', event)
        request = load_json(self.root/'.agent-watchdog/needs-input.json')
        self.assertEqual(request['agent'], 'codex')
        self.assertEqual(request['question'], 'Should I deploy to production?')
        self.assertEqual(request['status'], 'open')
        self.assertEqual(len(self.desktop), 0)  # existing poll sends via shared ledger
        from alerts import notify_pending_question
        self.assertIn('NEEDS USER INPUT', notify_pending_question(
            self.root, self.cfg, now=1001,
            desktop_sender=self.desktop_sender, phone_sender=self.phone_sender))
        self.assertEqual(len(self.desktop), 1)
        self.assertEqual(len(self.phone), 1)
        self.assertIsNone(self.monitor(1020, screen))
        self.assertEqual(len(self.desktop), 1)

    def test_formal_codex_decision_midturn_is_detected(self):
        screen = ('• Working (12m 1s • esc to interrupt)\n'
                  'USER_DECISION_REQUEST: Can I run the live test?\n'
                  '• Still reviewing safe local results\n'
                  '› Ask Codex\n')
        self.assertIn('CODEX VISIBLE QUESTION', self.monitor(1000, screen))
        self.assertEqual(load_json(self.root/'.agent-watchdog/needs-input.json')['question'],
                         'Can I run the live test?')

    def test_generic_questions_in_logs_and_user_prompts_do_not_alert(self):
        screens = [
            '└ Should I deploy to production?\n› Ask Codex',
            '• Ran grep tests\n└ Do you want to deploy?\n› Ask Codex',
            '• Working (19m 26s • esc to interrupt)\n'
            '  Should I deploy to production?\n› Ask Codex',
            '› Should I deploy to production?',
            '• Investigated whether the question is: Can I deploy?\n'
            '  The answer is no.\n› Ask Codex',
        ]
        for screen in screens:
            with self.subTest(screen=screen):
                self.assertIsNone(visible_decision(screen))

    def test_answered_question_does_not_reopen_until_new_episode(self):
        from alerts import clear_question
        screen = '• Finished the local audit.\n  Should I deploy to production?\n› Ask Codex\n'
        self.monitor(1000, screen)
        self.assertTrue(clear_question(self.root, 'codex'))
        self.assertIsNone(self.monitor(1020, screen))
        self.assertEqual(load_json(self.root/'.agent-watchdog/needs-input.json')['status'],
                         'answered')
        self.monitor(1040, '• Working\n› Ask Codex')
        self.monitor(1060, '• Working\n› Ask Codex')
        self.assertFalse(load_json(visible_path(self.root))['visible'])
        self.assertIn('CODEX VISIBLE QUESTION', self.monitor(1080, screen))

    def test_queued_question_alerts_desktop_and_phone_without_autoreply(self):
        message = self.monitor(1000)
        self.assertIn('CODEX QUEUED USER QUESTION (1)', message)
        self.assertEqual(len(self.desktop), 1)
        self.assertEqual(len(self.phone), 1)
        self.assertIn('Shift+Left', self.desktop[0][2])
        self.assertNotIn('test-host', self.phone[0][2])
        self.assertTrue(question_open(self.root))
        self.assertEqual(load_json(path(self.root))['count'], 1)
        self.assertFalse((self.root/'.agent-watchdog'/'needs-input.json').exists())

    def test_stable_count_only_one_alert_and_change_realerts(self):
        self.monitor(1000)
        self.assertIsNone(self.monitor(1020))
        self.assertIsNone(self.monitor(1400))
        self.assertEqual(len(self.desktop), 1)
        self.assertEqual(len(self.phone), 1)
        updated = SCREEN.replace('1 question', '2 questions')
        self.assertIn('QUESTION (2)', self.monitor(1420, updated))
        self.assertEqual(len(self.desktop), 2)

    def test_flicker_does_not_realert_but_new_episode_does(self):
        self.monitor(1000)
        self.monitor(1020, '• Working\n❯ Ask Codex')
        self.assertTrue(question_open(self.root))
        self.monitor(1040)
        self.assertEqual(len(self.desktop), 1)
        self.monitor(1060, '• Working\n❯ Ask Codex')
        self.monitor(1080, '• Working\n❯ Ask Codex')
        self.assertFalse(question_open(self.root))
        self.monitor(1100)
        self.assertEqual(len(self.desktop), 2)

    def test_failure_retries_only_failed_channel(self):
        calls = {'desktop':0, 'phone':0}
        def d(*args):
            calls['desktop'] += 1
            return 'sent'
        def p(*args):
            calls['phone'] += 1
            return 'phone unavailable' if calls['phone'] == 1 else 'sent'
        self.desktop_sender = d
        self.phone_sender = p
        self.monitor(1000)
        self.monitor(1010)
        self.assertEqual(calls, {'desktop':1,'phone':1})
        self.monitor(1130)
        self.assertEqual(calls, {'desktop':1,'phone':2})

    def test_no_unrelated_tmux_inspection(self):
        worker_file = self.root/'.agent-watchdog'/'worker.json'
        atomic_write_json(worker_file, {'type':'tmux','agent':'claude',
                                        'outcome':'launched','session':'test-owned-session','window':'@2'})
        with patch('codex_questions.tmux_exists', side_effect=AssertionError('should not inspect')):
            self.assertIsNone(check(self.root,self.cfg,now=1000))
        self.assertEqual(self.desktop, [])

    def test_no_alert_on_tmux_failure_no_dismissal(self):
        self.monitor(1000)
        with patch('codex_questions.session_name', return_value='test-owned-session'), \
             patch('codex_questions.tmux_exists', return_value=True):
            result = check(self.root, self.cfg, now=1010,
                           run=lambda *a, **k: SimpleNamespace(returncode=1,stdout=''),
                           desktop_sender=self.desktop_sender, phone_sender=self.phone_sender)
        self.assertIsNone(result)
        self.assertTrue(question_open(self.root))
        self.assertEqual(len(self.desktop), 1)

    def test_poll_includes_alert_without_stop_hook_or_input_marker(self):
        from watchdog import poll
        info = {'stage':'normal', 'used_percent':35., 'remaining_percent':65.,
                'limiting_window':'session', 'limiting_metric':'$.five_hour.used_percent',
                'windows':{}}
        with patch('watchdog.update', return_value=info), \
             patch('watchdog.check_codex_questions', return_value='CODEX QUEUED USER QUESTION (1): desktop=sent'), \
             patch('watchdog.telemetry_restored', return_value=False):
            events = poll(self.root, self.cfg, enable_launch=False)
        self.assertTrue(any('CODEX QUEUED USER QUESTION (1)' in e for e in events))
        self.assertFalse((self.root/'.agent-watchdog'/'needs-input.json').exists())

    def test_no_auto_continue_at_stop_for_queued_question(self):
        self.monitor(1000)
        info = {'stage':'normal','used_percent':42,'stale':False,'agent':'codex'}
        with patch('hook.current', return_value=info):
            self.assertEqual(hook(self.root,'codex','Stop','abc123',{'last_assistant_message': 'A task is done.'}), {})

    def test_dashboard_warns_about_queued_question(self):
        from console_ui import DashboardUI
        self.monitor(1000)
        ui=DashboardUI(self.root)
        text,colour=ui._pending()
        self.assertIn('Codex has 1 queued question',text)
        self.assertIn('Shift+Left',text)
        self.assertEqual(colour,'yellow')

    def test_installer_contains_monitor_and_preserves_it_on_reinstall(self):
        self.assertTrue((self.root/'.agent-watchdog'/'codex_questions.py').is_file())
        self.monitor(1000)
        install(self.root)
        self.assertTrue(question_open(self.root))
        self.assertIn('codex-questions.json', (self.root/'.gitignore').read_text())

if __name__ == '__main__':
    unittest.main()
