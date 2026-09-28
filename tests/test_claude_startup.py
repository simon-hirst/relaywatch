"""Regression tests for automatic first-turn submission in Claude's tmux TUI."""
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from claude_startup import looks_idle, draft_visible, deliver, INITIAL_MESSAGE, load_state, mark_received
from watchdog import alert_claude_startup_failure
from engine import atomic_write_json

IDLE='''Claude Code v2.1.310
Opus 5.5 with xhigh effort
/remote-control is active

❯ Try "refactor public-gateway.js"
› bypass permissions on
'''
DRAFT=('''Claude Code v2.1.310
Opus 5.5 with xhigh effort
/remote-control is active

❯ ''' + INITIAL_MESSAGE + '''
› bypass permissions on
''')
BUSY='''Claude Code v2.1.310
Opus 5.5 with xhigh effort
❯ Try "refactor public-gateway.js"
thinking...
Esc to interrupt
'''

class ClaudeStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=pathlib.Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_screen_recognition(self):
        self.assertTrue(looks_idle(IDLE))
        self.assertTrue(draft_visible(DRAFT))
        self.assertFalse(draft_visible(IDLE))
        self.assertFalse(looks_idle(DRAFT))
        self.assertFalse(looks_idle('user@linux:~$'))
        self.assertFalse(looks_idle(IDLE+'\n[watchdog] Agent exited (status 127)\n'))

    def run_mocked(self, *, accepted_on_enter=1, call_hook=True):
        commands=[]
        state={'screen':IDLE, 'enter':0}
        def run(args,**kwargs):
            commands.append(args)
            if args[1]=='capture-pane':
                return SimpleNamespace(returncode=0,stdout=state['screen'],stderr='')
            if args[1]=='send-keys' and '-l' in args:
                state['screen']=DRAFT
            elif args[1]=='send-keys' and args[-1]=='Enter':
                state['enter']+=1
                if state['enter']>=accepted_on_enter:
                    state['screen']=BUSY
                    if call_hook:
                        mark_received(self.root, INITIAL_MESSAGE)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        clock=[0.0]
        def now():
            clock[0]+=0.1
            return clock[0]
        result=deliver(self.root,'%3',run=run,sleep=lambda v:None,now=now)
        return result,commands,state

    def test_one_enter_confirmed_by_native_submit_hook(self):
        result,commands,state=self.run_mocked()
        self.assertEqual(result,'confirmed')
        self.assertEqual(load_state(self.root)['confirmation'],'UserPromptSubmit')
        self.assertEqual(state['enter'],1)
        self.assertEqual(len([c for c in commands if '-l' in c]),1)

    def test_retries_enter_only_when_exact_draft_stays_visible(self):
        result,commands,state=self.run_mocked(accepted_on_enter=2)
        self.assertEqual(result,'confirmed')
        self.assertEqual(state['enter'],2)
        self.assertEqual(len([c for c in commands if '-l' in c]),1)

    def test_no_more_than_three_enters_and_no_false_success(self):
        result,commands,state=self.run_mocked(accepted_on_enter=999)
        self.assertIn('not delivered',result)
        self.assertEqual(state['enter'],3)
        self.assertEqual(len([c for c in commands if '-l' in c]),1)
        self.assertEqual(load_state(self.root)['status'],'not_delivered')

    def test_hook_missing_must_not_claim_submission(self):
        result,commands,state=self.run_mocked(call_hook=False)
        self.assertIn('not delivered',result)
        self.assertEqual(state['enter'],1)

    def test_unrelated_hook_cannot_confirm(self):
        result,commands,state=self.run_mocked(call_hook=False)
        self.assertFalse(mark_received(self.root,'something else'))
        self.assertNotEqual(load_state(self.root)['status'],'confirmed')

    def test_never_injects_when_no_tui(self):
        commands=[]
        def run(args,**kwargs):
            commands.append(args)
            return SimpleNamespace(returncode=0,stdout='bash$ ',stderr='')
        clock=[0]
        def now(): clock[0]+=1;return clock[0]
        result=deliver(self.root,'%6',max_seconds=3,run=run,sleep=lambda v:None,now=now)
        self.assertIn('not delivered',result)
        self.assertFalse(any(x[1]=='send-keys' for x in commands))

    def test_bad_pane_never_sent(self):
        self.assertIn('unsafe',deliver(self.root,'; echo hi'))

    def test_single_startup_alert(self):
        path=self.root/'.agent-watchdog'/'claude-prompt-delivery.json'
        path.parent.mkdir(parents=True)
        atomic_write_json(path,{'token':'abcdef','status':'not_delivered','reason':'Claude did not confirm the first message'})
        sender=Mock(return_value={'desktop':'sent','phone':'sent'})
        first=alert_claude_startup_failure(self.root,{'alerts':{}},sender=sender)
        self.assertIn('NOT DELIVERED',first)
        self.assertIsNone(alert_claude_startup_failure(self.root,{},sender=sender))
        self.assertEqual(sender.call_count,1)

if __name__=='__main__':unittest.main()

class HookConfirmationTests(unittest.TestCase):
    def test_hook_confirms_exact_claude_message_without_waiting_for_telemetry(self):
        from hook import hook
        from engine import atomic_write_json
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp)
            atomic_write_json(root/'.agent-watchdog'/'config.json',{'agents':{'claude':{'url':'http://localhost:0'}}})
            atomic_write_json(root/'.agent-watchdog'/'claude-prompt-delivery.json',
                              {'status':'submitting','token':'abc123','pane':'%1'})
            with patch('hook.current',side_effect=AssertionError('Telemetry must not be read before acknowledgement')):
                with self.assertRaises(AssertionError):
                    hook(root,'claude','UserPromptSubmit','session-1',{'prompt':INITIAL_MESSAGE})
            self.assertEqual(load_state(root)['status'],'confirmed')
