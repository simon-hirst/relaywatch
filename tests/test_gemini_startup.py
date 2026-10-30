"""Argon first prompt must be delivered, not merely opened in tmux."""
import tempfile
import time
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from engine import load_json,atomic_write_json
from install import install
from runner import session_name
from gemini_session import record_path
import gemini_startup as gs
import gemini_attention as ga


class GeminiStartupDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        install(self.root)
        self.session=session_name(self.root)
        self.event='verified-codex-handoff'
        self.window='@8'
        atomic_write_json(self.root/'.agent-watchdog/worker.json',{
            'type':'tmux','agent':'gemini','session':self.session,
            'event_id':self.event,'window':self.window,
            'outcome':'launched','started_at':1000})
        atomic_write_json(record_path(self.root),{
            'type':'tmux','agent':'gemini','session':self.session,
            'window':self.window,'outcome':'launched'})
        gs.start(self.root,event_id=self.event,session=self.session,
                 window=self.window,now=1000)
        self.payload={'workspacePaths':[str(self.root)],
                      'modelName':'gemini-4-argon-high',
                      'conversationId':'abc','invocationNum':0}
        # Native SessionStart proves this *new* CLI loaded workspace hooks.
        gs.hook(self.root,'SessionStart',self.payload,now=1001)
        self.commands=[]
        self.notifications=[]
        self.sender=lambda _root,_cfg,title,desktop,phone: (
            self.notifications.append((title,desktop,phone)) or
            {'desktop':'sent','phone':'sent'})
        self.detect=lambda root,expected_window=None: (
            {'agent':'gemini','window':'@8','session':self.session},None)

    def run_screen(self,screen):
        def run(argv,**kwargs):
            self.commands.append(argv)
            if argv[1]=='list-panes':
                return SimpleNamespace(returncode=0,stdout='%2\n',stderr='')
            if argv[1]=='capture-pane':
                return SimpleNamespace(returncode=0,stdout=screen,stderr='')
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        return run

    def monitor(self,screen,now=1040):
        with patch('gemini_startup.paused',return_value=False):
            return gs.monitor(self.root,{},now=now,
                run=self.run_screen(screen), detect=self.detect,
                sender=self.sender,active=lambda root:True)

    def test_receipt_only_confirms_first_model_invocation_not_tmux_or_session_start(self):
        self.assertTrue(gs.gate(self.root,self.event,timeout=0.1))
        self.assertTrue(ga.handle(self.root,{},'SessionStart',self.payload)=={})
        receipt=load_json(gs.receipt_path(self.root))
        self.assertEqual(receipt['state'],'awaiting_first_invocation')
        self.assertEqual(receipt['session_seen_at']>0,True)
        self.assertEqual(self.monitor('Antigravity\nType a message'), 'GEMINI INITIAL HANDOFF SUBMITTED: one-shot verified idle rescue; awaiting model invocation')
        self.assertEqual(load_json(gs.receipt_path(self.root))['state'],'awaiting_first_invocation')
        self.assertEqual(ga.handle(self.root,{},'PreInvocation',self.payload),{})
        self.assertEqual(load_json(gs.receipt_path(self.root))['state'],'confirmed')
        self.assertEqual(self.monitor('Type a message',now=1090),None)
        self.assertEqual(sum(cmd[1]=='send-keys' for cmd in self.commands),2)
        self.assertEqual(len(self.notifications),0)

    def test_preinvocation_before_rescue_never_types_anything(self):
        self.assertEqual(ga.handle(self.root,{},'PreInvocation',self.payload),{})
        self.assertIsNone(self.monitor('Antigravity\nType a message',now=1200))
        self.assertEqual(self.commands,[])
        self.assertEqual(self.notifications,[])

    def test_idle_compose_submits_short_handoff_once_as_literal_text(self):
        result=self.monitor('Antigravity CLI\nGemini 4 Argon High\nType your message')
        self.assertIn('SUBMITTED',result)
        calls=[x for x in self.commands if x[1]=='send-keys']
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0][:5],['tmux','send-keys','-l','-t','@8'])
        self.assertEqual(calls[0][-1],gs.STARTUP_COMMAND)
        self.assertIn('CONTINUE WITH NEXT TASK',calls[0][-1])
        self.assertEqual(calls[1],['tmux','send-keys','-t','@8','Enter'])
        self.assertIsNone(self.monitor('Type your message',now=1060))
        self.assertEqual(len(self.commands),4)  # no extra send-keys
        self.assertEqual(len(self.notifications),0)

    def test_never_type_over_busy_or_approval_or_existing_composer(self):
        screens=['Antigravity working on task\nAnalyzing repository',
                 'Permission required\nAllow once\nDeny\nType a message',
                 'Your prompt: RelayWatch automatic handoff\nType a message',
                 'Choose your account\nType a message',
                 '❯\nIdle', 'Antigravity\nI already typed some instructions']
        for i,screen in enumerate(screens):
            with self.subTest(screen=screen):
                self.assertFalse(gs.empty_composer(screen))
                self.commands=[]
                result=self.monitor(screen,now=1040+i)
                self.assertIsNone(result)
                self.assertFalse(any(x[1]=='send-keys' for x in self.commands))

    def test_unverified_startup_alerts_ntfy_only_after_grace(self):
        self.assertIsNone(self.monitor('TUI loading...',now=1060))
        msg=self.monitor('TUI loading...',now=1075)
        self.assertIn('UNVERIFIED',msg)
        self.assertEqual(len(self.notifications),1)
        self.assertIn('initial handoff',self.notifications[0][0])
        self.assertFalse(any(x[1]=='send-keys' for x in self.commands))
        self.assertIsNone(self.monitor('Type a message',now=1140))
        self.assertEqual(len(self.notifications),1)

    def test_unverified_process_cannot_receive_keys(self):
        with patch('gemini_startup.paused',return_value=False):
            msg=gs.monitor(self.root,{},now=1080,
                run=self.run_screen('Type a message'),
                detect=lambda root,expected_window=None:(None,'unverified'),
                sender=self.sender,active=lambda root:True)
        self.assertIn('UNVERIFIED',msg)
        self.assertFalse(any(x[1]=='send-keys' for x in self.commands))

    def test_sending_failure_does_not_repeat_or_approve(self):
        def run(argv,**kwargs):
            self.commands.append(argv)
            if argv[1]=='list-panes':
                return SimpleNamespace(returncode=0,stdout='%2\n',stderr='')
            if argv[1]=='capture-pane':
                return SimpleNamespace(returncode=0,stdout='Type a message',stderr='')
            if argv[1]=='send-keys':
                return SimpleNamespace(returncode=1,stdout='',stderr='tmux failed')
        with patch('gemini_startup.paused',return_value=False):
            gs.monitor(self.root,{},now=1040,run=run,detect=self.detect,
                       sender=self.sender,active=lambda root:True)
            gs.monitor(self.root,{},now=1080,run=run,detect=self.detect,
                       sender=self.sender,active=lambda root:True)
        self.assertEqual(sum(x[1]=='send-keys' for x in self.commands),1)
        self.assertEqual(len(self.notifications),1)

    def test_other_workspace_and_model_cannot_confirm_or_overwrite_state(self):
        bad={**self.payload,'workspacePaths':['/other']}
        self.assertFalse(gs.hook(self.root,'PreInvocation',bad))
        bad={**self.payload,'modelName':'claude-opus'}
        self.assertFalse(gs.hook(self.root,'PreInvocation',bad))
        self.assertEqual(load_json(gs.receipt_path(self.root))['state'],
                         'awaiting_first_invocation')

    def test_can_adopt_current_v3_22_session_without_duplicate_input(self):
        gs.receipt_path(self.root).unlink()
        # Existing worker started long before the watcher got updated.
        # An old session might have already run and finished a prompt; never
        # re-send just because its composer is now empty.
        self.assertIn('UNVERIFIED',self.monitor('Antigravity\nType a message',now=1080))
        self.assertEqual(load_json(gs.receipt_path(self.root))['event_id'],self.event)
        self.assertEqual(self.notifications[0][0],'Antigravity initial handoff needs attention')
        self.assertFalse(any(cmd[1]=='send-keys' for cmd in self.commands))

    def test_without_native_sessionstart_does_not_type_into_unknown_cli(self):
        state=load_json(gs.receipt_path(self.root))
        state.pop('session_seen_at',None)
        atomic_write_json(gs.receipt_path(self.root),state)
        self.assertIsNone(self.monitor('Antigravity\nType a message',now=1040))
        self.assertIn('UNVERIFIED',self.monitor('Antigravity\nType a message',now=1080))
        self.assertFalse(any(cmd[1]=='send-keys' for cmd in self.commands))

    def test_gate_rejects_other_event_and_worker(self):
        self.assertFalse(gs.gate(self.root,'wrong',timeout=0.01))
        self.assertTrue(gs.gate(self.root,self.event,timeout=0.1))


if __name__=='__main__':
    unittest.main()
