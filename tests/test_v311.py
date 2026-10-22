"""v3.11 - bounded quota-wait correction, with user-safety precedence."""
from __future__ import annotations
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import atomic_write_json, load_json
from hook import hook
from install import install
from quota_behavior import premature_quota_wait


class QuotaWaitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        self.handoff=self.root/'docs/HANDOFF.md'
        self.handoff.write_text('Active plan and next safe local tasks. '*20)
        atomic_write_json(self.root/'.agent-watchdog/worker.json',
                          {'type':'tmux','agent':'claude','outcome':'launched','window':'@2'})

    def stop(self, used=86, agent='claude', message="I'll wait for my quota to reset.", **extra):
        state={'stage':'normal' if used<80 else 'prepare' if used<90 else 'handoff',
               'agent':agent, 'used_percent':used, 'remaining_percent':100-used,
               **extra}
        with patch('hook.current',return_value=state):
            return hook(self.root,agent,'Stop','managed',
                        {'last_assistant_message':message, 'stop_hook_active':False})

    def test_typical_quota_wait_language(self):
        for line in ("I'll wait for the 5-hour quota reset.",
                     "I'm going to wait until the usage limit resets.",
                     'We should pause until our quota resets.',
                     'Waiting for the next session limit reset.',
                     'The quota is low so I will wait for the reset.',
                     'I’m waiting for the quota reset.'):
            self.assertTrue(premature_quota_wait(line),line)

    def test_negations_descriptions_and_real_user_blocks_not_detected(self):
        for line in ("I won't wait for the quota to reset; continuing work.",
                     "I'm not waiting for the quota reset; tests are running.",
                     'The watchdog will wait for quota reset after the handoff.',
                     'I am waiting for the user to respond about quota allocations.',
                     'A user pause always outranks quota instructions.',
                     '> I am waiting for quota reset.',
                     '```\nI am waiting for quota reset.\n```',
                     'The five-hour quota resets at 13:00; continuing local work.'):
            self.assertFalse(premature_quota_wait(line),line)

    def test_prepare_stage_corrects_premature_quota_wait(self):
        result=self.stop(87)
        self.assertEqual(result['decision'],'block')
        self.assertIn('PREMATURE QUOTA WAIT DETECTED',result['reason'])
        self.assertIn('87.0%',result['reason'])
        self.assertIn('short',result['reason'])
        self.assertIn('Do not waste tokens',result['reason'])

    def test_normal_stage_also_corrects_quota_wait(self):
        self.assertIn('PREMATURE QUOTA WAIT',self.stop(72)['reason'])

    def test_codex_also_corrected(self):
        atomic_write_json(self.root/'.agent-watchdog/worker.json',
                          {'type':'tmux','agent':'codex','outcome':'launched','window':'@1'})
        self.assertIn('PREMATURE QUOTA WAIT',self.stop(85,agent='codex')['reason'])

    def test_90_percent_never_triggers_resume_work(self):
        response=self.stop(92)
        self.assertIn('URGENT USAGE WATCHDOG',response['reason'])
        self.assertNotIn('PREMATURE QUOTA WAIT',response['reason'])

    def test_stale_usage_no_correction(self):
        self.assertEqual(self.stop(86,stale=True),{})

    def test_explicit_user_pause_wins(self):
        (self.root/'.agent-watchdog/PAUSE_AUTO_CONTINUE').touch()
        self.assertFalse(self.stop(86).get('continue',True))

    def test_pending_genuine_question_wins(self):
        atomic_write_json(self.root/'.agent-watchdog/needs-input.json',
                          {'agent':'claude','status':'open','question':'Approve deployment?'})
        self.assertEqual(self.stop(86),{})

    def test_no_unmanaged_continuation(self):
        atomic_write_json(self.root/'.agent-watchdog/worker.json',
                          {'type':'tmux','agent':'codex','outcome':'launched','window':'@1'})
        self.assertEqual(self.stop(86),{})

    def test_repeated_refusal_stops_and_registers_no_progress(self):
        self.assertIn('PREMATURE',self.stop(86)['reason'])
        self.assertIn('PREMATURE',self.stop(86)['reason'])
        self.assertEqual(self.stop(86),{})
        q=load_json(self.root/'.agent-watchdog/needs-input.json')
        self.assertEqual(q['source'],'watchdog_auto_continue_stall')
        self.assertEqual(q['status'],'open')

    def test_actual_work_gets_normal_continue(self):
        response=self.stop(86,message='Verified tests and updated documentation.')
        self.assertEqual(response['decision'],'block')
        self.assertIn('CONTINUE WITH NEXT TASK',response['reason'])

    def test_install_adds_new_module_and_updated_managed_instructions(self):
        self.assertTrue((self.root/'.agent-watchdog/quota_behavior.py').exists())
        claude=(self.root/'CLAUDE.md').read_text()
        self.assertIn('Never wait for a quota reset',claude)
        self.assertIn('At 80–89%',(self.root/'docs/WORKFLOW.md').read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        self.assertEqual((self.root/'CLAUDE.md').read_text().count('BEGIN AGENT-HANDOFF-WATCHDOG'),1)

    def test_takeover_and_session_start_instructions(self):
        from runner import START_PROMPT
        self.assertIn('DO NOT voluntarily wait', START_PROMPT)
        with patch('hook.current',return_value={'stage':'normal','used_percent':50}):
            result=hook(self.root,'claude','SessionStart','managed',{})
        self.assertIn('Do not voluntarily park',result['hookSpecificOutput']['additionalContext'])


if __name__=='__main__':unittest.main()
