"""Regression tests for Windows shell checkout, stale handoffs and true CLI startup."""
import hashlib
import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from alerts import register_question, supersede_handed_off_question, request_path
from engine import load_json, atomic_write_json
from install import install
from runner import launch, mark_ready, maybe_launch, check_worker_startup, session_name


def quota(agent):
    return {'agent':agent,'stage':'normal','stale':False,'remaining_percent':90}


class RecoveryHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        install(self.root)
        self.config=load_json(self.root/'.agent-watchdog'/'config.json')
        self.config['auto_launch_next_agent']=True
        self.config['auto_open_terminal_window']=False
        self.file=self.root/'docs'/'HANDOFF.md'
        self.file.write_text('Verified changes and tests. Next: inspect local work. '*15)

    def test_installer_converts_windows_crlf_and_updates_manifest(self):
        from install import SOURCE
        original=shutil.copy2
        def injected_copy(src,dst,*args,**kwargs):
            result=original(src,dst,*args,**kwargs)
            if Path(src).name=='interactive-agent.sh':
                path=Path(dst)
                path.write_bytes(path.read_bytes().replace(b'\n',b'\r\n'))
            return result
        with patch('install.shutil.copy2',side_effect=injected_copy):
            install(self.root)
        script=self.root/'.agent-watchdog/interactive-agent.sh'
        self.assertNotIn(b'\r\n',script.read_bytes())
        manifest=load_json(self.root/'.agent-watchdog/managed-files.json')
        self.assertEqual(hashlib.sha256(script.read_bytes()).hexdigest(),manifest['interactive-agent.sh'])

    def test_preflight_refuses_crlf_before_launching_tmux(self):
        script=self.root/'.agent-watchdog/interactive-agent.sh'
        script.write_bytes(b'#!/bin/bash\r\nset -euo pipefail\r\n')
        calls=[]
        def fake(argv,**kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=1 if argv[1]=='has-session' else 0,stderr='',stdout='')
        with patch('runner.subprocess.run',side_effect=fake),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            result=launch(self.root,self.config,{'source':'claude','target':'codex','event_id':'x'},quota('codex'))
        self.assertIn('CRLF detected',result)
        self.assertNotIn('new-session',[args[1] for args in calls])

    def test_preflight_refuses_bash_syntax_errors_before_launch(self):
        calls=[]
        def fake(argv,**kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=1 if argv[0]=='bash' or argv[1]=='has-session' else 0,
                                   stderr='syntax invalid' if argv[0]=='bash' else '', stdout='')
        with patch('runner.subprocess.run',side_effect=fake),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            result=launch(self.root,self.config,{'source':'claude','target':'codex','event_id':'x'},quota('codex'))
        self.assertIn('FAILED launcher preflight',result)
        self.assertNotIn('new-session',[args[1] for args in calls])

    def test_older_unhandled_source_event_cannot_duplicate_new_worker(self):
        old=mark_ready(self.root,'codex','old',self.file)
        self.file.write_text(self.file.read_text()+' newer verified audit')
        current=mark_ready(self.root,'claude','new',self.file)
        self.assertGreater(current['created_at'],old['created_at']-0.01)
        commands=[]
        def fake(argv,**kwargs):
            commands.append(argv)
            if argv[1]=='has-session':
                return SimpleNamespace(returncode=1,stdout='',stderr='')
            return SimpleNamespace(returncode=0,
                                   stdout='@1' if argv[1]=='new-session' else '',stderr='')
        with patch('runner.subprocess.run',side_effect=fake),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            response=maybe_launch(self.root,self.config,{'codex':quota('codex')},now=current['created_at']+20)
            self.assertIn('CREATED codex',response)
            self.assertIsNone(maybe_launch(self.root,self.config,{'codex':quota('codex'),'claude':quota('claude')},
                                            now=current['created_at']+21))
        self.assertEqual(sum(argv[1]=='new-session' for argv in commands),1)

    def test_handoff_supersedes_only_older_source_questions(self):
        register_question(self.root,'claude','Should I start v7?',now=100)
        self.assertTrue(supersede_handed_off_question(self.root,'claude',200))
        item=load_json(request_path(self.root))
        self.assertEqual(item['status'],'superseded')
        self.assertIn('no permission',item['superseded_reason'])
        register_question(self.root,'codex','May I deploy?',now=210)
        self.assertFalse(supersede_handed_off_question(self.root,'claude',220))
        self.assertEqual(load_json(request_path(self.root))['status'],'open')
        self.assertFalse(supersede_handed_off_question(self.root,'codex',205))
        self.assertEqual(load_json(request_path(self.root))['status'],'open')

    @patch('runner.os.path.isdir', new=lambda path: True)
    def test_old_question_not_superseded_until_receiving_cli_is_verified(self):
        register_question(self.root,'claude','Should I start v7?',now=100)
        worker={'agent':'codex','type':'tmux','outcome':'launched',
                'session':session_name(self.root),'window':'@2',
                'source_agent':'claude','handoff_created_at':200,'started_at':205,
                'startup_probe_started_at':205}
        atomic_write_json(self.root/'.agent-watchdog/worker.json',worker)
        missing=lambda root:(None,'only an idle Bash shell')
        self.assertIn('NOT VERIFIED',check_worker_startup(self.root,now=266,detector=missing))
        self.assertEqual(load_json(request_path(self.root))['status'],'open')
        matched=lambda root:({'agent':'codex','session':session_name(self.root),'window':'@2'},None)
        self.assertIn('STARTUP VERIFIED',check_worker_startup(self.root,now=267,detector=matched))
        self.assertEqual(load_json(request_path(self.root))['status'],'superseded')

    def test_host_without_proc_does_not_claim_agent_crashed(self):
        worker={'agent':'codex','type':'tmux','outcome':'launched',
                'session':session_name(self.root),'window':'@2','started_at':100,'startup_probe_started_at':100}
        atomic_write_json(self.root/'.agent-watchdog/worker.json',worker)
        with patch('runner.os.path.isdir',return_value=False):
            result=check_worker_startup(self.root,now=200,
                                         detector=lambda root:(None,'cannot inspect'))
        self.assertIn('UNVERIFIED',result)
        self.assertEqual(load_json(self.root/'.agent-watchdog/worker.json')['startup_status'],'unavailable')
        self.assertIsNone(check_worker_startup(self.root,now=300,
                                               detector=lambda root:(None,'cannot inspect')))

    @patch('runner.os.path.isdir', new=lambda path: True)
    def test_auto_updater_does_not_fail_legacy_running_claude_worker(self):
        worker={'agent':'claude','type':'tmux','outcome':'launched',
                'session':session_name(self.root),'window':'@2',
                'started_at':100,'startup_status':'failed'}
        atomic_write_json(self.root/'.agent-watchdog/worker.json',worker)
        absent=lambda root:(None,'CLI process args changed after reexec')
        text=check_worker_startup(self.root,now=4000,detector=absent)
        self.assertIn('SESSION UNVERIFIED',text)
        saved=load_json(self.root/'.agent-watchdog/worker.json')
        self.assertEqual(saved['startup_status'],'unverified')
        self.assertIsNone(check_worker_startup(self.root,now=4100,detector=absent))
        recovered=lambda root:({'agent':'claude','session':session_name(self.root),
                                'window':'@2'},None)
        text=check_worker_startup(self.root,now=4120,detector=recovered)
        self.assertIn('STARTUP VERIFIED',text)
        self.assertEqual(load_json(self.root/'.agent-watchdog/worker.json')['startup_status'],'verified')

    @patch('runner.os.path.isdir', new=lambda path: True)
    def test_first_poll_of_old_uninspected_claude_worker_not_a_new_failed_launch(self):
        worker={'agent':'claude','type':'tmux','outcome':'launched',
                'session':session_name(self.root),'window':'@2','started_at':100}
        atomic_write_json(self.root/'.agent-watchdog/worker.json',worker)
        text=check_worker_startup(self.root,now=5000,detector=lambda root:(None,'old CLI not recognized'))
        self.assertIn('SESSION UNVERIFIED',text)
        self.assertNotIn('STARTUP NOT VERIFIED',text)
        self.assertEqual(load_json(self.root/'.agent-watchdog/worker.json')['startup_status'],'unverified')

    def test_npm_claude_wrapper_counts_as_real_interactive_cli(self):
        from session_recovery import _interactive_identity
        argv=(b'node\x00/home/simon/.npm-global/lib/node_modules/'
              b'@anthropic-ai/claude-code/cli.js\x00--rc\x00')
        # macOS temp paths often resolve /var -> /private/var; the production
        # caller passes a resolved root. Avoid mocking os.readlink globally,
        # since Path.resolve() itself may need to follow symlinks on macOS.
        import os
        root=self.root.resolve()
        real_readlink=os.readlink
        def readlink(path):
            if str(path)=='/proc/123/cwd':
                return str(root)
            return real_readlink(path)
        with (patch('session_recovery.os.readlink',side_effect=readlink),
              patch('session_recovery.Path.read_bytes',return_value=argv)):
            self.assertEqual(_interactive_identity(123,root),'claude')

    @patch('runner.os.path.isdir', new=lambda path: True)
    def test_startup_verification_requires_correct_cli_window(self):
        worker={'agent':'codex','type':'tmux','outcome':'launched','session':session_name(self.root),
                'window':'@2','started_at':100}
        atomic_write_json(self.root/'.agent-watchdog/worker.json',worker)
        no_match=lambda root:(None,'only an idle Bash shell')
        self.assertIsNone(check_worker_startup(self.root,now=130,detector=no_match))
        failure=check_worker_startup(self.root,now=170,detector=no_match)
        self.assertIn('NOT VERIFIED',failure)
        self.assertEqual(load_json(self.root/'.agent-watchdog/worker.json')['startup_status'],'failed')
        wrong=lambda root:({'agent':'claude','session':session_name(self.root),'window':'@2'},None)
        self.assertIsNone(check_worker_startup(self.root,now=171,detector=wrong))
        matched=lambda root:({'agent':'codex','session':session_name(self.root),'window':'@2'},None)
        self.assertIn('STARTUP VERIFIED',check_worker_startup(self.root,now=172,detector=matched))
        self.assertIsNone(check_worker_startup(self.root,now=173,detector=matched))
        self.assertEqual(load_json(self.root/'.agent-watchdog/worker.json')['startup_status'],'verified')


if __name__=='__main__':
    unittest.main()
