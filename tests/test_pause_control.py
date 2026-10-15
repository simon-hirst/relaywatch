"""v3.6 regression tests for explicit user pause and no automatic resume."""
from __future__ import annotations
import contextlib, io, tempfile, unittest, sys, time
from pathlib import Path
from unittest.mock import patch
TOOL=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
from install import install
from hook import hook
from pause_control import paused, activate, deactivate, is_pause_request
from watchdog import poll, queue_codex_if_safe
from runner import maybe_launch
from engine import load_json
from console_ui import DashboardUI


class PauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        self.config=load_json(self.root/'.agent-watchdog'/'config.json')

    def test_pause_command_is_explicit_not_casual_discussion(self):
        for phrase in ('WAIT PAUSE', 'pause', 'HOLD', 'STOP working', 'pause everything'):
            self.assertTrue(is_pause_request(phrase), phrase)
        for phrase in ('what is the pause feature?', 'Continue with next task',
                       'make a pause button', 'when should we pause?',
                       'Explain WAIT PAUSE in the documentation'):
            self.assertFalse(is_pause_request(phrase), phrase)

    def test_pause_prompt_blocks_and_requires_manual_resume(self):
        with patch('hook.current') as current:
            r=hook(self.root,'claude','UserPromptSubmit','thread1',{'prompt':'WAIT PAUSE'})
            self.assertFalse(r.get('continue',True))
            self.assertTrue(paused(self.root))
            # Model-generated continuation must not clear the pause.
            auto=hook(self.root,'claude','UserPromptSubmit','thread1',{'prompt':'Continue with next task'})
            self.assertFalse(auto.get('continue',True))
            self.assertTrue(paused(self.root))
            # No quota/network reads should happen during a paused event.
            current.assert_not_called()
        self.assertTrue(deactivate(self.root))
        self.assertFalse(paused(self.root))
        with patch('hook.current',return_value={'stage':'normal','agent':'claude'}):
            self.assertEqual(hook(self.root,'claude','UserPromptSubmit','thread1',{'prompt':'Continue with next task'}),{})

    def test_pause_blocks_tools_and_stop_continuations(self):
        activate(self.root)
        for agent in ('claude','codex'):
            with self.subTest(agent=agent):
                result=hook(self.root,agent,'PreToolUse','t',{'tool_name':'Bash'})
                self.assertFalse(result['continue'])
                self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
                self.assertFalse(hook(self.root,agent,'Stop','t',{})['continue'])
        self.assertIn('PAUSED BY USER',DashboardUI(self.root)._pending()[0])

    def test_poll_does_not_queue_or_launch_or_recover(self):
        activate(self.root)
        healthy={a:{'agent':a,'stage':'normal','used_percent':5.,'remaining_percent':95.,
                    'limiting_window':'session','windows':{}, 'timestamp':time.time()} for a in ('claude','codex')}
        with patch('watchdog.update',side_effect=[healthy['claude'],healthy['codex']]), \
             patch('watchdog.maybe_launch') as maybe, \
             patch('watchdog.capacity_check') as capacity, \
             patch('watchdog.recover_worker') as recover, \
             patch('watchdog.queue_codex_if_safe') as queue:
            messages=poll(self.root,self.config,enable_launch=True)
        self.assertTrue(any('USER PAUSED' in msg for msg in messages))
        maybe.assert_not_called()
        capacity.assert_not_called()
        recover.assert_not_called()
        # No codex queue even with missing worker.
        self.assertEqual(queue_codex_if_safe(self.root,self.config,{'stage':'handoff','stale':False}),None)
        self.assertIsNone(maybe_launch(self.root,self.config,healthy))

    def test_repeated_install_preserves_pause(self):
        activate(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            install(self.root)
        self.assertTrue(paused(self.root))
        self.assertTrue((self.root/'.agent-watchdog'/'pause_control.py').is_file())

if __name__=='__main__':unittest.main()
