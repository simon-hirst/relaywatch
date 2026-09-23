"""Model picker tests, including Codex live model/list protocol and CLI integration."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOL=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOL))
import model_picker as picker
from install import install

class ModelPickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=pathlib.Path(self.tmp.name)/'some project'
        self.root.mkdir()
        install(self.root)
        self.pref=self.root/'.agent-watchdog/models.json'

    def test_online_codex_efforts_are_model_specific(self):
        entry={'data':[{'model':'gpt-6.1-sol','displayName':'GPT-6.1 Sol','supportedReasoningEfforts':[
            {'reasoningEffort':'high'}, {'reasoningEffort':'xhigh'}]},
            {'id':'hidden','hidden':True,'supportedReasoningEfforts':[{'reasoningEffort':'low'}]},
            {'model':'gpt-6-astra','supportedReasoningEfforts':[{'reasoningEffort':'low'}]}]}
        parsed=picker.normalize_codex(entry)
        self.assertEqual([x['id'] for x in parsed],['gpt-6.1-sol','gpt-6-astra'])
        self.assertEqual(parsed[0]['efforts'],['high','xhigh'])

    def test_model_id_rejects_shell_code(self):
        self.assertFalse(picker.is_model_id('--dangerously-skip-permissions'))
        self.assertFalse(picker.is_model_id('gpt;rm -rf /'))
        self.assertFalse(picker.is_model_id(''))
        self.assertTrue(picker.is_model_id('gpt-6.1-sol'))

    def test_saved_choices_survive_reinstall_and_not_in_project_settings(self):
        with patch.object(picker,'DATA',self.pref):
            picker.save({'claude':{'model':'claude-sonnet-5-5','effort':'medium'},
                         'codex':{'model':'gpt-6-astra','effort':'ultra'}})
            self.assertEqual(picker.saved()['codex']['effort'],'ultra')
            install(self.root)
            self.assertEqual(picker.saved()['claude']['model'],'claude-sonnet-5-5')
        self.assertFalse((self.root/'.codex/config.toml').exists())
        self.assertIsNone(json.loads((self.root/'.claude/settings.local.json').read_text()).get('model'))

    def test_runtime_cli_args_actual_shell_script(self):
        bindir=self.root/'fake-bin';bindir.mkdir()
        logfile=self.root/'arguments.log'
        for agent in ('claude','codex'):
            binary=bindir/agent
            binary.write_text('''#!/usr/bin/env python3
import os,sys
with open(os.environ['FAKE_AGENT_LOG'],'a') as f:
 f.write(os.path.basename(sys.argv[0])+' '+json.dumps(sys.argv[1:])+'\\n')
''' .replace('import os,sys','import os,sys,json'))
            binary.chmod(0o755)
        (self.root/'.agent-watchdog/models.json').write_text(json.dumps({
            'claude':{'model':'claude-sonnet-5-5','effort':'high'},
            'codex':{'model':'gpt-6-astra','effort':'ultra'}}))
        prompt=self.root/'next-agent-prompt.txt';prompt.write_text('Continue with next task')
        env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'FAKE_AGENT_LOG':str(logfile)}
        script=self.root/'.agent-watchdog/interactive-agent.sh'
        for agent in ('claude','codex'):
            cp=subprocess.run(['bash',str(script),agent,str(self.root),str(prompt)],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(cp.returncode,0,cp.stderr)
        lines=logfile.read_text().splitlines()
        self.assertEqual(len(lines),3)
        self.assertEqual(lines[0].split(' ',1)[0],'claude')
        claude=json.loads(lines[0].split(' ',1)[1])
        self.assertEqual(claude[claude.index('--model')+1],'claude-sonnet-5-5')
        self.assertEqual(claude[claude.index('--effort')+1],'high')
        self.assertIn('--rc',claude)
        self.assertNotIn('--dangerously-skip-permissions',claude) # secure default
        self.assertNotIn('Continue with next task',claude)
        self.assertEqual(claude[-1],'--rc')
        codex=json.loads(lines[-1].split(' ',1)[1])
        self.assertEqual(codex[codex.index('--model')+1],'gpt-6-astra')
        self.assertIn('model_reasoning_effort="ultra"',codex)
        self.assertNotIn('--yolo',codex) # secure default
        self.assertIn('unix://',codex)

    def test_native_claude_binary_found_without_inherited_path(self):
        # Reproduces the user's WSL tmux PATH: ~/.local/bin exists but was
        # missing from the PATH inherited by the detached tmux agent shell.
        home=self.root/'fake-home'
        binary=home/'.local/bin/claude'
        binary.parent.mkdir(parents=True)
        logfile=self.root/'native-cli.log'
        binary.write_text('''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_AGENT_LOG"
''')
        binary.chmod(0o755)
        prompt=self.root/'handoff.prompt'
        prompt.write_text('Continue the next task')
        env={**os.environ,'HOME':str(home),'FAKE_AGENT_LOG':str(logfile),
             'PATH':'/usr/local/bin:/usr/bin:/bin'}
        cp=subprocess.run(['bash',str(self.root/'.agent-watchdog/interactive-agent.sh'),
                           'claude',str(self.root),str(prompt)],
                          env=env,text=True,capture_output=True,timeout=12)
        self.assertEqual(cp.returncode,0,cp.stderr)
        self.assertIn('Resolved Claude CLI:',cp.stdout)
        self.assertIn(str(binary),cp.stdout)
        self.assertNotIn('--dangerously-skip-permissions',logfile.read_text())
        self.assertIn('--rc',logfile.read_text())

    def test_missing_claude_reports_wsl_install_instructions(self):
        prompt=self.root/'handoff.prompt'
        prompt.write_text('Continue the next task')
        home=self.root/'empty-home'
        home.mkdir()
        env={**os.environ,'HOME':str(home),'PATH':'/usr/bin:/bin'}
        cp=subprocess.run(['bash',str(self.root/'.agent-watchdog/interactive-agent.sh'),
                           'claude',str(self.root),str(prompt)],
                          env=env,text=True,capture_output=True,timeout=12)
        self.assertEqual(cp.returncode,127)
        self.assertIn('not found in Debian WSL',cp.stderr)
        self.assertIn('claude.ai/install.sh',cp.stderr)

    def test_codex_app_server_model_list_authenticated_protocol(self):
        bindir=self.root/'fake-bin';bindir.mkdir()
        exe=bindir/'codex'
        exe.write_text('''#!/usr/bin/env python3
import json,sys
for line in sys.stdin:
 msg=json.loads(line)
 if msg.get('method')=='initialize':
  print(json.dumps({'id':1,'result':{'userAgent':'test'}}),flush=True)
 if msg.get('method')=='model/list':
  print(json.dumps({'id':2,'result':{'data':[{'model':'gpt-6.1-sol','displayName':'GPT-6.1 Sol','supportedReasoningEfforts':[{'reasoningEffort':'high'},{'reasoningEffort':'xhigh'}]}], 'nextCursor':None}}),flush=True)
''')
        exe.chmod(0o755)
        with patch.dict(os.environ,{'PATH':str(bindir)+os.pathsep+os.environ['PATH']}):
            found=picker.codex_models_from_cli(timeout=3)
        self.assertEqual(found[0]['id'],'gpt-6.1-sol')
        self.assertEqual(found[0]['efforts'],['high','xhigh'])

    def test_website_extracts_valid_claude_models(self):
        class Resp:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return b'<a>claude-opus-5-5</a> <li>claude-sonnet-5-5</li> <li>claude-opus-5-5</li>'
        with patch.object(picker.urllib.request,'urlopen',return_value=Resp()):
            found=picker.claude_models_online()
        self.assertEqual([x['id'] for x in found],['claude-opus-5-5','claude-sonnet-5-5'])

    def test_fail_closed_cancellation_preserves_choices(self):
        path=self.pref
        with patch.object(picker,'DATA',path):
            original=picker.saved()
            with patch.object(picker,'choose_all',side_effect=KeyboardInterrupt):
                try:picker.choose_all({}, {}, original)
                except KeyboardInterrupt:pass
            self.assertEqual(picker.saved(),original)

if __name__=='__main__':unittest.main()
