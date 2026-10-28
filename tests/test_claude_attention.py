"""Alerts for Claude native permission, background-agent, and MCP input prompts.

All hooks must be advisory: no implicit authorization, no prompt input
automation, no changes to the blocking needs-input.json lifecycle.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from claude_attention import describe, forward, path
from engine import atomic_write_json, load_json
from hook import hook
from install import install
from runner import session_name


class ClaudeAttentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        install(self.root)
        self.config=load_json(self.root/'.agent-watchdog/config.json')
        atomic_write_json(self.root/'.agent-watchdog/worker.json',{
            'type':'tmux','agent':'claude','outcome':'launched',
            'session':session_name(self.root),'window':'@4'})
        self.calls=[]
        def sender(root,config,title,desktop,phone):
            self.calls.append((title,desktop,phone))
            return {'desktop':'sent','phone':'sent'}
        self.sender=sender
        self.payload={
            'session_id':'claude-remote-1',
            'tool_name':'Bash',
            'tool_input':{'command':'rm -rf secret-folder',
                          'description':'Remove TOP_SECRET_TOKEN=abc123'},
            'permission_suggestions':[{'type':'addRules','behavior':'allow'}],
        }

    def forward(self,event,payload=None,now=1000):
        return forward(self.root,self.config,event,payload or self.payload,
                       now=now,sender=self.sender)

    def test_native_permission_alert_is_immediate_and_private(self):
        out=self.forward('PermissionRequest')
        self.assertEqual(out['desktop'],'sent')
        self.assertEqual(len(self.calls),1)
        self.assertIn('Bash',self.calls[0][0])
        content=str(self.calls)
        self.assertNotIn('rm -rf',content)
        self.assertNotIn('TOP_SECRET',content)
        self.assertNotIn('secret-folder',content)
        self.assertFalse((self.root/'.agent-watchdog/needs-input.json').exists())
        state=load_json(path(self.root))
        self.assertFalse('tool_input' in str(state))
        self.assertEqual(state['last_kind'],'permission')

    def test_duplicate_native_permission_not_resent_immediately(self):
        self.forward('PermissionRequest',now=1000)
        self.assertIsNone(self.forward('PermissionRequest',now=1010))
        self.assertEqual(len(self.calls),1)
        self.forward('PermissionRequest',now=1080)
        self.assertEqual(len(self.calls),2)

    def test_distinct_permissions_and_sessions_are_separate(self):
        self.forward('PermissionRequest',now=1000)
        newer={**self.payload,'tool_input':{'command':'npm install'}}
        self.forward('PermissionRequest',payload=newer,now=1010)
        another={**self.payload,'session_id':'another-subagent'}
        self.forward('PermissionRequest',payload=another,now=1011)
        self.assertEqual(len(self.calls),3)

    def test_notification_fallback_deduplicates_recent_permission(self):
        self.forward('PermissionRequest',now=1000)
        payload={'session_id':'claude-remote-1','notification_type':'permission_prompt',
                 'message':'Remove secret-folder?'}
        self.assertIsNone(self.forward('Notification',payload,now=1007))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.forward('Notification',payload,now=1050)['phone'],'sent')
        self.assertEqual(len(self.calls),2)

    def test_fallback_only_can_alert_when_immediate_hook_was_unavailable(self):
        payload={'session_id':'claude-remote-1','notification_type':'permission_prompt'}
        self.assertIn('sent',self.forward('Notification',payload,now=1000).values())

    def test_agent_teammate_prompt_alerts_without_agent_start_noise(self):
        prompt={'session_id':'claude-remote-1','notification_type':'agent_needs_input',
                'message':'Teammate wants a private passphrase'}
        self.assertEqual(self.forward('Notification',prompt)['phone'],'sent')
        self.assertIn('background agent',self.calls[0][0].lower())
        self.assertNotIn('passphrase',str(self.calls))
        self.assertIsNone(self.forward('Notification',{
            'session_id':'claude-remote-1','notification_type':'agent_completed'}))
        self.assertEqual(len(self.calls),1)

    def test_mcp_elicitation_and_late_dialog_de_duplicate_independently(self):
        prompt={'session_id':'claude-remote-1',
                'mcp_server_name':'internal-secrets-service','mode':'form',
                'elicitation_id':'request-1',
                'message':'Enter token SECRET_TOKEN=do-not-send'}
        self.assertEqual(self.forward('Elicitation',prompt)['desktop'],'sent')
        self.assertNotIn('SECRET_TOKEN',str(self.calls))
        self.assertIsNone(self.forward('Elicitation',prompt,now=1010))
        notified={'session_id':'claude-remote-1',
                  'notification_type':'elicitation_dialog'}
        self.assertEqual(self.forward('Notification',notified,now=1010)['desktop'],'sent')
        self.assertEqual(len(self.calls),2)

    def test_notification_ignores_idles_completions_and_non_inputs(self):
        for kind in ('idle_prompt','agent_completed','auth_success',None):
            self.assertIsNone(self.forward('Notification', {
                'session_id':'s','notification_type':kind,'message':'Anything'}))
        self.assertEqual(self.calls,[])

    def test_delivery_failure_can_retry_same_prompt(self):
        def failing(*args): return {'desktop':'desktop unavailable','phone':'disabled'}
        self.assertEqual(forward(self.root,self.config,'PermissionRequest',self.payload,
                                 now=1000,sender=failing)['desktop'],'desktop unavailable')
        self.assertEqual(self.forward('PermissionRequest',now=1001)['desktop'],'sent')
        self.assertEqual(len(self.calls),1)

    def test_hook_is_advisory_before_quota_parsing(self):
        for event,payload in [
            ('PermissionRequest',self.payload),
            ('Notification',{'session_id':'claude-remote-1','notification_type':'agent_needs_input'}),
            ('Elicitation',{'session_id':'claude-remote-1','mcp_server_name':'server','mode':'form'}),
        ]:
            with patch('hook.tmux_exists',return_value=True), \
                 patch('hook.forward_claude_attention',return_value={'desktop':'sent'}) as alerted, \
                 patch('hook.current',side_effect=AssertionError('quota is irrelevant for alerts')):
                result=hook(self.root,'claude',event,'claude-remote-1',payload)
            self.assertEqual(result,{})
            alerted.assert_called_once()
            self.assertFalse((self.root/'.agent-watchdog/needs-input.json').exists())

    def test_not_owned_cannot_send_notifications(self):
        atomic_write_json(self.root/'.agent-watchdog/worker.json',{
            'type':'tmux','agent':'codex','outcome':'launched',
            'session':session_name(self.root),'window':'@4'})
        with patch('hook.forward_claude_attention') as alert, \
             patch('hook.tmux_exists',side_effect=AssertionError('do not inspect alien tmux')):
            self.assertEqual(hook(self.root,'claude','PermissionRequest','foo',self.payload),{})
        alert.assert_not_called()

    def test_install_registers_hooks_once_and_ignores_local_state(self):
        install(self.root)
        settings=load_json(self.root/'.claude/settings.local.json')
        for kind in ('PermissionRequest','Notification','Elicitation'):
            with self.subTest(kind=kind):
                self.assertEqual(len(settings['hooks'][kind]),1)
        self.assertTrue((self.root/'.agent-watchdog/claude_attention.py').exists())
        self.assertIn('.agent-watchdog/claude-attention.json',
                      (self.root/'.gitignore').read_text())
        self.assertNotIn('PermissionRequest',
            load_json(self.root/'.codex/hooks.json')['hooks'])

if __name__=='__main__':
    unittest.main()
