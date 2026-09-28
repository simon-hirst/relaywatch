"""Regression tests for lost worker.json with a still-running managed tmux CLI."""
import sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from engine import load_json, atomic_write_json
from runner import session_name
from session_recovery import detect_managed_agent, recover_worker

class ReattachTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        (self.root/'.agent-watchdog').mkdir()

    def fake_run(self, cmd, **kwargs):
        if cmd[1]=='has-session':
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        if cmd[1]=='list-panes':
            return SimpleNamespace(returncode=0,
                stdout='@7\tcodex\t123\t0\n',stderr='')
        raise AssertionError('read-only recovery must not run '+str(cmd))

    def test_recover_lost_worker_when_unique_real_codex(self):
        evidence,reason=detect_managed_agent(self.root,run=self.fake_run,
            walk=lambda pid:[pid,789],identify=lambda pid,root:'codex' if pid==789 else None)
        self.assertIsNone(reason)
        self.assertEqual(evidence['window'],'@7')
        self.assertEqual(evidence['session'],session_name(self.root))
        answer=recover_worker(self.root,run=self.fake_run,
            walk=lambda pid:[pid,789],identify=lambda pid,root:'codex' if pid==789 else None)
        self.assertIn('RECOVERED codex',answer)
        worker=load_json(self.root/'.agent-watchdog/worker.json')
        self.assertTrue(worker['recovered'])
        self.assertEqual(worker['agent'],'codex')
        self.assertEqual(worker['window'],'@7')
        # Never overwrites the record once restored.
        self.assertIsNone(recover_worker(self.root,run=self.fake_run))

    def test_no_live_cli_never_adopts_terminal_title(self):
        answer=recover_worker(self.root,run=self.fake_run,
            walk=lambda pid:[pid],identify=lambda pid,root:None)
        self.assertIn('RECOVERY NOT APPLIED',answer)
        self.assertFalse((self.root/'.agent-watchdog/worker.json').exists())

    def test_more_than_one_cli_must_not_guess(self):
        def run(cmd,**kwargs):
            if cmd[1]=='has-session':return SimpleNamespace(returncode=0,stdout='',stderr='')
            if cmd[1]=='list-panes':return SimpleNamespace(returncode=0,stdout='@1\tcodex\t100\t0\n@2\tclaude\t200\t0\n',stderr='')
            raise AssertionError(cmd)
        answer=recover_worker(self.root,run=run,
            walk=lambda pid:[pid],identify=lambda pid,root:'codex' if pid==100 else 'claude')
        self.assertIn('found 2 verified',answer)
        self.assertFalse((self.root/'.agent-watchdog/worker.json').exists())

    def test_cannot_replace_known_record_even_if_disagrees(self):
        prior={'type':'tmux','agent':'claude','session':session_name(self.root),'window':'@4'}
        atomic_write_json(self.root/'.agent-watchdog/worker.json',prior)
        self.assertIsNone(recover_worker(self.root,run=self.fake_run))
        self.assertEqual(load_json(self.root/'.agent-watchdog/worker.json'),prior)

    def test_real_cli_detection_requires_correct_cwd_and_args(self):
        # Test actual /proc parsing using mocks of process metadata.
        from session_recovery import _interactive_identity
        orig=Path.read_bytes
        def bytes_(path):
            if str(path).endswith('cmdline'):
                return b'/usr/bin/codex\0--remote\0unix://\0--cd\0/elsewhere\0'
            return orig(path)
        with patch('session_recovery.Path.read_bytes',bytes_), patch('session_recovery.os.readlink',return_value=str(self.root)):
            self.assertEqual(_interactive_identity(123,self.root.resolve()),'codex')
        with patch('session_recovery.Path.read_bytes',bytes_), patch('session_recovery.os.readlink',return_value='/not-the-project'):
            self.assertIsNone(_interactive_identity(123,self.root.resolve()))

if __name__=='__main__':unittest.main()

class OrphanProcessClassificationTests(unittest.TestCase):
    def test_helpers_are_not_editing_agents(self):
        from session_recovery import _active_cli_kind
        self.assertIsNone(_active_cli_kind(['/home/example/.../bin/codex-code-mode-host']))
        self.assertIsNone(_active_cli_kind(['/usr/bin/codex','app-server','--remote-control']))
        self.assertIsNone(_active_cli_kind(['/usr/bin/codex','remote-control','start']))
        self.assertIsNone(_active_cli_kind(['claude','bg-pty-host','--bg-pty-host','/tmp/cc-daemon-1000/test.sock']))
        self.assertIsNone(_active_cli_kind(['node','/home/example/.nvm/lib/node_modules/@anthropic-ai/claude-code/cli.js','bg-pty-host']))
        self.assertIsNone(_active_cli_kind(['node','/path/to/codex-code-mode-host']))
        self.assertIsNone(_active_cli_kind(['node','/repo/scripts/claude-status.js']))
    def test_genuine_clis_still_block_recovery(self):
        from session_recovery import _active_cli_kind
        self.assertEqual(_active_cli_kind(['/usr/bin/claude','--rc','--dangerously-skip-permissions']),'claude')
        self.assertEqual(_active_cli_kind(['/usr/bin/codex','--remote','unix://','--yolo']),'codex')
        self.assertEqual(_active_cli_kind(['node','/home/example/.nvm/bin/codex','--remote','unix://']),'codex')
        self.assertEqual(_active_cli_kind(['node','/home/example/.nvm/lib/node_modules/@anthropic-ai/claude-code/cli.js']),'claude')
    def test_procs_in_actual_checkout_only(self):
        from session_recovery import active_repository_agents
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'repo'; root.mkdir()
            proc=Path(tmp)/'proc'; proc.mkdir()
            specimens={
                '1084': ('/opt/codex-code-mode-host',root),
                '9989': ('claude bg-pty-host --bg-pty-host /tmp/cc-daemon/pty/sock',root),
                '7777': ('/opt/claude --rc',root),
                '111': ('/opt/codex app-server',root),
                '222': ('/opt/codex --yolo',Path(tmp)),
            }
            for pid,(argline,cwd) in specimens.items():
                entry=proc/pid;entry.mkdir(); (entry/'cwd').touch(); (entry/'cmdline').write_bytes(b'\x00'.join(x.encode() for x in argline.split(' '))+b'\x00')
            orig=__import__('os').readlink
            def fake_readlink(path):
                if str(path).startswith(str(proc)):
                    return str(specimens[Path(path).parent.name][1])
                return orig(path)
            with patch('session_recovery.os.readlink',side_effect=fake_readlink):
                found=active_repository_agents(root,proc_root=proc)
            self.assertEqual(found,['claude PID 7777'])
