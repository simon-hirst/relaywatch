"""An exited interactive Codex must not strand a valid handoff forever."""
import sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from engine import atomic_write_json, load_json
from runner import maybe_launch, ready_path, session_name
from watchdog import acknowledge_exited_handoff

class OrphanTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        (self.root/'docs').mkdir()
        (self.root/'.agent-watchdog').mkdir()
        (self.root/'.agent-watchdog'/'interactive-agent.sh').write_text('exit 0\n')
        self.handoff=self.root/'docs'/'HANDOFF.md'
        self.handoff.write_text('Checked, completed handoff describing changed files, tests and next tasks. '*10)
        self.cfg={'agents':{'codex':{'url':'unused'},'claude':{'url':'unused'}},'auto_launch_next_agent':True,'auto_launch_grace_seconds':10,'prepare_remaining_pct':20}
        self.info={'agent':'codex','stage':'handoff','remaining_percent':4,'prep_at':time.time()-60,'triggered_at':time.time()-30}
    def recover(self, **kwargs):
        with patch('watchdog.tmux_exists',return_value=False),patch('watchdog.active_repository_agents',return_value=[]),patch('watchdog.current',return_value=self.info):
            return acknowledge_exited_handoff(self.root,self.cfg,'codex',**kwargs)
    def test_explicit_human_confirmation_required(self):
        self.assertIn('NOT READY', self.recover())
        self.assertFalse(ready_path(self.root,'codex').exists())
    def test_refuses_running_tmux(self):
        with patch('watchdog.tmux_exists',return_value=True):
            self.assertIn('NOT READY',acknowledge_exited_handoff(self.root,self.cfg,'codex',confirm_complete=True))
    def test_refuses_other_live_agent_process(self):
        with patch('watchdog.tmux_exists',return_value=False),patch('watchdog.active_repository_agents',return_value=['codex PID 123']):
            self.assertIn('NOT READY',acknowledge_exited_handoff(self.root,self.cfg,'codex',confirm_complete=True))
    def test_refuses_stale_or_insufficient_quota(self):
        with patch('watchdog.tmux_exists',return_value=False),patch('watchdog.active_repository_agents',return_value=[]),patch('watchdog.current',return_value={**self.info,'stage':'normal'}):
            self.assertIn('NOT READY', acknowledge_exited_handoff(self.root,self.cfg,'codex',confirm_complete=True))
        self.info['prep_at']=time.time()+100
        self.assertIn('not updated',self.recover(confirm_complete=True))
        self.assertIn('HANDOFF READY',self.recover(confirm_complete=True,allow_stale=True))
    def test_confirmed_orphan_launches_other_agent_once(self):
        self.assertIn('HANDOFF READY',self.recover(confirm_complete=True))
        event=load_json(ready_path(self.root,'codex'))
        self.assertEqual('codex',event['source'])
        self.assertEqual('claude',event['target'])
        def fake_run(args,**kwargs):
            if args[:2]==['tmux','has-session']:return SimpleNamespace(returncode=1,stdout='',stderr='')
            if args[:2]==['tmux','new-session']:return SimpleNamespace(returncode=0,stdout='@2\n',stderr='')
            raise AssertionError(args)
        q={'claude':{'agent':'claude','stage':'normal','remaining_percent':70}}
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            answer=maybe_launch(self.root,self.cfg,q,now=event['created_at']+11)
            self.assertIn('CREATED claude',answer)
            self.assertEqual(load_json(self.root/'.agent-watchdog'/'worker.json')['agent'],'claude')
            self.assertIsNone(maybe_launch(self.root,self.cfg,q,now=event['created_at']+15))
    def test_after_quota_reset_only_manual_stale_override_can_recover(self):
        self.info={**self.info,'stage':'normal','remaining_percent':95,'prep_at':None,'triggered_at':None}
        self.assertIn('NOT READY',self.recover(confirm_complete=True))
        self.assertIn('HANDOFF READY',self.recover(confirm_complete=True,allow_stale=True))
    def test_missing_handoff_refused(self):
        self.handoff.unlink()
        self.assertIn('NOT READY',self.recover(confirm_complete=True))

if __name__=='__main__':unittest.main()
