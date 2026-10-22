"""v3.13: do not lose approvals mentioned in natural language."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alerts import detect_question, extract_question, is_automatic_continuation, notify_pending_question, pending_question
from engine import atomic_write_json, load_json
from hook import hook
from install import install

SAMPLE = ("Decision for you: can I run the two-arm geometry test on test-host? "
          "It would take 20 guarded minutes at most, with both idle test instances paused "
          "and then restored, exactly as in the last diagnostic. Until you decide, "
          "I'll carry on with the remaining local security and cleanup review.")


class QuestionsV313(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        self.cfg = load_json(self.root/'.agent-watchdog/config.json')
        self.cfg['alerts']['phone']['enabled'] = True
        atomic_write_json(self.root/'.agent-watchdog/worker.json',
                          {'agent':'claude', 'type':'tmux','outcome':'launched','window':'@2'})

    def stop(self, message, agent='claude', stage='normal'):
        with patch('hook.current', return_value={'stage':stage, 'agent':agent, 'used_percent':50}):
            return hook(self.root,agent,'Stop','session-1',
                        {'last_assistant_message':message, 'stop_hook_active':False})

    def submit(self, message):
        with patch('hook.current', return_value={'stage':'normal','agent':'claude'}):
            return hook(self.root,'claude','UserPromptSubmit','session-1',{'prompt':message})

    def test_exact_realworld_question_detected_and_no_auto_continue(self):
        self.assertEqual(self.stop(SAMPLE),{})
        item=pending_question(self.root)
        self.assertEqual(item['source'],'agent_informal_decision')
        self.assertIn('can I run the two-arm geometry test on test-host?',item['question'])
        self.assertNotIn('Until you decide', item['question'])
        self.assertEqual(self.stop('Verified 3 other local tests.'),{})

    def test_question_notified_once_without_granting_approval(self):
        self.stop(SAMPLE)
        desktop=[]; mobile=[]
        def d(*args): desktop.append(args[-1]);return 'sent'
        def p(*args): mobile.append(args[-1]);return 'sent'
        first=notify_pending_question(self.root,self.cfg,now=1000,
                                      desktop_sender=d,phone_sender=p)
        self.assertIn('NEEDS USER INPUT',first)
        self.assertEqual(len(desktop),1)
        self.assertIn('two-arm geometry test',desktop[0])
        self.assertEqual(len(mobile),1)
        self.assertNotIn('two-arm geometry test',mobile[0])
        self.assertIsNone(notify_pending_question(self.root,self.cfg,now=1025,
                          desktop_sender=d,phone_sender=p))
        self.assertEqual(len(desktop),1)
        self.assertEqual(pending_question(self.root)['status'],'open')

    def test_formal_marker_and_markdown_form(self):
        for message in (
            'USER_DECISION_REQUEST: Can I restart both idle service test instances for a trial?',
            '**USER_DECISION_REQUEST:** Can I restart both idle service test instances for a trial?',
            '- USER_DECISION_REQUEST: Can I restart both idle service test instances for a trial?',
            'Finished local test.\nNEEDS_USER_INPUT: Can I restart both idle service test instances for a trial?',
        ):
            with self.subTest(message=message):
                self.assertIn('restart both idle service test instances',extract_question(message))
                self.assertEqual(detect_question(message)[1], 'agent_formal')
                self.assertEqual(self.stop(message),{})
                self.assertEqual(pending_question(self.root)['source'],'agent_formal')

    def test_multiline_bold_heading(self):
        text='**Decision for you:** May I run the guarded test-host trial\nwith the two idle test instances briefly paused?\n\nI can do local review meanwhile.'
        question, source=detect_question(text)
        self.assertEqual(source,'agent_informal_decision')
        self.assertIn('briefly paused?',question)

    def test_informal_detection_is_conservative(self):
        ignored = (
            'Is this okay? I can proceed with local tests.',
            'I updated docs describing \"Decision for you: can I run it?\"',
            'We do not need approval for these purely local tests.',
            '```\nDecision for you: can I restart live service?\n```',
            '```\nUSER_DECISION_REQUEST: May I restart the test instances?\n```',
            '> USER_DECISION_REQUEST: May I restart the test instances?',
            '> Decision for you: can I restart live service?',
            'Decision for you: All planned code changes are completed.',
            'Question for you: I will run the formatter automatically.',
            'Debug: permission request: can I deploy?',
        )
        for message in ignored:
            with self.subTest(message=message):
                self.assertEqual(detect_question(message),(None,None))
        self.assertEqual(self.stop('Verified docs and tests.')['decision'],'block')

    def test_watchdog_resume_and_continue_do_not_answer_approval(self):
        self.stop(SAMPLE)
        for message in ('', 'continue','resume', 'USER-REQUESTED RESUME. Read docs/HANDOFF.md and continue.'):
            with self.subTest(message=message):
                self.submit(message)
                self.assertEqual(pending_question(self.root)['status'],'open')
        self.assertTrue(is_automatic_continuation('continue'))
        self.submit('No, do not run that test-host trial. Keep everything local.')
        self.assertEqual(load_json(self.root/'.agent-watchdog/needs-input.json')['status'],'answered')

    def test_pending_approval_visible_in_dashboard(self):
        from console_ui import DashboardUI
        ui=DashboardUI(self.root)
        self.stop(SAMPLE)
        self.assertIn('Claude needs approval',ui._pending()[0])
        self.assertIn('status',ui._pending()[0])

    def test_user_pause_beats_decisions(self):
        (self.root/'.agent-watchdog/PAUSE_AUTO_CONTINUE').touch()
        out=self.stop(SAMPLE)
        self.assertIs(out['continue'],False)
        self.assertEqual(pending_question(self.root),{})

    def test_idempotent_install_preserves_workflow_changes(self):
        workflow=self.root/'docs/WORKFLOW.md'
        content=workflow.read_text()
        self.assertIn('USER_DECISION_REQUEST:',content)
        workflow.write_text(content+'\nCUSTOM RULE FOR MY PROJECT\n')
        with contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        updated=workflow.read_text()
        self.assertIn('CUSTOM RULE FOR MY PROJECT',updated)
        self.assertEqual(updated.count('<!-- BEGIN WATCHDOG-DECISION-PROTOCOL'),1)
        self.assertIn('USER_DECISION_REQUEST:',(self.root/'CLAUDE.md').read_text())
        self.assertIn('USER_DECISION_REQUEST:',(self.root/'AGENTS.md').read_text())
        from runner import START_PROMPT
        self.assertIn('USER_DECISION_REQUEST:',START_PROMPT)
        self.assertIn('USER_DECISION_REQUEST:',(self.root/'.agent-watchdog/hook.py').read_text())


if __name__=='__main__':
    unittest.main()
