"""Native Antigravity input alerts and exact 10%-remaining weekly handoff."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import gemini_attention as ga
from engine import atomic_write_json, load_json
from gemini_session import record_path, gemini_session_name
from install import install, merge_gemini_hooks_file


class GeminiAttentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        install(self.root)
        self.config=load_json(self.root/'.agent-watchdog/config.json')
        self.config['alerts']={'desktop':True,'phone':{
            'enabled':True,'server':'https://ntfy.sh',
            'topic':'test-not-a-real-topic','include_question':False}}
        atomic_write_json(record_path(self.root),{
            'agent':'gemini','session':gemini_session_name(self.root),
            'window':'@4','outcome':'launched','model':'gemini-4-argon-high'})
        self.sent=[]
        def sender(root,config,title,desktop,phone):
            self.sent.append((config,title,desktop,phone))
            return {'desktop':'sent' if config['alerts']['desktop'] else 'disabled',
                    'phone':'sent' if config['alerts']['phone']['enabled'] else 'disabled'}
        self.sender=sender
        self.active=lambda root:True
        self.payload={'conversationId':'conv1','stepIdx':8,'modelName':'gemini-4-argon-high',
                      'workspacePaths':[str(self.root)],
                      'toolCall':{'name':'ask_question','args':{
                          'questions':[{'question':'What is SECRET_TOKEN=12345?',
                                        'options':['approve','reject']}]}}}

    def quota(self,left,reset='2026-11-06T00:55:35Z',*,fresh=True):
        return {'fresh':fresh,'groups':{
           'Gemini Models':{'5h':{'remaining_fraction':0,'id':'gemini-5h'}},
           'Gemini 4 Argon':{'weekly':{
               'id':'gemini-argon-wtu-weekly','window':'weekly',
               'remaining_fraction':left,'remaining_percent':left*100,
               'reset_time':reset}}}}

    def test_native_questions_and_subagent_permissions_forward_to_both_channels(self):
        result=ga.handle(self.root,self.config,'PreToolUse',self.payload,
                         active=self.active,sender=self.sender)
        self.assertEqual(result,{'decision':'allow'})
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0][1],'Antigravity needs an answer')
        self.assertNotIn('SECRET_TOKEN',str(self.sent))
        self.assertTrue(self.sent[0][0]['alerts']['phone']['enabled'])
        self.assertEqual(ga.handle(self.root,self.config,'PreToolUse',self.payload,
                                  active=self.active,sender=self.sender),
                         {'decision':'allow'})
        self.assertEqual(len(self.sent),1)
        second={**self.payload,'stepIdx':9,
                'toolCall':{'name':'ask_permission','args':{'Reason':'private key'}}}
        ga.handle(self.root,self.config,'PreToolUse',second,active=self.active,sender=self.sender)
        self.assertEqual(len(self.sent),2)
        self.assertIn('permission',self.sent[-1][1].lower())
        self.assertNotIn('private key',str(self.sent))

    def test_unrelated_tools_no_alert_and_permissions_never_auto_approved(self):
        for name in ('run_command','invoke_subagent','write_to_file','list_dir'):
            data={**self.payload,'toolCall':{'name':name,'args':{'Password':'secrets'}}}
            self.assertEqual(ga.handle(self.root,self.config,'PreToolUse',data,
                                      active=self.active,sender=self.sender),
                             {'decision':'allow'})
        self.assertEqual(self.sent,[])
        self.assertEqual(ga.handle(self.root,self.config,'PostToolUse',self.payload,
                                  active=self.active,sender=self.sender),{})
        self.assertFalse((self.root/'.agent-watchdog/needs-input.json').exists())

    def test_unmanaged_or_wrong_workspace_cannot_notify(self):
        data={**self.payload,'workspacePaths':['/wrong/workspace']}
        ga.handle(self.root,self.config,'PreToolUse',data,active=self.active,sender=self.sender)
        ga.handle(self.root,self.config,'PreToolUse',self.payload,
                  active=lambda root:False,sender=self.sender)
        self.assertEqual(self.sent,[])

    def test_only_ten_percent_no_twenty_or_other_quotas(self):
        self.assertIsNone(ga.warn_at_ten(self.root,self.config,self.quota(.20),
                                         now=1000,sender=self.sender,active=self.active))
        self.assertIsNone(ga.warn_at_ten(self.root,self.config,self.quota(.100001),
                                         now=1010,sender=self.sender,active=self.active))
        self.assertEqual(self.sent,[])
        event=ga.warn_at_ten(self.root,self.config,self.quota(.10),
                             now=1020,sender=self.sender,active=self.active)
        self.assertIn('<=10%',event)
        self.assertEqual(len(self.sent),1)
        self.assertTrue(self.sent[0][0]['alerts']['phone']['enabled'])
        self.assertIn('docs/HANDOFF.md',self.sent[0][2])
        self.assertNotIn('20%',self.sent[0][2])
        self.assertIsNone(ga.warn_at_ten(self.root,self.config,self.quota(.09),
                                         now=1050,sender=self.sender,active=self.active))
        self.assertEqual(len(self.sent),1)
        # A NEW weekly reset is a new episode (not every poll).
        ga.warn_at_ten(self.root,self.config,self.quota(.08,reset='2026-11-13T00:55:35Z'),
                       now=1100,sender=self.sender,active=self.active)
        self.assertEqual(len(self.sent),2)

    def test_quota_stale_unowned_and_general_gemini_does_not_trigger(self):
        self.assertIsNone(ga.warn_at_ten(self.root,self.config,self.quota(.01,fresh=False),
                                         now=1000,sender=self.sender,active=self.active))
        self.assertIsNone(ga.warn_at_ten(self.root,self.config,self.quota(.01),
                                         now=1001,sender=self.sender,active=lambda root:False))
        empty={'fresh':True,'groups':{'Gemini Models':{'weekly':{
            'id':'gemini-weekly','remaining_fraction':0,'reset_time':'2026-11-06T00:00:00Z'}}}}
        self.assertIsNone(ga.warn_at_ten(self.root,self.config,empty,
                                         now=1002,sender=self.sender,active=self.active))
        self.assertEqual(self.sent,[])

    def test_single_ephemeral_handoff_warning_on_actual_model_turn(self):
        quota=self.quota(.09)
        self.assertEqual(ga.inject_handoff_note(self.root,self.payload,active=self.active,
                       quota_reader=lambda root:quota),{})
        ga.warn_at_ten(self.root,self.config,quota,now=1000,sender=self.sender,
                       active=self.active)
        first=ga.inject_handoff_note(self.root,self.payload,active=self.active,
                       quota_reader=lambda root:quota)
        self.assertIn('injectSteps',first)
        self.assertIn('docs/HANDOFF.md',first['injectSteps'][0]['ephemeralMessage'])
        self.assertNotIn('20%',first['injectSteps'][0]['ephemeralMessage'])
        self.assertEqual(ga.inject_handoff_note(self.root,self.payload,active=self.active,
                       quota_reader=lambda root:quota),{})
        other={**self.payload,'modelName':'gemini-3.6-flash-medium'}
        self.assertEqual(ga.inject_handoff_note(self.root,other,active=self.active,
                       quota_reader=lambda root:quota),{})

    def test_missed_warning_wont_inject_stale_or_without_session(self):
        quota=self.quota(.09)
        ga.warn_at_ten(self.root,self.config,quota,now=1000,sender=self.sender,
                       active=self.active)
        self.assertEqual(ga.inject_handoff_note(self.root,self.payload,active=self.active,
                       quota_reader=lambda root:{**quota,'fresh':False}),{})
        self.assertEqual(ga.inject_handoff_note(self.root,self.payload,
                       active=lambda root:False,quota_reader=lambda root:quota),{})

    def test_per_channel_retry_doesnt_duplicate_sent_phone(self):
        quota=self.quota(.07)
        seen=[]
        def failing(root,config,title,desktop,phone):
            seen.append(config['alerts'])
            return {'desktop':'failed' if config['alerts']['desktop'] else 'disabled',
                    'phone':'sent' if config['alerts']['phone']['enabled'] else 'disabled'}
        ga.warn_at_ten(self.root,self.config,quota,now=1000,sender=failing,active=self.active)
        ga.warn_at_ten(self.root,self.config,quota,now=1119,sender=failing,active=self.active)
        self.assertEqual(len(seen),1)
        ga.warn_at_ten(self.root,self.config,quota,now=1120,sender=failing,active=self.active)
        self.assertEqual(len(seen),2)
        self.assertFalse(seen[-1]['phone']['enabled'])
        self.assertTrue(seen[-1]['desktop'])

    def test_native_hook_file_preserves_other_hooks_and_reinstall_deduplicates(self):
        path=self.root/'.agents/hooks.json'
        original=load_json(path)
        self.assertIn('relaywatch-gemini-attention',original)
        cfg=original['relaywatch-gemini-attention']
        self.assertEqual(cfg['PreToolUse'][0]['matcher'],'^(ask_question|ask_permission)$')
        self.assertIn('decision',ga.handle(self.root,self.config,'PreToolUse',
                                          self.payload,active=self.active,sender=self.sender))
        original['another-hook']={'Stop':[{'hooks':[{'command':'echo hello'}]}]}
        path.write_text(json.dumps(original))
        install(self.root)
        after=load_json(path)
        self.assertEqual(after['another-hook'],original['another-hook'])
        self.assertEqual(len(after['relaywatch-gemini-attention']['PreToolUse']),1)
        self.assertTrue((self.root/'.agent-watchdog/gemini_attention.py').is_file())

    def test_visible_permission_card_detects_conservative_signals(self):
        screen='Antigravity working\nPermission required\nAllow once\nAlways allow\nDeny\n'
        self.assertIsNotNone(ga.visible_prompt(screen))
        self.assertIsNone(ga.visible_prompt('Log line: Allow once; Deny was printed'))
        self.assertIsNone(ga.visible_prompt('Would you like to continue?'))
        class FakeRun:
            def __init__(self): self.calls=[]
            def __call__(self,argv,**kwargs):
                self.calls.append(argv)
                return (SimpleNamespace(returncode=0,stdout='%1\n',stderr='')
                        if argv[1]=='list-panes'
                        else SimpleNamespace(returncode=0,stdout=screen,stderr=''))
        runner=FakeRun()
        result=ga.check_visible(self.root,self.config,now=1000,run=runner,
                                sender=self.sender,active=self.active)
        self.assertIn('VISIBLE APPROVAL',result)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(runner.calls[1][:3],['tmux','capture-pane','-p'])
        self.assertIsNone(ga.check_visible(self.root,self.config,now=1020,run=runner,
                                          sender=self.sender,active=self.active))
        self.assertEqual(len(self.sent),1)

    def test_no_local_approval_or_tmux_send_keys_anywhere(self):
        code=(ROOT/'gemini_attention.py').read_text()
        self.assertNotIn('send-keys',code)
        self.assertNotIn("'auto_approve'",code.split("def handle")[1].split("def main")[0].replace(
            "        # Crucial: "+'"allow" delegates to the usual Antigravity permission'+"\\n",''))


if __name__=='__main__':
    unittest.main()
