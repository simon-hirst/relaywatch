"""Regression tests for zero-token Antigravity /usage telemetry.

The Pro fixture has Argon weekly quota but no Argon five-hour bucket,
even though *general* Gemini and Claude/GPT groups both report a 5h quota.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

import gemini_usage as gu
from engine import load_json
from install import install
from console_ui import DashboardUI


def example():
    def bucket(ident,window,fraction,reset):
        return {'id':ident,'name':'Limit Remaining','window':window,
                'remaining_fraction':fraction,'reset_time':reset}
    future='2026-11-06T01:16:36Z'
    return {'conversation_id':'','status':'SUCCESS','response':'display-only TSV ignored',
            'duration_seconds':0,'num_turns':0,
            'usage':{'input_tokens':0,'output_tokens':0,'thinking_tokens':0,
                     'cache_read_tokens':0,'total_tokens':0},
            'command':{'name':'usage','data':{'description':'independent quota groups',
                'groups':[
                  {'name':'Gemini Models','buckets':[
                       bucket('gemini-weekly','weekly',1,future),
                       bucket('gemini-5h','5h',1,'2026-10-30T06:16:36Z')]},
                  {'name':'Claude and GPT models','buckets':[
                       bucket('3p-weekly','weekly',1,future),
                       bucket('3p-5h','5h',1,'2026-10-30T06:16:36Z')]},
                  {'name':'Gemini 4 Argon','buckets':[
                       bucket('gemini-argon-wtu-weekly','weekly',
                              0.9889033436775208,'2026-11-06T00:55:35Z')]}
                ]}}}


class GeminiQuotaTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        install(self.root)
        self.calls=[]
        self.fixture=json.dumps(example())

        def run(argv,**kwargs):
            self.calls.append(argv)
            if '--version' in argv:
                return SimpleNamespace(returncode=0,stdout='1.4.10\n',stderr='')
            if argv[1:]==['-p','/usage','--output-format','json']:
                return SimpleNamespace(returncode=0,stdout=self.fixture,stderr='')
            raise AssertionError('Unexpected CLI call '+str(argv))
        self.run=run

    def test_argon_weekly_uses_own_bucket_not_general_gemini_5h(self):
        parsed=gu.parse(example())
        argon=parsed['Gemini 4 Argon']
        weekly=argon['weekly']
        self.assertEqual(weekly['id'],'gemini-argon-wtu-weekly')
        self.assertAlmostEqual(weekly['remaining_percent'],98.8903,places=4)
        self.assertAlmostEqual(weekly['used_percent'],1.1097,places=4)
        self.assertEqual(weekly['reset_time'],'2026-11-06T00:55:35Z')
        self.assertNotIn('5h',argon)
        self.assertEqual(parsed['Gemini Models']['5h']['remaining_percent'],100)
        self.assertEqual(parsed['Claude and GPT models']['weekly']['remaining_percent'],100)

    def test_cli_probe_runs_zero_token_command_and_throttles(self):
        snapshot=gu.refresh(self.root,now=1000,run=self.run,locate=lambda:'/fake/agy')
        self.assertTrue(snapshot['fresh'])
        self.assertEqual(snapshot['cli_version'],'1.4.10')
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1],['/fake/agy','-p','/usage','--output-format','json'])
        cached=gu.refresh(self.root,now=1100,run=self.run,locate=lambda:'/fake/agy')
        self.assertEqual(len(self.calls),2)
        self.assertTrue(cached['fresh'])
        gu.refresh(self.root,now=1120,run=self.run,locate=lambda:'/fake/agy')
        self.assertEqual(len(self.calls),4)
        self.assertFalse(gu.read(self.root,now=1510)['fresh'])

    def test_old_unsafe_ady_version_refuses_usage_command(self):
        def run(argv,**kwargs):
            self.calls.append(argv)
            return SimpleNamespace(returncode=0,stdout='1.0.9',stderr='')
        res=gu.refresh(self.root,now=1000,run=run,locate=lambda:'/fake/agy')
        self.assertFalse(res['fresh'])
        self.assertIn('>=1.1.12',res['error'])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0],['/fake/agy','--version'])

    def test_missing_ady_does_not_attempt_model_or_cli_call(self):
        res=gu.refresh(self.root,now=1000,run=self.run,locate=lambda:None)
        self.assertIn('missing',res['error'])
        self.assertEqual(self.calls,[])

    def test_malformed_and_nonzero_turn_payloads_are_rejected(self):
        for key,value in [
            ('status','ERROR'),('num_turns',1),('command',{'name':'chat','data':{}}),
            ('usage',{'total_tokens':12}),('usage',None)]:
            with self.subTest(key=key):
                payload=example()
                payload[key]=value
                with self.assertRaises(ValueError):gu.parse(payload)
        payload=example()
        payload['command']['data']['groups'][2]['buckets'][0]['remaining_fraction']=1.5
        with self.assertRaisesRegex(ValueError,'fraction'):gu.parse(payload)
        payload=example()
        payload['command']['data']['groups'][2]['buckets'][0]['reset_time']='2026-11-06'
        with self.assertRaisesRegex(ValueError,'timezone'):gu.parse(payload)

    def test_duplicate_argon_weekly_buckets_are_rejected(self):
        payload=example()
        argon=payload['command']['data']['groups'][2]
        argon['buckets'].append(argon['buckets'][0].copy())
        with self.assertRaisesRegex(ValueError,'Duplicate'):gu.parse(payload)

    def test_after_failed_refresh_keep_last_good_only_if_fresh(self):
        good=gu.refresh(self.root,now=1000,run=self.run,locate=lambda:'/fake/agy')
        self.assertTrue(good['fresh'])
        def faulty(argv,**kwargs):
            if '--version' in argv:
                return SimpleNamespace(returncode=0,stdout='1.4.10',stderr='')
            return SimpleNamespace(returncode=1,stdout='',stderr='AUTH_SECRET_DO_NOT_CACHE')
        attempted=gu.refresh(self.root,now=1201,run=faulty,locate=lambda:'/fake/agy')
        self.assertTrue(attempted['fresh'])
        self.assertIn('exited unsuccessfully',attempted['error'])
        self.assertNotIn('AUTH_SECRET',str(load_json(gu.path(self.root))))
        self.assertEqual(attempted['groups']['Gemini 4 Argon']['weekly']['id'],
                         'gemini-argon-wtu-weekly')
        self.assertFalse(gu.read(self.root,now=1301)['fresh'])

    def test_dashboard_labels_pro_argon_as_weekly_only(self):
        gu.refresh(self.root,now=1000,run=self.run,locate=lambda:'/fake/agy')
        with patch('console_ui.cli_path',return_value='/fake/agy'), \
             patch('console_ui.read_gemini_quota',return_value=gu.read(self.root,now=1100)):
            ui=DashboardUI(self.root)
            rows=ui._lines(125)
            all_lines='\n'.join(text for text,_ in rows)
            self.assertIn('GEMINI',all_lines)
            self.assertIn('weekly 1.1% used',all_lines)
            self.assertIn('98.9% left',all_lines)
            self.assertIn('Weekly-only (Pro)',all_lines)
            self.assertNotIn('Argon 5H  0.0% used',all_lines)
            ui.gemini_active=True
            pending=ui._pending()[0]
            self.assertIn('Weekly-only (Pro)',pending)

    def test_different_account_quota_layout_not_mislabeled_pro(self):
        group=gu.parse(example())['Gemini 4 Argon']
        self.assertTrue(gu.argon_weekly_only_pro(group))
        unknown={'weekly': {**group['weekly'], 'id':'different-plan-weekly'}}
        self.assertFalse(gu.argon_weekly_only_pro(unknown))
        other={**group, '5h':{'remaining_percent': 77.0}}
        self.assertFalse(gu.argon_weekly_only_pro(other))
        with patch('console_ui.read_gemini_quota',return_value={
            'fresh':True,'groups':{'Gemini 4 Argon':unknown}}):
            lines='\n'.join(text for text,_ in DashboardUI(self.root)._lines(125))
            self.assertIn('Argon 5H  not reported',lines)
            self.assertNotIn('Weekly-only (Pro)',lines)
        with patch('console_ui.read_gemini_quota',return_value={
            'fresh':True,'groups':{'Gemini 4 Argon':other}}):
            lines='\n'.join(text for text,_ in DashboardUI(self.root)._lines(125))
            self.assertIn('Argon 5H',lines)
            self.assertNotIn('Weekly-only (Pro)',lines)

    def test_install_copies_and_hashes_quota_module_and_ignores_cache(self):
        import hashlib
        script=self.root/'.agent-watchdog/gemini_usage.py'
        manifest=load_json(self.root/'.agent-watchdog/managed-files.json')
        self.assertEqual(manifest['gemini_usage.py'],hashlib.sha256(script.read_bytes()).hexdigest())
        self.assertIn('.agent-watchdog/gemini-usage.json',
                      (self.root/'.gitignore').read_text())


if __name__=='__main__':
    unittest.main()
