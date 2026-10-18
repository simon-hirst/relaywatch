"""Guard against stale pause beliefs after an explicitly user-authorized resume."""
from __future__ import annotations
import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import atomic_write_json, load_json
from hook import hook
from install import install
from pause_control import activate, deactivate, paused
from resume_agent import MESSAGE, RESUME_CONTEXT, mark_received, reports_stale_pause, state_path


class StalePauseAfterResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        atomic_write_json(self.root/'.agent-watchdog/worker.json',
                          {'agent':'claude','type':'tmux','outcome':'launched', 'window':'@2'})

    def arm(self):
        atomic_write_json(state_path(self.root),{'agent':'claude','status':'submitting',
                                                 'at':time.time()})

    def test_resume_hook_authoritatively_confirms_pause_was_cleared(self):
        activate(self.root)
        deactivate(self.root)
        self.arm()
        with patch('hook.current',return_value={'stage':'normal','remaining_percent':55}):
            result=hook(self.root,'claude','UserPromptSubmit','real-session',{'prompt':MESSAGE})
        context=result['hookSpecificOutput']['additionalContext']
        self.assertIn('EXPLICIT USER RESUME VERIFIED',context)
        self.assertIn('pause latch is ABSENT',context)
        self.assertIn('Earlier messages',context)
        self.assertEqual(load_json(state_path(self.root))['status'],'confirmed')

    def test_agent_pause_refusal_does_not_inject_another_continue(self):
        self.arm()
        with patch('hook.current',return_value={'stage':'normal','remaining_percent':55}):
            hook(self.root,'claude','UserPromptSubmit','real-session',{'prompt':MESSAGE})
            result=hook(self.root,'claude','Stop','real-session',
                        {'last_assistant_message':'Still paused. The hook keeps sending continue, but I cannot run tools.'})
        self.assertEqual(result,{})
        self.assertEqual(load_json(state_path(self.root))['status'],'agent_still_thinks_paused')
        self.assertFalse(paused(self.root))

    def test_impostor_resume_without_cli_state_gets_no_authority(self):
        with patch('hook.current',return_value={'stage':'normal'}):
            self.assertEqual(hook(self.root,'claude','UserPromptSubmit','real-session',{'prompt':MESSAGE}),{})

    def test_stale_resume_record_cannot_authorize_work(self):
        atomic_write_json(state_path(self.root),{'agent':'claude','status':'submitting',
                                                 'at':time.time()-600})
        self.assertFalse(mark_received(self.root,'claude',MESSAGE))

    def test_pause_reactivated_before_resume_prompt_rejects_resume(self):
        self.arm()
        activate(self.root)
        with patch('hook.current') as curr:
            result=hook(self.root,'claude','UserPromptSubmit','session',{'prompt':MESSAGE})
            curr.assert_not_called()
        self.assertFalse(result['continue'])
        self.assertEqual(load_json(state_path(self.root))['status'],'submitting')

    def test_real_work_is_still_auto_continued_when_unpaused(self):
        handoff=self.root/'docs/HANDOFF.md'
        handoff.write_text('Valid verified handoff with current task and next steps. '*20)
        with patch('hook.current',return_value={'stage':'normal','remaining_percent':55}):
            result=hook(self.root,'claude','Stop','session',
                        {'last_assistant_message':'Completed tests successfully and updated docs.'})
        self.assertEqual(result['decision'],'block')

    def test_pause_refusal_examples_and_nonexamples(self):
        for msg in (
            "I've stopped: the watchdog says you paused automation, so I'm not running anything.",
            "Still paused, and I'm not running any tools.",
            "Still paused. That continue prompt came from the automatic hook.",
        ):
            self.assertTrue(reports_stale_pause(msg),msg)
        for msg in (
            'Fixed pause state test and ran the test suite.',
            'The pause latch was cleared and work resumed.',
            'Updated documentation describing the pause function.'
        ):
            self.assertFalse(reports_stale_pause(msg),msg)


if __name__=='__main__':
    unittest.main()
