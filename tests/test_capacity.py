import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOL))
from install import install
from capacity import candidate, check, state_path, record_manual_turn, record_tool_progress
from engine import load_json, atomic_write_json
from runner import session_name
from hook import hook

SCREEN=("Codex is trying to work\n"
        "■ Selected model is at capacity. Please try a different model.\n"
        "  help: use /model to change\n"
        "› Ask Codex to do anything\n")

class CapacityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        install(self.root)
        self.cfg=load_json(self.root/'.agent-watchdog/config.json')
        self.cfg['capacity_recovery']={'enabled':True,'stable_seconds':20,
                                       'max_retries':2,'delays_seconds':[30,90]}
        self.work={'type':'tmux','agent':'codex','outcome':'launched',
                   'window':'@3','session':session_name(self.root),
                   'event_id':'m1','started_at':10}
        atomic_write_json(self.root/'.agent-watchdog/worker.json',self.work)
        self.commands=[]
        self.screen=SCREEN
        self.info={'codex':{'stage':'normal','agent':'codex','remaining_percent':67}}
    def invoke(self,argv,**kwargs):
        self.commands.append(argv)
        if argv[1]=='capture-pane':return SimpleNamespace(returncode=0,stdout=self.screen,stderr='')
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    def poll(self,now):return check(self.root,self.cfg,self.info,now=now,run=self.invoke)

    def test_classifies_only_idle_latest_error(self):
        self.assertTrue(candidate(SCREEN))
        self.assertFalse(candidate('■ Selected model is at capacity. Please try a different model.\n'))
        self.assertFalse(candidate(SCREEN+'› continue\n'))
        self.assertFalse(candidate(SCREEN+'Progress\n'*100))
        self.assertFalse(candidate(SCREEN.replace('Selected model is at capacity','Different error')))

    def test_stable_screen_delayed_bounded_retries_and_alert(self):
        self.assertIn('capacity error detected',self.poll(100))
        self.assertIsNone(self.poll(110))
        self.assertIn('attempt 1/2',self.poll(121))
        self.assertEqual(len([x for x in self.commands if x[1]=='send-keys']),2)
        self.assertIn('capacity error detected',self.poll(122))
        self.assertIsNone(self.poll(150))
        self.assertIsNone(self.poll(170)) # second requires 90s after 121
        self.assertIn('attempt 2/2',self.poll(212))
        self.assertIn('capacity error detected',self.poll(220))
        self.assertIn('retries exhausted',self.poll(240))
        self.assertIsNone(self.poll(261))
        self.assertIsNone(self.poll(290))
        sent=[x for x in self.commands if x[1]=='send-keys']
        self.assertEqual(len(sent),4)
        self.assertEqual(sent[0][4],'-l')
        self.assertIn('Continue the existing task',sent[0][-1])
        self.assertEqual(sent[1][-1],'Enter')
        question=load_json(self.root/'.agent-watchdog/needs-input.json')
        self.assertEqual(question['status'],'open')
        self.assertIn('capacity',question['question'])

    def test_no_injection_on_managed_wrong_window_or_quota_or_pause(self):
        self.info['codex']['stage']='handoff'
        self.assertIsNone(self.poll(100))
        self.info['codex']['stage']='normal'
        self.work['session']='unrelated-session'
        atomic_write_json(self.root/'.agent-watchdog/worker.json',self.work)
        self.assertIsNone(self.poll(130))
        self.work['session']=session_name(self.root)
        atomic_write_json(self.root/'.agent-watchdog/worker.json',self.work)
        (self.root/'.agent-watchdog/PAUSE_AUTO_CONTINUE').touch()
        self.assertIsNone(self.poll(160))
        self.assertFalse(any(x[1]=='send-keys' for x in self.commands))

    def test_activity_or_changed_terminal_blocks_injection(self):
        self.poll(100)
        self.screen=SCREEN+'Task resumed\n' * 40
        self.assertIsNone(self.poll(150))
        self.assertFalse(any(x[1]=='send-keys' for x in self.commands))

    def test_manual_input_suppresses_automatic_retry_then_resets_budget(self):
        self.poll(100);self.poll(130)
        record_manual_turn(self.root,'codex',{'prompt':'continue'})
        state=load_json(state_path(self.root))
        self.assertIn('manual input',state['last_activity'])
        self.assertGreater(state['suppressed_until'],130)
        self.assertEqual(state['attempts'],0)


    def test_automatic_injection_does_not_reset_retry_budget(self):
        self.poll(100);self.poll(130)
        before=load_json(state_path(self.root))
        self.assertEqual(before['attempts'],1)
        record_manual_turn(self.root,'codex',{'prompt':'[WATCHDOG_CAPACITY_RETRY] Continue the existing task'})
        after=load_json(state_path(self.root))
        self.assertEqual(after['attempts'],1)
        self.assertEqual(after.get('suppressed_until'),before.get('suppressed_until'))

    def test_real_tool_progress_rearms_retry_budget(self):
        self.poll(100);self.poll(130)
        record_tool_progress(self.root,'codex')
        state=load_json(state_path(self.root))
        self.assertEqual(state['attempts'],0)
        self.assertEqual(state['last_activity'],'real tool progress')

    def test_stop_hook_does_not_loop_on_capacity(self):
        with patch('hook.current',return_value={'stage':'normal','agent':'codex','remaining_percent':70}):
            response=hook(self.root,'codex','Stop','session1',
                        {'last_assistant_message':'Selected model is at capacity. Please try a different model.'})
        self.assertEqual(response,{})

    def test_install_copies_capacity_and_defaults_preserved(self):
        self.assertTrue((self.root/'.agent-watchdog/capacity.py').is_file())
        original=self.cfg['capacity_recovery'];original['max_retries']=1
        atomic_write_json(self.root/'.agent-watchdog/config.json',self.cfg)
        install(self.root)
        self.assertEqual(load_json(self.root/'.agent-watchdog/config.json')['capacity_recovery']['max_retries'],1)

if __name__=='__main__':unittest.main()
