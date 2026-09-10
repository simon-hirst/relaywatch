"""Regression coverage: missed Stop hook must not strand a completed handoff."""
import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from engine import atomic_write_json, load_json
from runner import mark_ready, maybe_launch, session_name, ready_path
from watchdog import acknowledge_ready, handoff_diagnostics

class ExplicitAckTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'.agent-watchdog').mkdir()
        (self.root/'docs').mkdir()
        (self.root/'.agent-watchdog'/'interactive-agent.sh').write_text('echo safe\n')
        self.file=self.root/'docs'/'HANDOFF.md'
        self.file.write_text('Verified final handoff with tests, changes and next steps. '*12)
        (self.root/'docs'/'session-reports').mkdir()
        (self.root/'docs'/'session-reports'/'2026-10-26T12-00-00Z-codex.md').write_text('Completed: work finished. Verification: test command passed. Next: continue plan. '*5)
        self.config={'cache_seconds':20,'auto_launch_next_agent':True,'auto_launch_grace_seconds':10,
                     'prepare_remaining_pct':20,'agents':{'claude':{'url':'fake'},'codex':{'url':'fake'}}}
        self.session=session_name(self.root)
        self.worker={'agent':'codex','type':'tmux','outcome':'launched','session':self.session,
                     'window':'@1','event_id':'source-worker'}
        atomic_write_json(self.root/'.agent-watchdog'/'worker.json',self.worker)
        self.codex={'agent':'codex','stage':'handoff','stale':False,'remaining_percent':4,
                    'prep_at':1,'triggered_at':1}
    def test_84_percent_not_start_of_claude(self):
        atomic_write_json(self.root/'.agent-watchdog'/'usage-codex.json',{'stage':'prepare','remaining_percent':16})
        self.assertIn('80-89%',handoff_diagnostics(self.root,self.config))
        self.assertFalse(ready_path(self.root,'codex').exists())
    def test_explicit_ready_requires_90_and_fresh_handoff(self):
        with patch('watchdog.tmux_exists',return_value=True),patch('watchdog.current',return_value={**self.codex,'stage':'prepare'}):
            self.assertIn('NOT READY',acknowledge_ready(self.root,self.config,'codex'))
        self.assertFalse(ready_path(self.root,'codex').exists())
        with patch('watchdog.tmux_exists',return_value=True),patch('watchdog.current',return_value={**self.codex,'prep_at':999999999999}):
            self.assertIn('not been freshly updated',acknowledge_ready(self.root,self.config,'codex'))
        self.assertFalse(ready_path(self.root,'codex').exists())
    def test_explicit_ack_fills_missing_stop_and_switches(self):
        with patch('watchdog.tmux_exists',return_value=True),patch('watchdog.current',return_value=self.codex):
            self.assertIn('HANDOFF READY',acknowledge_ready(self.root,self.config,'codex'))
        marker=load_json(ready_path(self.root,'codex'))
        self.assertEqual(marker['source'],'codex')
        self.assertEqual(marker['target'],'claude')
        # A later Stop hook must not replace the marker or lead to a second launch.
        again=mark_ready(self.root,'codex','real-codex-thread',self.file)
        self.assertEqual(marker['event_id'],again['event_id'])
        now=marker['created_at']+15
        def fake_run(argv,**kwargs):
            return SimpleNamespace(returncode=0,stdout='@2' if argv[1]=='new-window' else '',stderr='')
        quota={'claude':{'agent':'claude','stage':'normal','remaining_percent':90,'stale':False}}
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            result=maybe_launch(self.root,self.config,quota,now=now)
        self.assertIn('CREATED claude',result)
        self.assertEqual(load_json(self.root/'.agent-watchdog'/'worker.json')['agent'],'claude')
        with patch('runner.subprocess.run',side_effect=fake_run):
            self.assertIsNone(maybe_launch(self.root,self.config,quota,now=now+1))
    def test_diagnostics_after_exhaustion_without_ack(self):
        atomic_write_json(self.root/'.agent-watchdog'/'usage-codex.json',{'stage':'handoff'})
        msg=handoff_diagnostics(self.root,self.config)
        self.assertIn('has NOT acknowledged',msg)
        self.assertIn('ready --agent codex',msg)

if __name__=='__main__': unittest.main()
