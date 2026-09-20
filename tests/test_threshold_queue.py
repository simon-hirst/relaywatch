"""Regression coverage for the missing 80% proactive Codex instruction."""
import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
TOOL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL))
from engine import atomic_write_json, load_json
from watchdog import queue_codex_if_safe

class ThresholdQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'.agent-watchdog').mkdir()
        atomic_write_json(self.root/'.agent-watchdog'/'active-codex.json',{'thread-123':1000})
        self.cfg={'enable_codex_queue':True,'active_session_seconds':120}
        self.worker={'agent':'codex','type':'tmux','outcome':'launched','session':'managed'}
        self.sent=[]
        self.cli_patch=patch('watchdog.find_cli',return_value='/home/example/.nvm/versions/node/v24.13.0/bin/codex')
        self.cli_patch.start();self.addCleanup(self.cli_patch.stop)
    def fake_run(self,argv,**kwargs):
        self.sent.append(argv)
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    def call(self,stage,now=1050):
        usage={'stage':stage,'agent':'codex','stale':False,'prep_at':1025,'triggered_at':1045}
        with patch('watchdog.latest_worker',return_value=self.worker), patch('watchdog.tmux_exists',return_value=True):
            return queue_codex_if_safe(self.root,self.cfg,usage,now=now,run=self.fake_run)
    def test_prepare_queues_at_80_not_90(self):
        out=self.call('prepare')
        self.assertIn('PREP (80%) MESSAGE QUEUED',out)
        self.assertEqual(self.sent[0][0],'/home/example/.nvm/versions/node/v24.13.0/bin/codex')
        self.assertEqual(self.sent[0][1:3],['queue','--remote'])
        self.assertIn('unix://',self.sent[0])
        self.assertIn('Begin preparing',self.sent[0][-1])
        self.assertEqual(load_json(self.root/'.agent-watchdog'/'codex-queued.json')['status'],'ok')
    def test_does_not_repeat_successful_prepare(self):
        self.call('prepare')
        self.assertIsNone(self.call('prepare',now=1060))
        self.assertEqual(len(self.sent),1)
    def test_escalates_to_90_message(self):
        self.call('prepare')
        out=self.call('handoff',now=1060)
        self.assertIn('FINAL HANDOFF (90%) MESSAGE QUEUED',out)
        self.assertIn('STOP NEW FEATURE WORK',self.sent[-1][-1])
        self.assertEqual(len(self.sent),2)
    def test_never_guesses_when_missing_or_ambiguous_thread(self):
        atomic_write_json(self.root/'.agent-watchdog'/'active-codex.json',{})
        out=self.call('prepare')
        self.assertIn('NOT DELIVERED',out)
        self.assertIn('no unambiguous',out)
        self.assertEqual(self.sent,[])
        self.assertIsNone(self.call('prepare',now=1060))
    def test_never_targets_unmanaged_session(self):
        with patch('watchdog.latest_worker',return_value={}):
            result=queue_codex_if_safe(self.root,self.cfg,{'stage':'prepare','prep_at':1025},now=1050,run=self.fake_run)
        self.assertIn('NOT DELIVERED',result)
        self.assertFalse(self.sent)
    def test_failures_retry_after_backoff(self):
        def fail(argv,**kwargs):
            self.sent.append(argv)
            return SimpleNamespace(returncode=1,stdout='',stderr='not connected')
        with patch('watchdog.latest_worker',return_value=self.worker), patch('watchdog.tmux_exists',return_value=True):
            usage={'stage':'prepare','prep_at':1025}
            first=queue_codex_if_safe(self.root,self.cfg,usage,now=1050,run=fail)
            second=queue_codex_if_safe(self.root,self.cfg,usage,now=1060,run=fail)
            # A real tool lifecycle hook refreshes the active thread after 90 seconds.
            atomic_write_json(self.root/'.agent-watchdog'/'active-codex.json',{'thread-123':1130})
            third=queue_codex_if_safe(self.root,self.cfg,usage,now=1141,run=fail)
        self.assertIn('NOT DELIVERED',first)
        self.assertIsNone(second)
        self.assertIn('NOT DELIVERED',third)
        self.assertEqual(len(self.sent),2)
    def test_normal_has_no_queue(self):
        self.assertIsNone(self.call('normal'))
        self.assertFalse(self.sent)

if __name__=='__main__':unittest.main()

class CompletedSourceHandoffTests(unittest.TestCase):
    """Once Codex hands off to Claude, its old 97%-used quota is historical."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'.agent-watchdog').mkdir()
        self.cfg={'enable_codex_queue':True,'active_session_seconds':120}
        self.usage={'stage':'handoff','agent':'codex','triggered_at':1000,
                    'prep_at':950,'remaining_percent':3,'stale':False}
        from runner import ready_path
        self.ready_file=ready_path(self.root,'codex')
        atomic_write_json(self.ready_file,{
            'event_id':'completed-event','source':'codex','created_at':1030,
        })
        self.invoked=[]
    def pretend_run(self,*args,**kwargs):
        self.invoked.append(args);return SimpleNamespace(returncode=0,stdout='',stderr='')
    def test_stale_exhausted_source_does_not_retry_its_message(self):
        from watchdog import handoff_acknowledged_for_usage
        self.assertTrue(handoff_acknowledged_for_usage(self.root,'codex',self.usage))
        self.assertIsNone(queue_codex_if_safe(self.root,self.cfg,self.usage,
                                                now=1300,run=self.pretend_run))
        self.assertFalse(self.invoked)
        self.assertFalse((self.root/'.agent-watchdog'/'codex-queued.json').exists())
    def test_new_quota_episode_rearms_message(self):
        from watchdog import handoff_acknowledged_for_usage
        later={**self.usage,'stage':'prepare','triggered_at':None,'prep_at':2000}
        self.assertFalse(handoff_acknowledged_for_usage(self.root,'codex',later))
        # It will now attempt delivery (or clearly say why it cannot).
        with patch('watchdog.latest_worker',return_value={}):
            response=queue_codex_if_safe(self.root,self.cfg,later,now=2100,run=self.pretend_run)
        self.assertIn('NOT DELIVERED',response)
    def test_missing_ready_event_does_not_falsely_suppress(self):
        self.ready_file.unlink()
        from watchdog import handoff_acknowledged_for_usage
        self.assertFalse(handoff_acknowledged_for_usage(self.root,'codex',self.usage))
    def test_completed_handoff_suppressed_even_if_old_delivery_failed(self):
        atomic_write_json(self.root/'.agent-watchdog'/'codex-queued.json',{
            'status':'not delivered: no confirmed watchdog-managed Codex session',
            'stage':'handoff','quota_event':1000,'last_attempt':1020})
        self.assertIsNone(queue_codex_if_safe(self.root,self.cfg,self.usage,
                                                now=1500,run=self.pretend_run))
        self.assertFalse(self.invoked)
