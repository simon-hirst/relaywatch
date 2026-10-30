"""Automatic Argon fallback after both managed Claude/Codex quotas run out."""
import time
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from engine import load_json,atomic_write_json
from install import install
from runner import mark_ready,maybe_launch,session_name
from gemini_session import (automatic_eligible,blocks_auto_handoff,
                            status as gemini_status,record_path,show)
from gemini_attention import _managed
from session_recovery import _active_cli_kind


def quota(agent,left):
    return {'agent':agent,'stage':'normal' if left>20 else 'handoff',
            'remaining_percent':left,'stale':False}


def argon(left=.998,*,fresh=True):
    return {'fresh':fresh,'groups':{
      'Gemini Models':{'5h':{'id':'gemini-5h','remaining_fraction':1}},
      'Gemini 4 Argon':{'weekly':{
          'id':'gemini-argon-wtu-weekly','remaining_fraction':left,
          'remaining_percent':round(left*100,3),
          'reset_time':'2026-11-06T00:55:35Z'}}}}


class AutomaticArgonFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        install(self.root)
        self.config=load_json(self.root/'.agent-watchdog/config.json')
        self.config['auto_launch_next_agent']=True
        self.handoff=self.root/'docs/HANDOFF.md'
        self.handoff.write_text('Verified completed Codex quota handoff and safe next steps. '*14)
        self.event=mark_ready(self.root,'codex','session-completed',self.handoff)
        self.old_session=session_name(self.root)
        atomic_write_json(self.root/'.agent-watchdog/worker.json',{
            'type':'tmux','agent':'codex','session':self.old_session,
            'window':'@4','started_at':self.event['created_at']-100,
            'outcome':'launched','startup_status':'verified'})
        self.commands=[]
        def run(argv,**kwargs):
            self.commands.append(argv)
            if argv[1]=='new-window':
                return SimpleNamespace(returncode=0,stdout='@8\n',stderr='')
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        self.run=run

    def launch_with(self, snapshot=None, usages=None):
        snapshot=argon() if snapshot is None else snapshot
        usages=usages or {'codex':quota('codex',8),'claude':quota('claude',4)}
        with patch('gemini_usage.read',return_value=snapshot),\
             patch('gemini_session.cli_path',return_value='/home/simon/.local/bin/agy'),\
             patch('runner.tmux_exists',return_value=True),\
             patch('runner.shutil.which',return_value='/usr/bin/tmux'),\
             patch('runner.subprocess.run',side_effect=self.run),\
             patch('runner.show_agent_window',return_value='existing attached terminal follows'):
            return maybe_launch(self.root,self.config,usages,
                                now=self.event['created_at']+20)

    def test_exact_both_exhausted_scenario_falls_back_to_interactive_argon(self):
        result=self.launch_with()
        self.assertIn('CREATED gemini interactive tmux',result)
        created=[x for x in self.commands if x[1]=='new-window']
        self.assertEqual(len(created),1)
        command=created[0]
        self.assertEqual(command[command.index('-n')+1],'gemini')
        shell=command[-1]
        self.assertIn('agy',shell)
        self.assertIn('gemini-4-argon-high --remote-control ',shell)
        self.assertNotIn(' --remote-control -i ',shell)
        self.assertIn('gemini_startup.py',shell)
        self.assertIn(' gate --project ',shell)
        self.assertIn('docs/HANDOFF.md',shell)
        self.assertNotIn(' --print',shell)
        self.assertIn('exec bash -i',shell)
        self.assertEqual([x[1] for x in self.commands].count('kill-window'),1)
        self.assertEqual([x for x in self.commands if x[1]=='kill-window'][0][-1],'@4')
        worker=load_json(self.root/'.agent-watchdog/worker.json')
        self.assertEqual((worker['agent'],worker['session'],worker['window']),
                         ('gemini',self.old_session,'@8'))
        gemini=load_json(record_path(self.root))
        self.assertEqual(gemini['session'],self.old_session)
        self.assertTrue(gemini['automatic'])
        start=load_json(self.root/'.agent-watchdog/gemini-startup.json')
        self.assertEqual(start['event_id'],self.event['event_id'])
        self.assertEqual(start['window'],'@8')
        self.assertEqual(start['state'],'awaiting_first_invocation')
        self.assertFalse(start['attempted'])
        self.assertEqual(load_json(self.root/'.agent-watchdog/launch-history.json')
                         [self.event['event_id']]['target'],'gemini')
        self.assertEqual(load_json(self.root/'.agent-watchdog/pending-quota.json')
                         ['status'],'resumed')

    def test_never_start_duplicate_when_gemini_is_already_managed(self):
        self.launch_with()
        with patch('gemini_session.tmux_exists',return_value=True):
            self.assertTrue(blocks_auto_handoff(self.root))
            self.assertIsNone(maybe_launch(self.root,self.config,{
                'codex':quota('codex',95),'claude':quota('claude',95)},
                now=self.event['created_at']+40))
            self.assertTrue(_managed(self.root,active=lambda root:True))
            state=gemini_status(self.root,has_session=lambda name:True)
            self.assertTrue(state['automatic'])
            self.assertEqual(state['session'],self.old_session)
        with patch('gemini_session.show_agent_window',return_value='reconnected') as opened:
            self.assertEqual(show(self.root,self.config),'reconnected')
            self.assertNotIn('session_override',opened.call_args.kwargs)

    def test_no_fallback_before_verified_stop_event(self):
        (self.root/'.agent-watchdog/ready-codex.json').unlink()
        result=self.launch_with()
        self.assertIsNone(result)
        self.assertEqual(self.commands,[])
        self.assertEqual(load_json(self.root/'.agent-watchdog/worker.json')['agent'],'codex')

    def test_no_fallback_without_proven_fresh_weekly_argon_quota(self):
        for snapshot in (argon(.05),argon(.10),argon(.99,fresh=False),
                         {'fresh':True,'groups':{'Gemini Models':{
                             'weekly':{'remaining_fraction':1},
                             '5h':{'remaining_fraction':1}}}}):
            with self.subTest(snapshot=snapshot):
                self.assertFalse(automatic_eligible(snapshot))
                # Each subtest represents a new observed waiting episode;
                # RelayWatch intentionally deduplicates repeated wait logs.
                (self.root/'.agent-watchdog/pending-quota.json').unlink(missing_ok=True)
                self.assertIn('WAITING for quota',self.launch_with(snapshot=snapshot))
                self.assertEqual(self.commands,[])

    def test_good_general_gemini_5h_does_not_replace_missing_argon_weekly(self):
        self.assertFalse(automatic_eligible({'fresh':True,'groups':{
           'Gemini Models':{'5h':{'remaining_fraction':1}}}}))
        self.assertFalse(automatic_eligible({'fresh':True,'groups':{
           'Gemini 4 Argon':{'weekly':{'id':'argon','remaining_fraction':.9}}}}))

    def test_healthy_claude_or_codex_takes_priority_over_argon(self):
        for healthy in ('claude','codex'):
            with self.subTest(provider=healthy):
                usages={'claude':quota('claude',5),'codex':quota('codex',4)}
                usages[healthy]=quota(healthy,90)
                with patch('gemini_usage.read',return_value=argon()),\
                     patch('gemini_session.cli_path',return_value='/fake/agy'),\
                     patch('runner.launch',return_value='CREATED '+healthy+' interactive tmux') as launched:
                    result=maybe_launch(self.root,self.config,usages,
                                        now=self.event['created_at']+20)
                    self.assertIn('CREATED '+healthy,result)
                    self.assertEqual(launched.call_args.args[2]['target'],healthy)

    def test_missing_cli_or_opt_out_waits_instead_of_starting(self):
        with patch('gemini_usage.read',return_value=argon()),\
             patch('gemini_session.cli_path',return_value=None):
            self.assertIn('WAITING for quota',maybe_launch(self.root,self.config,
                {'codex':quota('codex',8),'claude':quota('claude',4)},
                now=self.event['created_at']+20))
        self.config['auto_launch_gemini_fallback']=False
        self.assertIsNone(maybe_launch(self.root,self.config,
            {'codex':quota('codex',8),'claude':quota('claude',4)},
            now=self.event['created_at']+40))
        self.assertEqual(self.commands,[])

    def test_pause_and_unknown_sessions_cannot_be_interrupted(self):
        with patch('runner.paused',return_value=True):
            self.assertIsNone(self.launch_with())
        self.assertEqual(self.commands,[])
        atomic_write_json(self.root/'.agent-watchdog/worker.json',{
            'type':'tmux','agent':'claude','session':self.old_session,
            'window':'@4','outcome':'launched',
            'started_at':self.event['created_at']-100})
        res=self.launch_with()
        self.assertIn('not launched',res)
        self.assertFalse(any(cmd[1] in ('new-window','kill-window') for cmd in self.commands))

    def test_real_agy_cli_identity_excludes_quota_helper_processes(self):
        self.assertEqual(_active_cli_kind(['agy','--model','gemini-4-argon-high',
                                          '--remote-control','-i','handoff']),'gemini')
        self.assertIsNone(_active_cli_kind(['agy','-p','/usage','--output-format','json']))
        self.assertIsNone(_active_cli_kind(['agy','--version']))
        self.assertIsNone(_active_cli_kind(['agy','remote-control','start']))
        self.assertIsNone(_active_cli_kind(['agy','models']))
        self.assertEqual(_active_cli_kind(['node','/home/simon/.local/share/antigravity-cli/cli.js',
                                          '--model','gemini-4-argon-high']),'gemini')
        self.assertIsNone(_active_cli_kind(['node','/home/simon/.local/share/antigravity-cli/cli.js',
                                          '-p','/usage']))
        self.assertIsNone(_active_cli_kind(['node','/project/lib/cli.js',
                                          '--model','something']))


if __name__=='__main__':
    unittest.main()
