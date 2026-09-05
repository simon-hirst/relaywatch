import importlib.util,json,os,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch

TOOL=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(TOOL))
from usage import metrics_from_json,window_name,summarise_metrics,get_api_usage
from engine import stage,update,load_json,handoff_current
from install import install
from hook import hook

class ParserTests(unittest.TestCase):
    def test_nested_explicit_percents(self):
        samples={'usage':{'current_session':{'used_percent':82},'weekly':{'remaining_pct':7},'resets_at':100}}
        metrics=metrics_from_json(samples)
        self.assertEqual(max(m.used_percent for m in metrics),93)
    def test_fractional_utilization_and_nested_derived(self):
        metrics=metrics_from_json({'five_hour':{'utilization':0.95},'weekly':{'used':9,'limit':10}})
        self.assertEqual(max(m.used_percent for m in metrics),95)
    def test_reject_bare_numbers(self):
        self.assertEqual(metrics_from_json({'usage':{'session':{'used':95},'weekly':{'remaining':6}},'timeout':90}),[])
    def test_explicit_config(self):
        metric=metrics_from_json({'foo':{'value':88}},'$.foo.value')
        self.assertEqual(metric[0].used_percent,88)
    def test_expose_both_quota_windows(self):
        from usage import summarise_metrics
        samples={'five_hour':{'used_percent':77},'weekly':{'used_percent':24}}
        windows=summarise_metrics(metrics_from_json(samples))
        self.assertEqual(windows['session']['used_percent'],77)
        self.assertEqual(windows['session']['remaining_percent'],23)
        self.assertEqual(windows['weekly']['used_percent'],24)
        self.assertEqual(windows['weekly']['remaining_percent'],76)
        samples={'five_hour':{'remaining_percent':100},'weekly':{'remaining_percent':51}}
        windows=summarise_metrics(metrics_from_json(samples))
        self.assertEqual(windows['session']['used_percent'],0)
        self.assertEqual(windows['weekly']['used_percent'],49)
    def test_recognize_window_names(self):
        self.assertEqual(window_name('$.five_hour.used_percent'),'session')
        self.assertEqual(window_name('$.weekly.used_percent'),'weekly')
        self.assertEqual(window_name('$.usage.current_session.used_percent'),'session')
        self.assertEqual(window_name('$.usage.seven_day.utilization'),'weekly')
        self.assertEqual(window_name('$.used_pct'),'other')
    def test_stages(self):
        self.assertEqual(stage(20),'prepare')
        self.assertEqual(stage(10),'handoff')
        self.assertEqual(stage(20.01),'normal')

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        (self.root/'docs').mkdir()
        (self.root/'docs'/'NEXT-CLAUDE-FOUNDERS-POLISH-2026-10-25.md').write_text('Existing project-specific requirements')
        (self.root/'CLAUDE.md').write_text('Original Claude instructions\n')
        (self.root/'AGENTS.md').write_text('Original Codex instructions\n')
        (self.root/'.claude').mkdir()
        (self.root/'.claude'/'settings.local.json').write_text(json.dumps({'permissions':{'allow':['Bash(ls)']},'hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo untouched'}]}]}}))
    def tearDown(self):self.tmp.cleanup()
    def test_install_repeat_preserves_existing(self):
        install(self.root);install(self.root)
        self.assertEqual((self.root/'CLAUDE.md').read_text().count('BEGIN AGENT-HANDOFF-WATCHDOG'),1)
        self.assertIn('Original Claude instructions',(self.root/'CLAUDE.md').read_text())
        self.assertIn('Original Codex instructions',(self.root/'AGENTS.md').read_text())
        data=json.loads((self.root/'.claude'/'settings.local.json').read_text())
        self.assertIn('Bash(ls)',data['permissions']['allow'])
        self.assertEqual(len(data['hooks']['Stop']),2)
        self.assertIn('echo untouched',json.dumps(data))
        self.assertIn('NEXT-CLAUDE-FOUNDERS', (self.root/'docs'/'HANDOFF.md').read_text())
        self.assertTrue((self.root/'docs'/'NEXT-CLAUDE-FOUNDERS-POLISH-2026-10-25.md').is_file())
    def test_hook_threshold_outputs(self):
        install(self.root)
        cfg=load_json(self.root/'.agent-watchdog'/'config.json')
        with patch('engine.get_api_usage',return_value={'used_percent':91,'remaining_percent':9,'limiting_metric':'$.session','metrics':[]}):
            first=update(self.root,'claude',cfg)
            result=hook(self.root,'claude','PostToolUse','session-one',{})
            self.assertIn('URGENT',result['hookSpecificOutput']['additionalContext'])
            # Stop hook must request missing handoff (installer placeholder predates quota event)
            stop=hook(self.root,'claude','Stop','session-one',{'stop_hook_active':False})
            self.assertEqual(stop['decision'],'block')
            self.assertEqual(hook(self.root,'claude','Stop','session-one',{'stop_hook_active':True}),{})
            (self.root/'docs'/'HANDOFF.md').write_text('Verified handoff with tests, changes, risks, details. '*12)
            reports=self.root/'docs'/'session-reports'
            reports.mkdir(exist_ok=True)
            (reports/'2026-10-26T12-00-00Z-claude.md').write_text('Completed: feature. Verification: passed tests. Next: next task. '*5)
            self.assertEqual(hook(self.root,'claude','Stop','session-one',{}),{})


class EndpointIntegrationTests(unittest.TestCase):
    def test_live_endpoint_and_hook_subprocess(self):
        from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data=json.dumps({'usage':{'session':{'percent_used':93},'week':{'used_percent':35}}}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        from threading import Thread
        t=Thread(target=server.serve_forever,daemon=True);t.start()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);install(root)
            path=root/'.agent-watchdog'/'config.json'
            cfg=load_json(path)
            cfg['agents']['claude']['url']=f'http://127.0.0.1:{server.server_port}/get'
            cfg['agents']['codex']['url']=cfg['agents']['claude']['url']
            path.write_text(json.dumps(cfg))
            cmd=[sys.executable,str(root/'.agent-watchdog'/'watchdog.py'),'once']
            cp=subprocess.run(cmd,capture_output=True,text=True,timeout=10)
            self.assertEqual(cp.returncode,0,cp.stderr)
            self.assertIn('93.0% used',cp.stdout)
            self.assertIn('session',cp.stdout)
            self.assertIn('weekly',cp.stdout)
            self.assertIn('35.0% used',cp.stdout)
            h=subprocess.run([sys.executable,str(root/'.agent-watchdog'/'hook.py'), '--agent','claude','--project',str(root)],
                             input=json.dumps({'hook_event_name':'PostToolUse','session_id':'abc123'}),capture_output=True,text=True,timeout=10)
            self.assertEqual(h.returncode,0,h.stderr)
            self.assertIn('URGENT',json.loads(h.stdout)['hookSpecificOutput']['additionalContext'])
        server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
