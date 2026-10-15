"""v3.0 quota telemetry, session-summary and model-preset hotkey coverage."""
import os,pty,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch

TOOL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOL))
from engine import atomic_write_json,load_json
from quota_outage import failed,restored,read,qualifies,fresh_handoff,directive
from session_summary import current_summary
from console_ui import ConsoleUI
from hook import hook
from watchdog import acknowledge_ready,poll
from runner import ready_path,session_name

class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup)
        self.root=Path(self.t.name)
        (self.root/'.agent-watchdog').mkdir()
        (self.root/'docs').mkdir()
        (self.root/'docs'/'session-reports').mkdir()
        self.config={'agents':{'claude':{'url':'http://localhost:1/get'},'codex':{'url':'http://localhost:1/get'}},
                     'telemetry_checkpoint_seconds':60,'telemetry_pause_seconds':180,
                     'prepare_remaining_pct':20,'handoff_remaining_pct':10,
                     'auto_launch_next_agent':False}
        atomic_write_json(self.root/'.agent-watchdog'/'config.json',self.config)
        atomic_write_json(self.root/'.agent-watchdog'/'worker.json',{'type':'tmux','agent':'claude','outcome':'launched','session':session_name(self.root),'window':'@1'})
    def test_downtime_has_three_distinct_stages_and_no_invented_percentage(self):
        first,_=failed(self.root,'claude',self.config,now=1000)
        self.assertEqual(first['state'],'grace')
        second,change=failed(self.root,'claude',self.config,now=1061)
        self.assertTrue(change)
        self.assertEqual(second['state'],'checkpoint')
        self.assertIn('do not guess',directive(self.root,'claude'))
        third,change=failed(self.root,'claude',self.config,now=1181)
        self.assertTrue(change)
        self.assertEqual(third['state'],'pause')
        self.assertNotIn('used_percent',third)
        self.assertFalse(fresh_handoff(self.root,'claude'))
        self.assertTrue(qualifies(self.root,'claude'))
        self.assertTrue(restored(self.root,'claude'))
        self.assertFalse(qualifies(self.root,'claude'))
        self.assertEqual(read(self.root,'claude')['state'],'healthy')
    def test_outage_must_have_fresh_handoff_and_summary_before_ready(self):
        failed(self.root,'claude',self.config,now=time.time()-190)
        failed(self.root,'claude',self.config,now=time.time())
        self.assertIn('NOT READY',acknowledge_ready(self.root,self.config,'claude'))
        handoff=self.root/'docs'/'HANDOFF.md'
        handoff.write_text('Verified checkpoint and handoff, detailed tasks and failures. '*15)
        self.assertIn('summary is missing',self._ack())
        summary=self.root/'docs'/'session-reports'/'2026-10-26T12-00-00Z-claude.md'
        summary.write_text('Completed: local tasks. Verification: targeted tests passed. Next: other tasks. '*10)
        self.assertTrue(current_summary(self.root,'claude',read(self.root,'claude')['escalated_at']))
        self.assertIn('HANDOFF READY',self._ack())
        self.assertTrue(ready_path(self.root,'claude').exists())
    def _ack(self):
        with patch('watchdog.tmux_exists',return_value=True):
            return acknowledge_ready(self.root,self.config,'claude')
    def test_restored_source_never_switches_because_of_outage(self):
        failed(self.root,'claude',self.config,now=time.time()-200)
        failed(self.root,'claude',self.config,now=time.time())
        restored(self.root,'claude')
        self.assertFalse(qualifies(self.root,'claude'))
    def test_poll_reports_outage_but_not_fake_usage(self):
        # Healthy Codex while Claude endpoint is unreachable.
        with patch('watchdog.update',side_effect=[OSError('429 temporarily unavailable'),
            {'agent':'codex','stage':'normal','used_percent':10,'remaining_percent':90,
             'limiting_window':'session','windows':{},'timestamp':time.time()}]):
            out=poll(self.root,self.config,enable_launch=False)
        self.assertTrue(any('TELEMETRY UNAVAILABLE' in msg for msg in out))
        self.assertFalse(any('claude limiting ' in msg for msg in out))
    def test_hook_preuse_emits_outage_instruction_without_faked_usage(self):
        failed(self.root,'claude',self.config,now=time.time()-190)
        failed(self.root,'claude',self.config,now=time.time())
        with patch('hook.current',return_value={'agent':'claude','stage':'unknown','stale':True}):
            ctx=hook(self.root,'claude','PreToolUse','abc',{})
        self.assertIn('SUSTAINED USAGE TELEMETRY OUTAGE',ctx['hookSpecificOutput']['additionalContext'])
        self.assertIn('Usage unverified',ctx['hookSpecificOutput']['additionalContext'])
    def test_no_outage_directives_in_unrelated_claude(self):
        failed(self.root,'codex',self.config,now=time.time()-190)
        failed(self.root,'codex',self.config,now=time.time())
        with patch('hook.current',return_value={'agent':'codex','stage':'unknown','stale':True}):
            self.assertEqual(hook(self.root,'codex','PreToolUse','other',{}),{})

class ConsoleTests(unittest.TestCase):
    def test_footer_presents_model_shortcut_and_saved_selection(self):
        with tempfile.TemporaryDirectory() as d:
            ui=ConsoleUI(d)
            ui.available=True
            self.assertIn('Shift+M',ui.footer())
            self.assertIn('Claude',ui.footer())
            ui.available=False
            self.assertIn('-Command models',ui.footer())
    def test_hotkey_accepts_uppercase_only(self):
        with tempfile.TemporaryDirectory() as d:
            master,slave=pty.openpty()
            try:
                ui=ConsoleUI(d)
                ui.fd=slave
                ui.saved_mode=__import__('termios').tcgetattr(slave)
                __import__('tty').setcbreak(slave)
                ui.available=True
                os.write(master,b'm')
                self.assertFalse(ui.read_key(.1))
                os.write(master,b'M')
                self.assertTrue(ui.read_key(.1))
                ui.restore()
            finally:
                os.close(master);os.close(slave)

if __name__=='__main__':unittest.main()
