import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
from engine import load_json, atomic_write_json
from alerts import request_path, notify_pending_question, clear_question
from claude_questions import visible_question, check, path as live_path
from hook import hook
from install import install
from watchdog import poll


class ClaudeQuestionTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name)
        install(self.root)
        self.config = load_json(self.root / '.agent-watchdog/config.json')
        self.config['alerts']['desktop'] = False
        self.config['alerts']['phone']['enabled'] = False
        atomic_write_json(self.root/'.agent-watchdog/worker.json', {
            'agent': 'claude', 'outcome': 'launched', 'type': 'tmux',
            'window': '@2', 'session': 'owned'})
        self.screen = ('● Working\nUSER_DECISION_REQUEST: Can I run the test-host trial?'
                       '\nUntil then I can review local tests.\n❯ ')

    def scan(self, *, now=100, screen=None):
        view = self.screen if screen is None else screen
        def fake_run(cmd, **kwargs):
            if cmd[1] == 'list-panes': return SimpleNamespace(returncode=0,stdout='%2\n')
            return SimpleNamespace(returncode=0,stdout=view)
        with patch('claude_questions.session_name', return_value='owned'), \
             patch('claude_questions.tmux_exists', return_value=True):
            return check(self.root, self.config, now=now, run=fake_run)

    def test_live_question_caught_without_stop(self):
        self.assertIn('CLAUDE QUESTION',self.scan())
        question=load_json(request_path(self.root))
        self.assertEqual(question['question'], 'Can I run the test-host trial?')
        self.assertEqual(question['source'], 'claude_live_agent_formal')
        self.assertIsNone(self.scan(now=120))
        self.assertEqual(question['created_at'],load_json(request_path(self.root))['created_at'])

    def test_answered_stays_answered_when_marker_remains(self):
        self.scan()
        clear_question(self.root,'claude')
        for t in (120,140,160):self.scan(now=t)
        self.assertEqual(load_json(request_path(self.root))['status'],'answered')

    def test_stop_question_preceding_first_scan_remains_one_episode(self):
        from alerts import register_question
        register_question(self.root,'claude','Can I run the test-host trial?',now=50)
        self.scan()
        self.assertEqual(load_json(request_path(self.root))['created_at'],50)

    def test_answered_stop_question_still_visible_is_not_reopened(self):
        from alerts import register_question
        register_question(self.root,'claude','Can I run the test-host trial?',now=50)
        clear_question(self.root,'claude')
        self.scan()
        self.assertEqual(load_json(request_path(self.root))['status'],'answered')

    def test_no_arbitrary_question_or_code_example(self):
        self.assertIsNone(visible_question('Should I test?\n❯'))
        self.assertIsNone(visible_question('```\nUSER_DECISION_REQUEST: fictional?\n```\n❯'))
        self.assertIsNone(visible_question('> USER_DECISION_REQUEST: in document?\n❯'))

    def test_no_nonmanaged_pane_reading(self):
        atomic_write_json(self.root/'.agent-watchdog/worker.json', {
            'agent':'codex','outcome':'launched','type':'tmux','session':'owned','window':'@2'})
        with patch('claude_questions.tmux_exists', side_effect=AssertionError('must not inspect')):
            self.assertIsNone(check(self.root,self.config))

    def test_tmux_unavailable_does_not_clear_question(self):
        self.scan()
        with patch('claude_questions.session_name', return_value='owned'), \
             patch('claude_questions.tmux_exists', return_value=False):
            self.assertIsNone(check(self.root,self.config))
        self.assertEqual(load_json(request_path(self.root))['status'],'open')

    def test_stop_push_immediately_using_shared_delivery_path(self):
        payload={'last_assistant_message':'USER_DECISION_REQUEST: May I pause the test instances?'}
        with patch('hook.current',return_value={'stage':'normal','stale':False}), \
             patch('hook.session_name',return_value='owned'), \
             patch('hook.tmux_exists',return_value=True), \
             patch('hook.notify_pending_question',return_value='NEEDS USER INPUT from claude: phone=sent') as alert:
            hook(self.root,'claude','Stop','claude123',payload)
        alert.assert_called_once()
        self.assertEqual(load_json(request_path(self.root))['question'],'May I pause the test instances?')

    def test_duplicate_poll_alert_channels_are_not_repeated(self):
        self.scan()
        calls={'desktop':0,'phone':0}
        def d(*a):calls['desktop']+=1;return 'sent'
        def p(*a):calls['phone']+=1;return 'sent'
        self.config['alerts']['desktop']=True
        self.config['alerts']['phone']['enabled']=True
        notify_pending_question(self.root,self.config,now=150,desktop_sender=d,phone_sender=p)
        notify_pending_question(self.root,self.config,now=180,desktop_sender=d,phone_sender=p)
        self.assertEqual(calls, {'desktop':1,'phone':1})

    def test_poller_calls_live_claude_monitor(self):
        info={'stage':'normal', 'used_percent':25.,'remaining_percent':75.,
              'limiting_window':'session', 'windows':{}}
        with patch('watchdog.update',return_value=info), \
             patch('watchdog.telemetry_restored',return_value=False), \
             patch('watchdog.check_claude_questions', return_value='CLAUDE QUESTION serviceERVED: queued for desktop/phone alerts'), \
             patch('watchdog.check_codex_questions',return_value=None), \
             patch('watchdog.check_daybreak',return_value=None):
            lines=poll(self.root,self.config,enable_launch=False)
        self.assertTrue(any('CLAUDE QUESTION serviceERVED' in line for line in lines))


if __name__ == '__main__':unittest.main()
