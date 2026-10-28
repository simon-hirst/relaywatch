"""Native Claude question dialogs must alert without answering for the user."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alerts import request_path
from engine import atomic_write_json, load_json
from hook import hook, native_claude_decision
from install import install
from runner import session_name


class NativeClaudeDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        install(self.root)
        atomic_write_json(self.root / '.agent-watchdog' / 'worker.json', {
            'type': 'tmux', 'agent': 'claude', 'outcome': 'launched',
            'session': session_name(self.root), 'window': '@3'})
        self.question = {
            'tool_name': 'AskUserQuestion',
            'tool_input': {
                'questions': [
                    {'question': 'Which fix should I investigate?',
                     'header': 'Next test', 'multiSelect': False,
                     'options': [{'label':'v7','description':'Try the local lab only'},
                                 {'label':'Wait','description':'No more changes'}]},
                    {'question': 'May I run further local tests?',
                     'header': 'Testing', 'multiSelect': False,
                     'options': []}
                ]},
            'session_id': 'claude-managed'}

    def send(self, event, payload):
        with patch('hook.tmux_exists', return_value=True), \
             patch('hook.notify_pending_question') as delivered, \
             patch('hook.current', side_effect=AssertionError('native dialog should not need quota API')):
            result = hook(self.root, 'claude', event, 'claude-managed', payload)
        return result, delivered

    def test_native_question_alerts_immediately_preserving_choice(self):
        reply, delivered = self.send('PreToolUse', self.question)
        self.assertEqual(reply, {})
        delivered.assert_called_once()
        item = load_json(request_path(self.root))
        self.assertEqual(item['agent'], 'claude')
        self.assertEqual(item['status'], 'open')
        self.assertEqual(item['source'], 'claude_native_AskUserQuestion')
        self.assertIn('Which fix should I investigate?', item['question'])
        self.assertIn('May I run further local tests?', item['question'])
        self.assertNotIn('v7', item['question'])  # No option selected.
        self.assertEqual(self.send('PreToolUse', self.question)[1].call_count, 0)

    def test_tool_completion_marks_only_its_native_question_answered(self):
        self.send('PreToolUse', self.question)
        reply, delivered = self.send('PostToolUse', {'tool_name':'AskUserQuestion'})
        self.assertEqual(reply, {})
        self.assertEqual(delivered.call_count, 0)
        self.assertEqual(load_json(request_path(self.root))['status'], 'answered')
        self.assertEqual(self.send('PreToolUse', self.question)[1].call_count, 1)

    def test_plan_approval_is_not_granted_by_notification(self):
        reply, delivered = self.send('PreToolUse', {
            'tool_name':'ExitPlanMode','tool_input':{'plan':'Deploy to production?'}})
        self.assertEqual(reply, {})
        delivered.assert_called_once()
        item = load_json(request_path(self.root))
        self.assertEqual(item['source'], 'claude_native_ExitPlanMode')
        self.assertNotIn('Deploy to production?', item['question'])
        self.assertEqual(self.send('PostToolUse', {'tool_name':'ExitPlanMode'})[0], {})
        self.assertEqual(load_json(request_path(self.root))['status'], 'answered')

    def test_unrelated_tools_do_not_generate_decisions(self):
        self.assertIsNone(native_claude_decision({'tool_name':'Bash','tool_input':{'command':'echo x'}}))
        with patch('hook.notify_pending_question') as delivery, \
             patch('hook.current', return_value={'stage':'normal','stale':False}):
            reply = hook(self.root,'claude','PreToolUse','claude-managed',
                         {'tool_name':'Bash','tool_input':{}})
        self.assertEqual(reply, {})
        delivery.assert_not_called()
        self.assertFalse(request_path(self.root).exists())

    def test_unowned_session_cannot_emit_claude_notifications(self):
        atomic_write_json(self.root/'.agent-watchdog/worker.json', {
            'type':'tmux','agent':'codex','outcome':'launched','session':session_name(self.root),'window':'@3'})
        with patch('hook.tmux_exists', side_effect=AssertionError('do not inspect unrelated tmux')), \
             patch('hook.notify_pending_question') as delivered, \
             patch('hook.current', return_value={'stage':'normal','stale':False}):
            hook(self.root,'claude','PreToolUse','other-session',self.question)
        delivered.assert_not_called()
        self.assertFalse(request_path(self.root).exists())

    def test_malformed_native_question_still_reports_without_leaking_details(self):
        reply, delivered = self.send('PreToolUse', {
            'tool_name':'AskUserQuestion','tool_input':{'questions':[]}})
        self.assertEqual(reply, {})
        delivered.assert_called_once()
        self.assertIn('interactive question picker',load_json(request_path(self.root))['question'])

    def test_installer_preserves_native_pretool_hook_after_reinstall(self):
        for _ in range(2):
            install(self.root)
        data = load_json(self.root/'.claude/settings.local.json')
        hooks = data['hooks']['PreToolUse']
        commands = [h['command'] for entry in hooks for h in entry['hooks']]
        managed = [cmd for cmd in commands if '/.agent-watchdog/hook.py' in cmd]
        self.assertEqual(len(managed), 1)  # matcherless, covers AskUserQuestion too.


if __name__ == '__main__':
    unittest.main()
