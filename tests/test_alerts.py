import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOL))
from alerts import extract_question,register_question,clear_question,request_path, notify_pending_question, send_event
from engine import load_json, atomic_write_json
from runner import START_PROMPT
from hook import hook
from install import install

class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        install(self.root)
        self.cfg=load_json(self.root/'.agent-watchdog'/'config.json')
        self.cfg['alerts']['phone']['enabled'] = True
        atomic_write_json(self.root/'.agent-watchdog'/'worker.json',{
            'type':'tmux','agent':'codex','outcome':'launched','window':'@1'})
    def test_default_phone_config_is_random_and_not_in_handoff(self):
        topic=self.cfg['alerts']['phone']['topic']
        self.assertTrue(topic.startswith('relaywatch-'))
        self.assertGreaterEqual(len(topic),30)
        self.assertNotIn(topic,(self.root/'docs'/'WORKFLOW.md').read_text())
        self.assertTrue(self.cfg['alerts']['desktop'])
        self.assertFalse(self.cfg['alerts']['phone']['include_question'])
        self.assertTrue(self.cfg['alerts']['phone']['enabled']) # enabled explicitly for alert tests
        self.assertTrue((self.root/'.agent-watchdog'/'desktop-alert.ps1').exists())
    def test_repeat_install_keeps_topic(self):
        first=self.cfg['alerts']['phone']['topic']
        install(self.root)
        self.assertEqual(load_json(self.root/'.agent-watchdog'/'config.json')['alerts']['phone']['topic'],first)
    def test_takeover_summary_and_continuation(self):
        self.assertIn('BEFORE doing implementation',START_PROMPT)
        self.assertIn('completed milestones',START_PROMPT)
        self.assertIn('CONTINUE WITH NEXT TASK',START_PROMPT)
        self.assertIn('CONTINUE WITH NEXT TASK',(self.root/'CLAUDE.md').read_text())
    def test_explicit_marker_only(self):
        self.assertIsNone(extract_question('Is that okay? I can proceed.'))
        self.assertEqual(extract_question('All blocked.\nNEEDS_USER_INPUT: What is the callback URL?'), 'What is the callback URL?')
    def test_hook_stops_for_question_and_user_reply_clears(self):
        info={'stage':'normal','agent':'codex'}
        with patch('hook.current',return_value=info):
            result=hook(self.root,'codex','Stop','codex123',{'last_assistant_message':'NEEDS_USER_INPUT: Which staging key should I use?'})
            self.assertEqual(result,{})
            self.assertEqual(load_json(request_path(self.root))['status'],'open')
            self.assertEqual(hook(self.root,'codex','Stop','codex123',{}),{})
            hook(self.root,'codex','UserPromptSubmit','codex123',{'prompt':'Use staging key B for the test.'})
            self.assertEqual(load_json(request_path(self.root))['status'],'answered')
    def test_poll_deduplicates_delivered_and_does_not_push_question_text(self):
        register_question(self.root,'codex','What is the private incident management routing key?',now=100)
        desktop=[];phone=[]
        def d(root,title,message): desktop.append(message);return 'sent'
        def p(config,title,message): phone.append(message);return 'sent'
        out=notify_pending_question(self.root,self.cfg,now=1000,desktop_sender=d,phone_sender=p)
        self.assertIn('desktop=sent',out)
        self.assertEqual(len(desktop),1)
        self.assertEqual(len(phone),1)
        self.assertIn('incident management',desktop[0])
        self.assertNotIn('incident management',phone[0])
        self.assertIsNone(notify_pending_question(self.root,self.cfg,now=1200,desktop_sender=d,phone_sender=p))
        self.assertEqual(len(desktop),1)
    def test_new_question_notifies_again(self):
        d=[]
        p=[]
        def desktop(*args):d.append('x');return 'sent'
        def phone(*args):p.append('x');return 'sent'
        register_question(self.root,'codex','First decision',now=111)
        notify_pending_question(self.root,self.cfg,now=1000,desktop_sender=desktop,phone_sender=phone)
        register_question(self.root,'codex','Another decision',now=222)
        notify_pending_question(self.root,self.cfg,now=1001,desktop_sender=desktop,phone_sender=phone)
        self.assertEqual(len(d),2)
        self.assertEqual(len(p),2)
    def test_failed_channel_retries_without_spamming_successful_one(self):
        calls={'desktop':0,'phone':0}
        def d(*args):calls['desktop']+=1;return 'sent'
        def p(*args):calls['phone']+=1;return 'phone unavailable: URLError' if calls['phone']==1 else 'sent'
        register_question(self.root,'claude','Please confirm?',now=100)
        notify_pending_question(self.root,self.cfg,now=1000,desktop_sender=d,phone_sender=p)
        notify_pending_question(self.root,self.cfg,now=1010,desktop_sender=d,phone_sender=p)
        notify_pending_question(self.root,self.cfg,now=1201,desktop_sender=d,phone_sender=p)
        self.assertEqual(calls,{'desktop':1,'phone':2})
    def test_question_written_directly_by_agent_is_picked_up(self):
        atomic_write_json(request_path(self.root),{'agent':'claude','question':'What domain?','status':'open'})
        self.assertIn('NEEDS USER INPUT',notify_pending_question(self.root,self.cfg,now=1000,
              desktop_sender=lambda *a:'sent',phone_sender=lambda *a:'sent'))
    def test_desktop_disabled(self):
        self.cfg['alerts']['desktop']=False
        register_question(self.root,'claude','An issue?',now=100)
        notify_pending_question(self.root,self.cfg,now=1000,desktop_sender=lambda *a:self.fail('desktop unexpectedly called'),phone_sender=lambda *a:'sent')
    def test_user_reply_does_not_clear_other_agents_question(self):
        register_question(self.root,'claude','What next?')
        self.assertFalse(clear_question(self.root,'codex'))
        self.assertEqual(load_json(request_path(self.root))['status'],'open')
    def test_privacy_unsafe_topic_rejected(self):
        from alerts import _phone_push
        self.cfg['alerts']['phone']['topic']='bad/../topic'
        self.assertEqual(_phone_push(self.cfg['alerts'],'title','body'), 'invalid topic')

if __name__=='__main__':unittest.main()
