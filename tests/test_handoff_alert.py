"""Never silently strand an agent at 90% with no verified ready marker."""
import sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from engine import atomic_write_json
from runner import mark_ready
from watchdog import alert_missing_handoff_ack

class MissingAckAlertTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'.agent-watchdog').mkdir();(self.root/'docs').mkdir()
        atomic_write_json(self.root/'.agent-watchdog'/'worker.json',{'agent':'codex','outcome':'launched','type':'tmux'})
        self.cfg={'handoff_alert_after_seconds':300}
        self.info={'codex':{'stage':'handoff','triggered_at':1000,'stale':False}}
        self.sent=[]
    def sender(self,*args):self.sent.append(args);return {'desktop':'sent','phone':'sent'}
    def test_alert_after_five_minutes_once_and_no_switch(self):
        self.assertIsNone(alert_missing_handoff_ack(self.root,self.cfg,self.info,now=1100,sender=self.sender))
        self.assertIn('HANDOFF NOT CONFIRMED',alert_missing_handoff_ack(self.root,self.cfg,self.info,now=1400,sender=self.sender))
        self.assertIsNone(alert_missing_handoff_ack(self.root,self.cfg,self.info,now=1500,sender=self.sender))
        self.assertEqual(len(self.sent),1)
    def test_ready_marker_cancels_alert(self):
        f=self.root/'docs'/'HANDOFF.md';f.write_text('completed and verified '*20)
        marker=mark_ready(self.root,'codex','id',f)
        # Synthetic time before/after real marker for stable test.
        self.info['codex']['triggered_at']=marker['created_at']-200
        self.assertIsNone(alert_missing_handoff_ack(self.root,self.cfg,self.info,
            now=marker['created_at']+401,sender=self.sender))
        self.assertEqual(len(self.sent),0)

if __name__=='__main__':unittest.main()
