"""v3.14: waiting for a background job is not a user question/stall."""
from __future__ import annotations
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alerts import register_question, pending_question, notify_pending_question
from background_wait import detect_background_wait, current, finish
from console_ui import DashboardUI
from engine import atomic_write_json, load_json
from hook import hook
from install import install

FIRST = ("The session summary is updated. Arm 1 is still being measured, "
         "and no other local work makes sense until it finishes. "
         "When the background watch reports, I'll check arm 1's restoration "
         "and geometry, then launch arm 2.")
SECOND = ("The geometry step comes after the first 120 s hold, around 22:23 UTC, "
          "and the watch is still waiting for it. I'll wait for its notification "
          "instead of polling.")

class BackgroundWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()): install(self.root)
        atomic_write_json(self.root/'.agent-watchdog/worker.json',
                          {'agent':'claude','type':'tmux','outcome':'launched','window':'@2'})
        self.handoff=self.root/'docs/HANDOFF.md'
        self.handoff.write_text('Still checking background geometry. '*15)

    def stop(self,message,stage='normal'):
        with patch('hook.current',return_value={'stage':stage,'agent':'claude','used_percent':32}):
            return hook(self.root,'claude','Stop','same-session',
                        {'last_assistant_message':message,'stop_hook_active':False})

    def test_realworld_statuses_not_questions_and_not_forced(self):
        for sample in (FIRST, SECOND):
            self.assertIsNone(pending_question(self.root).get('question'))
            self.assertEqual(self.stop(sample),{})
            self.assertEqual(current(self.root)['status'],'waiting')
            self.assertEqual(self.stop(sample),{})
        self.assertEqual(load_json(self.root/'.agent-watchdog/auto-continue-state.json',{}),{})
        self.assertEqual(pending_question(self.root),{})

    def test_explicit_marker(self):
        sample='Tests still run.\nWAITING_ON_BACKGROUND: arm1 geometry watch; expect async process completion'
        self.assertIn('arm1 geometry',detect_background_wait(sample))
        self.assertEqual(self.stop(sample),{})
        self.assertIn('waiting for background job',DashboardUI(self.root)._pending()[0])

    def test_no_false_alert_for_background_wait(self):
        self.stop(FIRST)
        with patch('alerts._windows_desktop',side_effect=AssertionError('desktop alert called')):
            self.assertIsNone(notify_pending_question(self.root,{'alerts':{'desktop':True}},
                desktop_sender=lambda *args: self.fail('false question alert')))

    def test_obsolete_watchdog_stall_dismissed_but_real_approval_preserved(self):
        register_question(self.root,'claude',
            'The agent stopped after repeated continuations without updating the handoff. Please review.',
            source='watchdog_auto_continue_stall')
        self.stop(FIRST)
        self.assertEqual(pending_question(self.root),{})
        self.assertEqual(load_json(self.root/'.agent-watchdog/needs-input.json')['acknowledged_by'],
                         'background_wait_detected')
        self.stop('USER_DECISION_REQUEST: May I run another live pilot trial?')
        self.stop(SECOND)
        self.assertTrue(pending_question(self.root))

    def test_background_wait_clears_stale_no_progress_count(self):
        statefile=self.root/'.agent-watchdog/auto-continue-state.json'
        atomic_write_json(statefile,{'claude:same-session':{'stalled':2,'signature':['old']}})
        self.stop(FIRST)
        self.assertNotIn('claude:same-session',load_json(statefile))

    def test_followup_tool_activity_preserves_wait_until_result_or_user(self):
        self.stop(SECOND)
        with patch('hook.current',return_value={'stage':'normal','agent':'claude'}):
            hook(self.root,'claude','PostToolUse','same-session',{'tool_name':'Bash'})
        # Checkpointing a document while a watch is running is not job completion.
        self.assertEqual(current(self.root)['status'],'waiting')
        with patch('hook.current',return_value={'stage':'normal','agent':'claude'}):
            hook(self.root,'claude','UserPromptSubmit','same-session',{'prompt':'The job finished; proceed safely.'})
        self.assertEqual(current(self.root),{})

    def test_genuine_question_still_blocks(self):
        self.assertEqual(self.stop('USER_DECISION_REQUEST: May I pause two test instances to measure test-host?'),{})
        self.assertTrue(pending_question(self.root))
        self.assertIn('needs approval',DashboardUI(self.root)._pending()[0])

    def test_external_run_not_user_question(self):
        examples=('Waiting for the Codex quota reset.',
                  'Need your approval for the background test?',
                  'Do not wait for the background task; finish local work.',
                  '```\nWAITING_ON_BACKGROUND: fake background watch\n```')
        for text in examples:
            with self.subTest(text=text):
                self.assertIsNone(detect_background_wait(text))

    def test_stall_notification_is_not_mislabeled_question(self):
        register_question(self.root,'claude',
            'The agent stopped after repeated continuations without updating the handoff. Please review.',
            source='watchdog_auto_continue_stall')
        titles=[]
        output=notify_pending_question(self.root,{'alerts':{'desktop':True}},now=1000,
            desktop_sender=lambda root,title,body:(titles.append(title) or 'sent'))
        self.assertIn('AGENT STALLED',output)
        self.assertIn('stopped making progress',titles[0])
        self.assertIn('stalled',DashboardUI(self.root)._pending()[0])

    def test_90pct_handoff_precedes_background_wait(self):
        # The existing mandatory 90% handoff cannot be bypassed by a wait marker.
        out=self.stop('WAITING_ON_BACKGROUND: running local tests',stage='handoff')
        self.assertEqual(out.get('decision'),'block')

    def test_reinstall_contains_new_protocol(self):
        with contextlib.redirect_stdout(io.StringIO()): install(self.root)
        self.assertTrue((self.root/'.agent-watchdog/background_wait.py').exists())
        self.assertIn('WAITING_ON_BACKGROUND:',(self.root/'CLAUDE.md').read_text())
        self.assertIn('WAITING_ON_BACKGROUND:',(self.root/'AGENTS.md').read_text())
        self.assertIn('WAITING_ON_BACKGROUND:',(self.root/'docs/WORKFLOW.md').read_text())
        from runner import START_PROMPT
        self.assertIn('WAITING_ON_BACKGROUND:',START_PROMPT)

if __name__=='__main__': unittest.main()
