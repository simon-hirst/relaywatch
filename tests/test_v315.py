"""v3.15: actual test-host monitoring transcript should not spin Claude's Stop hook."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alerts import pending_question
from background_wait import detect_background_wait, current, reports_background_completion
from engine import atomic_write_json, load_json
from hook import hook
from install import install

STATUS_MESSAGES = (
    "The records now say the earlier result covered only the first switch-on. "
    "The follow-up run (existing service, 13 Oct content, 10 preview cycles) finishes "
    "around 22:58 UTC. I'll report from its watch.",
    "The handoff is checkpointed. The content run is still in progress on test-host, "
    "and I'm waiting for its watch to finish.",
    "The content run is still measuring on test-host (expected to finish around "
    "22:58 UTC). Its result decides the next step and nothing else may use "
    "that host meanwhile.",
    "It's only 22:50 UTC; the run began at 22:49. Nothing to do until its watch returns.",
)

class HostWaitRegression(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        with contextlib.redirect_stdout(io.StringIO()): install(self.root)
        atomic_write_json(self.root/'.agent-watchdog/worker.json',
                          {'agent':'claude','type':'tmux','outcome':'launched','window':'@2'})
        (self.root/'docs/HANDOFF.md').write_text('The test-host watch is active. '*20)

    def hook(self,event,payload,agent='claude',stage='normal',session='active'):
        with patch('hook.current',return_value={'stage':stage,'agent':agent,'used_percent':48}):
            return hook(self.root,agent,event,session,payload)

    def stop(self,message,stage='normal',session='active'):
        return self.hook('Stop',{'last_assistant_message':message},stage=stage,session=session)

    def test_each_real_status_is_a_wait_without_question(self):
        for msg in STATUS_MESSAGES:
            with self.subTest(msg=msg):
                self.assertTrue(detect_background_wait(msg),msg)
                self.assertEqual(self.stop(msg),{})
                self.assertEqual(current(self.root)['status'],'waiting')
                self.assertEqual(pending_question(self.root),{})
        self.assertEqual(load_json(self.root/'.agent-watchdog/auto-continue-state.json',{}),{})

    def test_checkpoint_tool_does_not_restart_stop_loop(self):
        self.stop(STATUS_MESSAGES[0])
        self.hook('PreToolUse',{'tool_name':'Bash'})
        self.hook('PostToolUse',{'tool_name':'Bash'})
        self.assertEqual(current(self.root)['status'],'waiting')
        self.assertEqual(self.stop('The handoff has been checkpointed.'),{})
        self.assertEqual(self.stop(STATUS_MESSAGES[2]),{})
        self.assertFalse(pending_question(self.root))

    def test_explicit_completion_releases_wait(self):
        self.stop(STATUS_MESSAGES[1])
        self.assertTrue(reports_background_completion('The watch has completed and all test instances were restored.'))
        self.assertTrue(reports_background_completion('BACKGROUND_JOB_COMPLETE: test-host measurement returned'))
        result=self.stop('The watch has completed and all test instances were restored.')
        self.assertEqual(result['decision'],'block')
        self.assertEqual(current(self.root),{})

    def test_manual_message_clears_wait_auto_continue_does_not(self):
        self.stop(STATUS_MESSAGES[0])
        self.hook('UserPromptSubmit',{'prompt':'CONTINUE WITH NEXT TASK. AUTOMATIC WORKFLOW CONTINUATION.'})
        self.assertEqual(current(self.root)['status'],'waiting')
        self.hook('UserPromptSubmit',{'prompt':'The test returned; continue with safe local review.'})
        self.assertEqual(current(self.root),{})

    def test_actual_async_result_summaries_unlock(self):
        for message in ('Geometry results show the preview is stable.',
                        'The restoration was verified on test-host.',
                        'The watch returned and production settings were restored.'):
            with self.subTest(message=message):
                self.stop(STATUS_MESSAGES[1])
                self.assertTrue(reports_background_completion(message))
                self.assertEqual(self.stop(message)['decision'],'block')

    def test_quota_handoff_still_takes_priority(self):
        result=self.stop(STATUS_MESSAGES[0],stage='handoff')
        self.assertEqual(result['decision'],'block')

    def test_genuine_permission_is_still_recorded(self):
        self.stop(STATUS_MESSAGES[1])
        self.assertEqual(self.stop('USER_DECISION_REQUEST: Can I pause both service test instances?'),{})
        self.assertIn('pause both service test instances',pending_question(self.root)['question'])

    def test_not_false_positive_on_plan_or_past_log(self):
        for message in (
            'I will run the tests tomorrow.',
            'The test finished and the next step is local cleanup.',
            'Quota is exhausted, waiting for reset.',
            '```\nThe content run is still measuring; waiting for its watch.\n```',
            'The doc contains the phrase "content run is still measuring" and a plan to wait for results.',
            'Do not wait for the background test. Continue locally.',
        ):
            with self.subTest(message=message):
                self.assertIsNone(detect_background_wait(message))

    def test_other_worker_does_not_inherit_previous_wait(self):
        self.stop(STATUS_MESSAGES[0])
        self.assertEqual(self.stop('Local tests complete.',session='different')['decision'],'block')

if __name__=='__main__':unittest.main()
