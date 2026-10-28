import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

TOOL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOL))
from install import install
from engine import load_json, update
from runner import (mark_ready, maybe_launch, explicit_codex, launch, ready_path,
                    handoff_is_real, pid_running, show_agent_window, session_name)
from hook import hook

class FakeProcess:
    pid=99999999


def make_handoff(root):
    path=root/'docs'/'HANDOFF.md'
    path.write_text('## Complete handoff\n\nVerified local tests passed; no deploy. '\
                    'Remaining priority: implement the theme in the streaming panel. '\
                    'Changes and git diff preserved. '*8)
    return path


def quota(agent,used=49,stage='normal'):
    return {'agent':agent, 'stage':stage, 'used_percent':used,
            'remaining_percent':100-used, 'timestamp':time.time()}


class AutolaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        install(self.root)
        self.config=load_json(self.root/'.agent-watchdog/config.json')
        self.config['auto_launch_next_agent']=True # explicit opt-in for managed-launch tests
    def tearDown(self):self.temp.cleanup()

    def test_installer_upgrades_old_config_and_preserves_docs(self):
        path=self.root/'.agent-watchdog/config.json'
        old=load_json(path);old.pop('auto_launch_next_agent');old.pop('auto_launch_grace_seconds');old['auto_launch_max_marker_age_seconds']=7200  # legacy option ignored, preserved
        old['enable_codex_queue']=False
        path.write_text(json.dumps(old))
        workflow=self.root/'docs'/'WORKFLOW.md'
        workflow.write_text('Original team instructions\n')
        install(self.root);install(self.root)
        new=load_json(path)
        self.assertFalse(new['auto_launch_next_agent'])
        self.assertFalse(new['enable_codex_queue'])
        self.assertEqual(new['auto_launch_max_marker_age_seconds'],7200)
        self.assertEqual(workflow.read_text().count('BEGIN AUTO-AGENT-LAUNCH'),1)
        self.assertIn('Original team instructions',workflow.read_text())
        gitignore=(self.root/'.gitignore').read_text()
        self.assertIn('.agent-watchdog/logs/',gitignore)
        self.assertEqual(gitignore.count('.agent-watchdog/logs/'),1)
        self.assertTrue((self.root/'.agent-watchdog/model_picker.py').exists())
        self.assertFalse((self.root/'.agent-watchdog/models.json').exists())
        self.assertIn('.agent-watchdog/models.json',gitignore)
        self.assertEqual(load_json(self.root/'.claude/settings.local.json').get('model'),None)

    def test_marker_only_after_verified_stop(self):
        with patch('engine.get_api_usage',return_value={**quota('claude',93,'handoff'),'limiting_metric':'$.five_hour.used_percent','metrics':[]}):
            update(self.root,'claude',self.config)
            self.assertEqual(hook(self.root,'claude','Stop','c1',{}).get('decision'),'block')
            self.assertFalse(ready_path(self.root,'claude').exists())
            make_handoff(self.root)
            reports=self.root/'docs'/'session-reports'
            reports.mkdir(exist_ok=True)
            (reports/'2026-10-26T12-00-00Z-claude.md').write_text('Completed: feature. Verification: tests passed. Next: implement remaining tasks. '*5)
            self.assertEqual(hook(self.root,'claude','Stop','c1',{}),{})
            marker=load_json(ready_path(self.root,'claude'))
            self.assertEqual(marker['target'],'codex')
            self.assertEqual(marker['session_id'],'c1')

    def test_auto_launch_once_in_interactive_tmux_and_correct_project(self):
        file=make_handoff(self.root)
        event=mark_ready(self.root,'claude','c1',file)
        commands=[]
        def fake_run(argv,**kwargs):
            commands.append(argv)
            return SimpleNamespace(returncode=1 if argv[1]=='has-session' else 0,stderr='',stdout='@1' if argv[1]=='new-session' else '')
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            result=maybe_launch(self.root,self.config,{'codex':quota('codex')},now=event['created_at']+11)
            self.assertTrue(result.startswith('CREATED codex interactive tmux'),result)
            tmux_new=[arg for arg in commands if arg[1]=='new-session'][0]
            self.assertEqual(tmux_new[2:4],['-d','-P'])
            self.assertIn('window_id',tmux_new[5])
            self.assertFalse(any(c[1]=='display-message' for c in commands))
            self.assertIn(str(self.root.resolve()),tmux_new)
            self.assertIn('interactive-agent.sh',tmux_new[-1])
            self.assertNotIn('codex exec',tmux_new[-1])
            self.assertIsNone(maybe_launch(self.root,self.config,{'codex':quota('codex')},now=event['created_at']+12))
        worker=load_json(self.root/'.agent-watchdog/worker.json')
        self.assertEqual(worker['type'],'tmux')
        self.assertEqual(worker['agent'],'codex')

    def test_waiting_handoff_survives_hours_and_safe_handoff_edit(self):
        file=make_handoff(self.root)
        event=mark_ready(self.root,'claude','c1',file)
        unavailable={'claude':quota('claude',96,'handoff'),
                     'codex':quota('codex',85,'prepare')}
        with patch('runner.subprocess.run') as run:
            waiting=maybe_launch(self.root,self.config,unavailable,now=event['created_at']+8000)
            self.assertIn('WAITING for quota',waiting)
            self.assertIsNone(maybe_launch(self.root,self.config,unavailable,now=event['created_at']+9000))
            run.assert_not_called()
        file.write_text(file.read_text()+' safe subsequent edit')
        commands=[]
        def fake_run(argv,**kwargs):
            commands.append(argv)
            return SimpleNamespace(returncode=1 if argv[1]=='has-session' else 0,
                                   stdout='@1' if argv[1]=='new-session' else '',stderr='')
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            result=maybe_launch(self.root,self.config,{'claude':quota('claude',96,'handoff'),
                                'codex':quota('codex',5,'normal')},now=event['created_at']+10000)
            self.assertIn('CREATED codex',result)
            self.assertIsNone(maybe_launch(self.root,self.config,{'codex':quota('codex',5,'normal')},now=event['created_at']+10001))
        self.assertEqual(sum(cmd[1]=='new-session' for cmd in commands),1)
        self.assertEqual(load_json(self.root/'.agent-watchdog/pending-quota.json')['status'],'resumed')

    def test_first_provider_to_recover_can_be_same_source(self):
        file=make_handoff(self.root)
        event=mark_ready(self.root,'codex','c1',file)
        self.assertIn('WAITING for quota',maybe_launch(self.root,self.config,{
            'codex':quota('codex',96,'handoff'), 'claude':quota('claude',94,'handoff')},
            now=event['created_at']+15))
        commands=[]
        def fake_run(argv,**kwargs):
            commands.append(argv)
            return SimpleNamespace(returncode=1 if argv[1]=='has-session' else 0,
                                   stdout='@1' if argv[1]=='new-session' else '',stderr='')
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            result=maybe_launch(self.root,self.config,{
                'codex':quota('codex',0,'normal'), 'claude':quota('claude',94,'handoff')},
                now=event['created_at']+20000)
        self.assertIn('CREATED codex',result)
        self.assertEqual(load_json(self.root/'.agent-watchdog/launch-history.json')[event['event_id']]['target'],'codex')

    def test_same_source_reset_switches_managed_window_without_duplicate(self):
        file=make_handoff(self.root)
        old=mark_ready(self.root,'claude','old-1',file)
        running=False
        cmds=[]
        def fake_run(argv,**kwargs):
            nonlocal running
            cmds.append(argv)
            cmd=argv[1]
            if cmd=='has-session':
                return SimpleNamespace(returncode=0 if running else 1,stdout='',stderr='')
            if cmd=='new-session':
                running=True
                return SimpleNamespace(returncode=0,stdout='@1',stderr='')
            if cmd=='new-window':
                return SimpleNamespace(returncode=0,stdout='@2',stderr='')
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            self.assertIn('CREATED codex',maybe_launch(self.root,self.config,
                {'claude':quota('claude',95,'handoff'),'codex':quota('codex',10)},now=old['created_at']+11))
            file.write_text(file.read_text()+' next handoff')
            event=mark_ready(self.root,'codex','cdx-1',file)
            self.assertIn('WAITING',maybe_launch(self.root,self.config,
                {'claude':quota('claude',98,'handoff'),'codex':quota('codex',92,'handoff')},now=event['created_at']+20))
            self.assertIn('CREATED codex',maybe_launch(self.root,self.config,
                {'claude':quota('claude',98,'handoff'),'codex':quota('codex',0,'normal')},now=event['created_at']+72000))
        self.assertEqual(sum(c[1]=='new-session' for c in cmds),1)
        self.assertEqual(sum(c[1]=='new-window' for c in cmds),1)
        self.assertIn('kill-window',[c[1] for c in cmds])
        self.assertNotIn('kill-session',[c[1] for c in cmds])
        self.assertEqual(load_json(self.root/'.agent-watchdog/worker.json')['agent'],'codex')

    def test_both_available_prefers_other_provider(self):
        file=make_handoff(self.root)
        event=mark_ready(self.root,'codex','c1',file)
        def fake_run(argv,**kwargs):
            return SimpleNamespace(returncode=1 if argv[1]=='has-session' else 0,
                                   stdout='@1' if argv[1]=='new-session' else '',stderr='')
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            result=maybe_launch(self.root,self.config,{
                'codex':quota('codex',0,'normal'), 'claude':quota('claude',0,'normal')},
                now=event['created_at']+11)
        self.assertIn('CREATED claude',result)

    def test_stale_or_unknown_quota_does_not_resume(self):
        file=make_handoff(self.root)
        event=mark_ready(self.root,'codex','c1',file)
        with patch('runner.subprocess.run') as run:
            result=maybe_launch(self.root,self.config,{
                'codex':{**quota('codex',0,'normal'),'stale':True},
                'claude':{'agent':'claude','stage':'unknown'}},now=event['created_at']+11)
            self.assertIn('WAITING',result)
            run.assert_not_called()

    def test_no_launch_without_verified_handoff_or_quota(self):
        event={'event_id':'abc','target':'codex'}
        self.assertIn('placeholder',launch(self.root,self.config,event,quota('codex')))
        make_handoff(self.root)
        self.assertIn('insufficient',launch(self.root,self.config,event,quota('codex',94,'handoff')))
        self.assertIn('insufficient',launch(self.root,self.config,event,{**quota('codex'), 'stale':True}))
        self.assertIn('insufficient',launch(self.root,self.config,event,quota('codex',81,'prepare')))

    def test_explicit_recovery_from_previous_claude_handoff(self):
        make_handoff(self.root)
        commands=[]
        def fake_run(argv,**kwargs):
            commands.append(argv)
            return SimpleNamespace(returncode=1 if argv[1]=='has-session' else 0,stderr='',stdout='@1' if argv[1]=='new-session' else '')
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            self.assertIn('CREATED codex',explicit_codex(self.root,self.config,quota('codex')))
            self.assertIn('already been handled',explicit_codex(self.root,self.config,quota('codex')))
            self.assertEqual(sum(cmd[1]=='new-session' for cmd in commands),1)

    def test_reverse_auto_launch_switches_watchdog_owned_session(self):
        path=make_handoff(self.root)
        event=mark_ready(self.root,'claude','c1',path)
        running=False
        commands=[]
        def fake_run(argv,**kwargs):
            nonlocal running
            commands.append(argv)
            if argv[1]=='has-session':return SimpleNamespace(returncode=0 if running else 1,stderr='',stdout='')
            if argv[1]=='new-session':running=True;return SimpleNamespace(returncode=0,stderr='',stdout='@1')
            if argv[1]=='new-window':return SimpleNamespace(returncode=0,stderr='',stdout='@2')
            if argv[1]=='display-message':return SimpleNamespace(returncode=0,stderr='',stdout='@1')
            return SimpleNamespace(returncode=0,stderr='',stdout='')
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            self.assertIn('CREATED codex',maybe_launch(self.root,self.config,{'codex':quota('codex')},now=event['created_at']+11))
            path.write_text(path.read_text()+' another completed handoff')
            event2=mark_ready(self.root,'codex','cdx-1',path)
            self.assertIn('CREATED claude',maybe_launch(self.root,self.config,{'claude':quota('claude',20)},now=event2['created_at']+11))
        self.assertIn('new-window',[c[1] for c in commands])
        self.assertIn('select-window',[c[1] for c in commands])
        self.assertIn('kill-window',[c[1] for c in commands])
        self.assertNotIn('kill-session',[c[1] for c in commands])
        self.assertEqual(load_json(self.root/'.agent-watchdog/worker.json')['agent'],'claude')

    def test_never_kill_unowned_tmux_session(self):
        make_handoff(self.root)
        with patch('runner.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='',stderr='')) as run,patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            response=explicit_codex(self.root,self.config,quota('codex'))
            self.assertIn('already exists',response)
            self.assertFalse(any(args.args[0][1] in ('kill-session','kill-window') for args in run.call_args_list))

    def test_interactive_scripts_use_requested_flags_and_no_headless_mode(self):
        text=(TOOL/'interactive-agent.sh').read_text()
        self.assertIn('codex remote-control start',text)
        self.assertIn('model_picker.py" print --agent',text)
        self.assertIn('exec codex --remote unix:// --cd "$PWD" --model "$model" -c',text)
        self.assertIn('model_reasoning_effort=',text)
        self.assertIn('if [[ "$unrestricted" == yes ]]',text)
        self.assertIn('exec codex --remote unix:// --cd "$PWD" --model "$model" --yolo',text)
        self.assertIn('exec claude --model "$model" --dangerously-skip-permissions --rc',text)
        self.assertNotIn('--rc "$prompt"',text)
        self.assertIn('claude_startup.py',text)
        self.assertNotIn('codex exec ',text)
        self.assertNotIn('claude -p ',text)


    def test_fast_exit_keeps_terminal_and_atomic_window_id(self):
        make_handoff(self.root)
        commands=[]
        def fake_run(argv,**kwargs):
            commands.append(argv)
            if argv[1]=='has-session':return SimpleNamespace(returncode=1,stderr='',stdout='')
            if argv[1]=='new-session':return SimpleNamespace(returncode=0,stderr='',stdout='@9\n')
            raise AssertionError('Unexpected second call that depends on a still-living tmux session: '+str(argv))
        with patch('runner.subprocess.run',side_effect=fake_run),patch('runner.shutil.which',return_value='/usr/bin/tmux'):
            result=explicit_codex(self.root,self.config,quota('codex'))
        self.assertIn('CREATED codex',result)
        cmd=next(c for c in commands if c[1]=='new-session')
        self.assertEqual(cmd[1:6],['new-session','-d','-P','-F','#{window_id}'])
        self.assertIn('exec bash -i',cmd[-1])
        self.assertIn('Agent exited',cmd[-1])
        self.assertIn('bash',cmd)
        self.assertIn('-lic',cmd)

    def test_window_opening_is_enabled_by_default_and_upgrade_preserves_selection(self):
        self.assertTrue(self.config['auto_open_terminal_window'])
        self.assertEqual(self.config['wsl_distro'],'Debian')
        path=self.root/'.agent-watchdog/config.json'
        data=load_json(path)
        data['auto_open_terminal_window']=False
        path.write_text(json.dumps(data))
        install(self.root)
        self.assertFalse(load_json(path)['auto_open_terminal_window'])
        self.assertTrue((self.root/'.agent-watchdog/open-agent-window.ps1').is_file())

    def test_show_opens_windows_terminal_for_existing_agent_only(self):
        make_handoff(self.root)
        script=self.root/'.agent-watchdog/open-agent-window.ps1'
        self.assertTrue(script.exists())
        commands=[]
        def fake_run(argv,**kwargs):
            commands.append(argv)
            if argv[0]=='tmux':
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            if argv[0]=='wslpath':
                return SimpleNamespace(returncode=0,stdout=r'C:\temp\open-agent-window.ps1'+"\n",stderr='')
            if argv[0]=='powershell.exe':
                return SimpleNamespace(returncode=0,stdout='Opened Windows Terminal',stderr='')
            raise AssertionError(argv)
        with patch('runner.wsl_host',return_value=True):
            result=show_agent_window(self.root,self.config,'codex',force=True,invoke=fake_run)
        self.assertIn('Windows Terminal opened',result)
        self.assertEqual(commands[0][1],'has-session')
        self.assertEqual(commands[1][0],'wslpath')
        self.assertEqual(commands[2][0],'powershell.exe')
        self.assertIn('-Session',commands[2])
        self.assertIn(session_name(self.root),commands[2])
        self.assertIn('-Agent',commands[2])
        self.assertIn('codex',commands[2])
        self.assertFalse(any('new-session' in cmd for cmd in commands))

    def test_auto_show_does_not_open_duplicate_window_if_attached(self):
        commands=[]
        def fake_run(argv,**kwargs):
            commands.append(argv)
            if argv[1]=='has-session':return SimpleNamespace(returncode=0,stdout='',stderr='')
            if argv[1]=='list-clients':return SimpleNamespace(returncode=0,stdout='/dev/pts/1\n',stderr='')
            raise AssertionError('Should not start an additional Windows Terminal')
        with patch('runner.wsl_host',return_value=True):
            result=show_agent_window(self.root,self.config,'claude',invoke=fake_run)
        self.assertIn('already attached',result)
        self.assertEqual(len(commands),2)

    def test_auto_show_opens_when_no_tmux_clients(self):
        commands=[]
        def fake_run(argv,**kwargs):
            commands.append(argv)
            if argv[0]=='tmux':
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            if argv[0]=='wslpath':
                return SimpleNamespace(returncode=0,stdout=r'C:\temp\open-agent-window.ps1'+"\n",stderr='')
            if argv[0]=='powershell.exe':
                return SimpleNamespace(returncode=0,stdout='Opened',stderr='')
            raise AssertionError(argv)
        with patch('runner.wsl_host',return_value=True):
            result=show_agent_window(self.root,self.config,'claude',invoke=fake_run)
        self.assertIn('Windows Terminal opened',result)
        self.assertEqual([c[0] for c in commands],['tmux','tmux','wslpath','powershell.exe'])

    def test_no_wsl_interop_fails_safely_without_launching_second_agent(self):
        cmds=[]
        def fake_run(argv,**kwargs):
            cmds.append(argv)
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch('runner.wsl_host',return_value=False):
            result=show_agent_window(self.root,self.config,'codex',force=True,invoke=fake_run)
        self.assertIn('terminal opened for codex',result)
        self.assertEqual([c[0] for c in cmds],['tmux','bash'])

    def test_config_merge_preserves_other_settings(self):
        cfg=self.root/'.codex/config.toml'
        cfg.write_text('# user comment\nmodel = "old"\n[features]\nfast_mode = true\n')
        claude=self.root/'.claude/settings.local.json'
        val=load_json(claude);val['customField']='preserved';claude.write_text(json.dumps(val))
        install(self.root)
        out=cfg.read_text()
        self.assertIn('# user comment',out)
        self.assertIn('fast_mode = true',out)
        self.assertEqual(out.count('model = "old"'),1)
        self.assertEqual(load_json(claude)['customField'],'preserved')

if __name__=='__main__':unittest.main()
