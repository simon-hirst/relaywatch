import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
TOOL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOL))
from install import install
from hook import hook
from engine import atomic_write_json,load_json
from runner import START_PROMPT

class AutoContinueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        install(self.root)
        self.handoff=self.root/'docs'/'HANDOFF.md'
        self.handoff.write_text('Verified handoff with prioritized local task T06 and acceptance tests. '*15)
        atomic_write_json(self.root/'.agent-watchdog'/'worker.json',{'type':'tmux','agent':'codex','outcome':'launched','window':'@1'})
    def call(self,agent='codex',info=None,session='managed'): 
        info=info or {'stage':'normal','agent':agent}
        with patch('hook.current',return_value=info):
            return hook(self.root,agent,'Stop',session,{'stop_hook_active':False})
    def test_prompt_is_not_single_task(self):
        self.assertIn('CONTINUE AUTOMATICALLY',START_PROMPT)
    def test_codex_auto_continue_on_healthy_quota(self):
        result=self.call()
        self.assertEqual(result['decision'],'block')
        self.assertIn('next highest-priority',result['reason'])
    def test_80pct_small_tasks_continue(self):
        self.assertEqual(self.call(info={'stage':'prepare','agent':'codex'})['decision'],'block')
    def test_90pct_not_continue_new_tasks(self):
        self.assertIn('URGENT',self.call(info={'stage':'handoff','agent':'codex','prep_at':10**20})['reason'])
    def test_no_active_managed_agent(self):
        self.assertEqual(self.call('claude'),{})
    def test_stale_quota(self):
        self.assertEqual(self.call(info={'stage':'normal','stale':True}),{})
    def test_user_pause_file(self):
        (self.root/'.agent-watchdog'/'PAUSE_AUTO_CONTINUE').touch()
        self.assertFalse(self.call().get('continue', True))
    def test_quota_unknown(self):
        self.assertEqual(self.call(info={'stage':'unknown'}),{})
    def test_bounded_no_progress(self):
        self.assertEqual(self.call()['decision'],'block')
        self.assertEqual(self.call()['decision'],'block')
        self.assertEqual(self.call(),{})
    def test_handoff_update_resets_no_progress_counter(self):
        self.call();self.call()
        self.handoff.write_text('Next task started and tests passing. '*30)
        self.assertEqual(self.call()['decision'],'block')
    def test_manual_config_disable(self):
        p=self.root/'.agent-watchdog'/'config.json'
        cfg=load_json(p);cfg['auto_continue_turns']=False
        atomic_write_json(p,cfg)
        self.assertEqual(self.call(),{})

if __name__=='__main__':unittest.main()
