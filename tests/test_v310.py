"""Regression: an old automatic no-progress alert must not trap a resumed worker."""
import io
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from install import install
from engine import atomic_write_json,load_json
from alerts import (register_question,request_path,pending_question,is_auto_stall,
                    dismiss_auto_stall, AUTO_STALL_PREFIX)
from runner import session_name
from resume_agent import resume_existing,MESSAGE,mark_received

IDLE='''Claude Code v2.1.310
❯ Try "find a bug"
⏵⏵ bypass permissions on (shift+tab to cycle)
'''

class ResumeInputBlockerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        with contextlib.redirect_stdout(io.StringIO()):install(self.root)
        self.config=load_json(self.root/'.agent-watchdog/config.json')
        self.worker={'agent':'claude','type':'tmux','outcome':'launched','window':'@2',
                     'session':session_name(self.root)}

    def simulation(self,check_only=False):
        commands=[]
        display={'text':IDLE}
        def run(args,**kw):
            commands.append(args)
            if args[1]=='list-panes': return SimpleNamespace(returncode=0,stdout='%2\n')
            if args[1]=='capture-pane':return SimpleNamespace(returncode=0,stdout=display['text'])
            if args[1]=='send-keys':
                if '-l' in args:display['text']='Claude Code\n❯ '+MESSAGE+'\n'
                elif args[-1]=='Enter':
                    mark_received(self.root,'claude',MESSAGE)
                    display['text']='Claude Code\nThinking...\n'
            return SimpleNamespace(returncode=0,stdout='')
        with patch('resume_agent.latest_worker',return_value=self.worker),\
             patch('resume_agent.current',return_value={'stage':'normal','remaining_percent':50}),\
             patch('resume_agent.tmux_exists',return_value=True):
            response=resume_existing(self.root,self.config,run=run,sleep=lambda _:None,check_only=check_only)
        return response,commands

    def test_old_no_progress_blocker_automatically_acknowledged_only_by_resume(self):
        atomic_write_json(request_path(self.root),{'agent':'claude','status':'open',
             'question':AUTO_STALL_PREFIX+' Please review the session and give it a next step.'})
        pre,_=self.simulation(check_only=True)
        self.assertIn('RESUMABLE STALE BLOCKER',pre)
        self.assertEqual(pending_question(self.root)['status'],'open')
        response,cmds=self.simulation()
        self.assertIn('RESUMED claude',response)
        self.assertTrue(any(c[1]=='send-keys' for c in cmds))
        question=load_json(request_path(self.root))
        self.assertEqual(question['status'],'acknowledged')
        self.assertEqual(question['acknowledged_by'],'explicit_cli_resume')

    def test_new_tagged_auto_stall(self):
        register_question(self.root,'claude',AUTO_STALL_PREFIX+' Please review the session.',source='watchdog_auto_continue_stall')
        self.assertTrue(is_auto_stall(pending_question(self.root)))
        self.assertTrue(dismiss_auto_stall(self.root,'claude'))

    def test_genuine_question_remains_a_blocker(self):
        register_question(self.root,'claude','May I deploy to production?',source='agent')
        result,cmds=self.simulation()
        self.assertIn('May I deploy to production?',result)
        self.assertNotIn('send-keys', [c[1] for c in cmds])
        self.assertEqual(pending_question(self.root)['status'],'open')

    def test_auto_stall_text_with_explicit_agent_source_not_acknowledged(self):
        register_question(self.root,'claude',AUTO_STALL_PREFIX+' Is this a real decision?',source='agent')
        self.assertFalse(is_auto_stall(pending_question(self.root)))
        self.assertFalse(dismiss_auto_stall(self.root,'claude'))

    def test_previous_provider_question_does_not_block_current_claude(self):
        register_question(self.root,'codex','Which build target should I deploy?',source='agent')
        result,cmds=self.simulation()
        self.assertIn('RESUMED claude',result)
        self.assertTrue(any(c[1]=='send-keys' for c in cmds))
        self.assertEqual(pending_question(self.root)['agent'],'codex')
        self.assertEqual(pending_question(self.root)['status'],'open')

    def test_pause_has_priority_even_with_stale_question(self):
        register_question(self.root,'claude',AUTO_STALL_PREFIX,source='watchdog_auto_continue_stall')
        (self.root/'.agent-watchdog/PAUSE_AUTO_CONTINUE').touch()
        result,cmds=self.simulation()
        self.assertIn('pause is still active',result)
        self.assertEqual(pending_question(self.root)['status'],'open')
        self.assertFalse(cmds)

    def test_legacy_question_not_attributable_to_auto_stall_kept(self):
        atomic_write_json(request_path(self.root),{'agent':'claude','question':'Another real input please', 'status':'open'})
        result,cmds=self.simulation()
        self.assertIn('Another real input please',result)
        self.assertEqual(pending_question(self.root)['status'],'open')
        self.assertFalse(cmds)

if __name__=='__main__':unittest.main()

class StatusVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        with contextlib.redirect_stdout(io.StringIO()):install(self.root)

    def output(self):
        import watchdog
        with contextlib.redirect_stdout(io.StringIO()) as stream, \
             patch('watchdog.ROOT',self.root), \
             patch('watchdog.sys.argv',['watchdog.py','status']), \
             patch('watchdog.handoff_diagnostics',return_value='No handoff required'):
            watchdog.main()
        return stream.getvalue()

    def test_shows_local_question_and_owner(self):
        register_question(self.root,'claude','Which source branch should I use?')
        status=self.output()
        self.assertIn('PENDING USER INPUT:',status)
        self.assertIn('Which source branch should I use?',status)
        self.assertIn('"agent": "claude"',status)

    def test_shows_empty_when_no_question(self):
        self.assertIn('PENDING USER INPUT: none',self.output())
