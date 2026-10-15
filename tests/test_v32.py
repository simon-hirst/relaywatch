"""v3.2 regression tests: nonduplicating footer and immediate next-launch presets."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from console_ui import ConsoleUI
import model_picker


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


class FooterTests(unittest.TestCase):
    def test_tty_footer_is_replaced_in_place_not_reprinted_as_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = ConsoleUI(tmp)
            ui.available = True
            ui.ansi_output = True
            output = TerminalStream()
            with patch('sys.stdout', output), patch('console_ui.shutil.get_terminal_size', return_value=os.terminal_size((120, 30))):
                ui.show_footer()
                ui.write_messages(['Claude is normal'], timestamp='[T] ')
                ui.write_messages(['Codex is normal'], timestamp='[T] ')
                value = output.getvalue()
                self.assertIn('\r\x1b[2K[T] Claude is normal\n', value)
                self.assertIn('\r\x1b[2K[T] Codex is normal\n', value)
                # Every prior footer is immediately cleared BEFORE a log line.
                self.assertEqual(value.count('━━'), 3 * 2)  # one opening/closing per redraw
                self.assertEqual(value.count('\r\x1b[2K'), 5)  # 3 footer draws + 2 erasures
                self.assertTrue(value.endswith(ui.footer()))
                self.assertNotIn('━━━━━━━━\n[T]', value)
                ui.clear_footer()
                self.assertFalse(ui.footer_visible)
                self.assertTrue(output.getvalue().endswith('\r\x1b[2K'))

    def test_plain_output_prints_footer_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=ConsoleUI(tmp)
            ui.available=False
            ui.ansi_output=False
            output=io.StringIO()
            with patch('sys.stdout',output):
                ui.write_messages(['one'],timestamp='[T] ')
                ui.write_messages(['two'],timestamp='[T] ')
            self.assertEqual(output.getvalue().count('change models:'),1)
            self.assertIn('[T] one',output.getvalue())
            self.assertIn('[T] two',output.getvalue())
            self.assertNotIn('\x1b[2K',output.getvalue())

    def test_footer_reflects_external_live_choice_without_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=ConsoleUI(tmp)
            ui.available=True
            choices_file=Path(tmp)/'models.json'
            with patch.object(model_picker,'DATA',choices_file):
                self.assertIn('gpt-6.1-sol/xhigh',ui.footer())
                model_picker.save({'claude':{'model':'claude-sonnet-5-5','effort':'low'},
                                   'codex':{'model':'gpt-6-astra','effort':'high'}})
                self.assertIn('claude-sonnet-5-5/low',ui.footer())
                self.assertIn('gpt-6-astra/high',ui.footer())


class LaunchPresetTests(unittest.TestCase):
    def test_next_shell_launch_picks_modified_presets_without_watchdog_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp)
            wd=project/'.agent-watchdog';wd.mkdir()
            shutil.copy2(ROOT/'model_picker.py',wd/'model_picker.py')
            shutil.copy2(ROOT/'interactive-agent.sh',wd/'interactive-agent.sh')
            prompt=wd/'next-agent-prompt.txt';prompt.write_text('Continue with next task')
            bindir=project/'bin';bindir.mkdir()
            log=project/'models-seen.jsonl'
            fake=bindir/'claude'
            fake.write_text("#!/usr/bin/env python3\nimport json, os, sys\nwith open(os.environ['TEST_MODEL_LOG'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n")
            fake.chmod(0o755)
            env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'TEST_MODEL_LOG':str(log)}
            settings=wd/'models.json'
            for model, effort in [('claude-opus-5-5','xhigh'),('claude-sonnet-5-5','medium')]:
                settings.write_text(json.dumps({'claude':{'model':model,'effort':effort}}))
                result=subprocess.run(['bash',str(wd/'interactive-agent.sh'),'claude',str(project),str(prompt)],
                                       env=env,capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
            calls=[json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(calls[0][calls[0].index('--model')+1],'claude-opus-5-5')
            self.assertEqual(calls[1][calls[1].index('--model')+1],'claude-sonnet-5-5')
            self.assertEqual(calls[1][calls[1].index('--effort')+1],'medium')
            self.assertIn('--rc',calls[1])
            self.assertNotIn('--dangerously-skip-permissions',calls[1])


if __name__=='__main__': unittest.main()
