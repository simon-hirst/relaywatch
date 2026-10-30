"""Opt-in Antigravity Gemini 4 Argon CLI integration safety tests."""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from engine import load_json, atomic_write_json
from gemini_session import (launch, status, show, MODEL_IDS, record_path,
                            gemini_session_name, blocks_auto_handoff)
from install import install
from runner import session_name, maybe_launch


class GeminiArgonTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        install(self.root)
        file=self.root/'docs'/'HANDOFF.md'
        file.write_text('Verified local tasks, tests and current handoff; ' * 15)
        self.config=load_json(self.root/'.agent-watchdog'/'config.json')
        self.calls=[]
        self.which=lambda name,**kwargs: '/fake/'+name
        self.run=lambda args,**kwargs: (self.calls.append(args) or
                       SimpleNamespace(returncode=0,stdout='@8\n',stderr=''))

    def test_model_slugs_are_official_antigravity_cli_models(self):
        self.assertEqual(MODEL_IDS['high'],'gemini-4-argon-high')
        self.assertEqual(MODEL_IDS['medium'],'gemini-4-argon-medium')

    def test_explicit_argon_high_starts_interactive_remote_session(self):
        with patch('gemini_session.tmux_exists',return_value=False), \
             patch('gemini_session.active_repository_agents',return_value=[]):
            result=launch(self.root,self.config,invoke=self.run,which=self.which,
                          other_sessions=lambda root:[])
        self.assertIn('CREATED Gemini 4 Argon (high)',result)
        cmd=self.calls[0]
        self.assertEqual(cmd[:5],['tmux','new-session','-d','-P','-F'])
        shell=cmd[-1]
        self.assertIn('agy --model gemini-4-argon-high --remote-control -i ',shell)
        self.assertNotIn('--print',shell)
        self.assertNotIn(' --dangerously-skip-permissions',shell)
        self.assertIn('exec bash -i',shell)
        self.assertEqual(load_json(record_path(self.root))['agent'],'gemini')
        self.assertEqual(status(self.root,has_session=lambda n:True)['quota'],'unknown')

    def test_medium_model_and_invalid_name(self):
        with patch('gemini_session.tmux_exists',return_value=False):
            result=launch(self.root,self.config,model='medium',invoke=self.run,
                          which=self.which,other_sessions=lambda root:[])
        self.assertIn('CREATED',result)
        self.assertIn('gemini-4-argon-medium',self.calls[-1][-1])
        self.assertIn('choose Gemini',launch(self.root,self.config,model='ultra',
                                            invoke=self.run,which=self.which))

    def test_existing_claude_codex_tmux_is_never_closed_or_replaced(self):
        with patch('gemini_session.tmux_exists',side_effect=lambda name,**kwargs:name==session_name(self.root)):
            res=launch(self.root,self.config,invoke=self.run,which=self.which)
        self.assertIn('Claude/Codex managed tmux',res)
        self.assertEqual(self.calls,[])

    def test_an_existing_gemini_session_is_not_restarted(self):
        with patch('gemini_session.tmux_exists',
                   side_effect=lambda name,**kwargs:name==gemini_session_name(self.root)):
            res=launch(self.root,self.config,invoke=self.run,which=self.which)
        self.assertIn('already exists',res)
        self.assertEqual(self.calls,[])

    def test_other_coding_process_blocks_launch(self):
        with patch('gemini_session.tmux_exists',return_value=False):
            res=launch(self.root,self.config,invoke=self.run,which=self.which,
                       other_sessions=lambda root:['claude PID 300'])
        self.assertIn('existing coding process',res)
        self.assertEqual(self.calls,[])

    def test_user_pause_and_no_handoff_block_launch(self):
        (self.root/'docs'/'HANDOFF.md').unlink()
        self.assertIn('handoff',launch(self.root,self.config,which=self.which))
        (self.root/'docs'/'HANDOFF.md').write_text('Verified tasks ' * 35)
        atomic_write_json(self.root/'.agent-watchdog/paused.json',
                          {'active':True})
        # paused() has its own exact schema: test via patched decision instead.
        with patch('gemini_session.paused',return_value=True):
            self.assertIn('paused',launch(self.root,self.config,which=self.which))

    def test_missing_agy_never_starts_tmux(self):
        with patch('gemini_session.tmux_exists',return_value=False):
            res=launch(self.root,self.config,invoke=self.run,
                       which=lambda name,**kwargs:None if name=='agy' else '/usr/bin/tmux')
        self.assertIn('agy) not found',res)
        self.assertEqual(self.calls,[])

    def test_default_two_agent_handoff_unaffected_without_gemini_record(self):
        with patch('gemini_session.tmux_exists',side_effect=AssertionError('must not inspect tmux')):
            self.assertFalse(blocks_auto_handoff(self.root))

    def test_present_gemini_session_suspends_automatic_handoff(self):
        atomic_write_json(record_path(self.root),{'agent':'gemini','session':gemini_session_name(self.root)})
        self.config['auto_launch_next_agent']=True
        with patch('gemini_session.tmux_exists',return_value=True):
            self.assertTrue(blocks_auto_handoff(self.root))
            self.assertIsNone(maybe_launch(self.root,self.config,{}))
        self.assertEqual(self.calls,[])

    def test_reconnect_never_creates_second_agent(self):
        with patch('gemini_session.tmux_exists',return_value=True), \
             patch('gemini_session.show_agent_window',return_value='Windows Terminal opened for gemini') as view:
            result=show(self.root,self.config)
        self.assertIn('opened',result)
        view.assert_called_once_with(self.root,self.config,'gemini',force=True,
                                     invoke=None,session_override=gemini_session_name(self.root))

    def test_installer_installs_and_hashes_module(self):
        import hashlib
        script=self.root/'.agent-watchdog/gemini_session.py'
        self.assertTrue(script.exists())
        manifest=load_json(self.root/'.agent-watchdog/managed-files.json')
        self.assertEqual(manifest['gemini_session.py'],
                         hashlib.sha256(script.read_bytes()).hexdigest())


if __name__=='__main__':
    unittest.main()
