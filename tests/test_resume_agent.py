"""v3.7 regression tests for user-requested resume of an existing TUI."""
from __future__ import annotations
import io,contextlib,tempfile,unittest,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from install import install
from engine import load_json,atomic_write_json
from resume_agent import MESSAGE, mark_received, resume_existing, state_path
from runner import session_name

CLAUDE_IDLE='''Claude Code v2.1.310\nOpus 5.5\n❯ Try "refactor"\n› bypass permissions on\n'''
CODEX_IDLE='''OpenAI Codex\n● GPT\n› Ask Codex to do anything\n'''

def draft(agent):
    return (CLAUDE_IDLE if agent=='claude' else CODEX_IDLE) + '\n❯ '+MESSAGE+'\n'

class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        with contextlib.redirect_stdout(io.StringIO()):install(self.root)
        self.config=load_json(self.root/'.agent-watchdog'/'config.json')
        self.worker={'agent':'claude','type':'tmux','outcome':'launched','window':'@2','session':session_name(self.root)}
    def simulate(self,agent='claude',enter_accept=1,hook=True,initial=None):
        commands=[]
        tui={'text': initial if initial is not None else CLAUDE_IDLE if agent=='claude' else CODEX_IDLE,'enters':0}
        def run(args,**kw):
            commands.append(args)
            if args[1]=='list-panes':return SimpleNamespace(returncode=0,stdout='%9\n',stderr='')
            if args[1]=='capture-pane':return SimpleNamespace(returncode=0,stdout=tui['text'],stderr='')
            if args[1]=='send-keys' and '-l' in args:
                tui['text']=draft(agent)
            elif args[1]=='send-keys' and args[-1]=='Enter':
                tui['enters']+=1
                if tui['enters']>=enter_accept:
                    tui['text']='Claude Code\nThinking...\n' if agent=='claude' else 'Codex\nWorking...\n'
                    if hook:mark_received(self.root,agent,MESSAGE)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        worker={**self.worker,'agent':agent}
        with patch('resume_agent.latest_worker',return_value=worker),\
             patch('resume_agent.current',return_value={'stage':'normal','remaining_percent':60}),\
             patch('resume_agent.tmux_exists',return_value=True):
            result=resume_existing(self.root,self.config,run=run,sleep=lambda _:None)
        return result,commands,tui
    def test_claude_works_without_new_session(self):
        msg,cmds,tui=self.simulate()
        self.assertIn('RESUMED claude',msg)
        self.assertEqual(tui['enters'],1)
        self.assertEqual(sum('-l' in c for c in cmds),1)
        self.assertEqual(load_json(state_path(self.root))['status'],'confirmed')
    def test_retry_enter_but_never_retype_text(self):
        msg,cmds,tui=self.simulate(enter_accept=2)
        self.assertIn('RESUMED claude',msg)
        self.assertEqual(tui['enters'],2)
        self.assertEqual(sum('-l' in c for c in cmds),1)
    def test_codex_same_session(self):
        msg,cmds,tui=self.simulate(agent='codex')
        self.assertIn('RESUMED codex',msg)
        self.assertEqual(tui['enters'],1)
    def test_busy_does_not_inject(self):
        msg,cmds,tui=self.simulate(initial='Claude Code\nEsc to interrupt\nWorking...\n')
        self.assertIn('busy',msg)
        self.assertFalse(any('send-keys' in c for c in cmds))
    def test_existing_draft_does_not_inject(self):
        msg,cmds,tui=self.simulate(initial=draft('claude'))
        self.assertIn('busy',msg)
        self.assertFalse(any('send-keys' in c for c in cmds))
    def test_block_question(self):
        atomic_write_json(self.root/'.agent-watchdog'/'needs-input.json',{'agent':'claude','status':'open','question':'Approve?'})
        msg,cmds,tui=self.simulate()
        self.assertIn('question',msg)
        self.assertFalse(any('send-keys' in c for c in cmds))
    def test_no_quota_or_unknown_quota(self):
        with patch('resume_agent.latest_worker',return_value=self.worker),\
             patch('resume_agent.current',return_value={'stage':'unknown','remaining_percent':None}):
            result=resume_existing(self.root,self.config)
        self.assertIn('quota',result)
    def test_unconfirmed_submission_not_claimed(self):
        msg,cmds,tui=self.simulate(hook=False)
        self.assertIn('not confirmed',msg)
        self.assertEqual(tui['enters'],1)
    def test_composer_swallowed_enter_returns_prompt_to_user(self):
        msg,cmds,tui=self.simulate(enter_accept=999,hook=False)
        self.assertIn('press Enter manually',msg)
        self.assertEqual(tui['enters'],3)
        self.assertEqual(sum('-l' in c for c in cmds),1)
    def test_confirmation_rejects_unrelated_text(self):
        self.assertFalse(mark_received(self.root,'claude','unrelated'))

if __name__=='__main__':unittest.main()

class RealWorldResumeTests(ResumeTests):
    """Realistic terminal captures: startup banner scrolled away after hours."""
    def test_idle_claude_without_startup_banner(self):
        screen='''\n● Completed verification successfully.\n  Changed docs and tests.\n\n────────────────────────────────────────────────────────\n❯ \n────────────────────────────────────────────────────────\n  ⏵⏵ bypass permissions on (shift+tab to cycle) · install gh for PR status\n'''
        msg,cmds,tui=self.simulate(initial=screen)
        self.assertIn('RESUMED claude',msg)
        self.assertEqual(tui['enters'],1)
        # Capture only live screen rather than potentially stale scrollback.
        captures=[c for c in cmds if c[1]=='capture-pane']
        self.assertTrue(captures)
        self.assertTrue(all('-S' not in c for c in captures))

    def test_claude_with_draft_that_begins_try_is_not_idle(self):
        screen='''\nPreviously completed a task\n❯ Try to continue without asking the user\n────────────────────────────────────\n ⏵⏵ bypass permissions on (shift+tab to cycle)\n'''
        msg,cmds,_=self.simulate(initial=screen)
        self.assertIn('existing text',msg)
        self.assertFalse(any('send-keys' in c for c in cmds))

    def test_claude_interruption_controls_are_not_idle(self):
        screen='''\n✽ Thinking… (esc to interrupt)\n────────────────────────────────────\n❯\n────────────────────────────────────\n ⏵⏵ bypass permissions on (shift+tab to cycle)\n'''
        msg,cmds,_=self.simulate(initial=screen)
        self.assertIn('active work',msg)
        self.assertFalse(any('send-keys' in c for c in cmds))

    def test_claude_history_prompt_does_not_mean_idle(self):
        screen='''\n❯\n────────────────────────────────────\n⏵⏵ bypass permissions on (shift+tab to cycle)\n⏺ Running ongoing work\n⎿ Test output\n'''
        msg,cmds,_=self.simulate(initial=screen)
        self.assertIn('activity below',msg)
        self.assertFalse(any('send-keys' in c for c in cmds))

    def test_check_only_leaves_agent_untouched(self):
        from resume_agent import resume_existing
        def run(args,**kwargs):
            if args[1]=='list-panes':return SimpleNamespace(returncode=0,stdout='%9\n',stderr='')
            if args[1]=='capture-pane':return SimpleNamespace(returncode=0,stdout=CLAUDE_IDLE,stderr='')
            raise AssertionError(f'Unexpected command in check_only: {args}')
        with patch('resume_agent.latest_worker',return_value=self.worker),\
             patch('resume_agent.current',return_value={'stage':'normal','remaining_percent':60}),\
             patch('resume_agent.tmux_exists',return_value=True):
            msg=resume_existing(self.root,self.config,run=run,sleep=lambda _:None,check_only=True)
        self.assertIn('READY TO RESUME claude',msg)
        self.assertFalse(state_path(self.root).exists())
