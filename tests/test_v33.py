"""v3.3 dashboard: compact status and durable events without functional changes."""
import io,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from console_ui import DashboardUI


INPUT=[
 'claude limiting session: 97.0% used (3.0% left) — handoff complete → codex',
 '         session 97.0% used, 3.0% left (metric $.five_hour.used_percent)',
 '         weekly  39.0% used, 61.0% left (metric $.weekly.used_percent)',
 'codex  limiting session: 97.0% used (3.0% left) — handoff complete (awaiting next agent)',
 '         session 97.0% used, 3.0% left (metric $.five_hour.used_percent)',
 '         weekly  30.0% used, 70.0% left (metric $.weekly.used_percent)',
]


class DashboardTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.ui=DashboardUI(self.tmp.name)

 def test_compact_no_poll_spam_in_plain_output(self):
  stream=io.StringIO()
  with patch('sys.stdout',stream):
   self.ui.write_messages(INPUT,timestamp='12:40')
   self.ui.write_messages(INPUT,timestamp='12:41')
  output=stream.getvalue()
  self.assertEqual(output.count('NEXT'),1)
  self.assertIn('CLAUDE',output)
  self.assertIn('CODEX',output)
  self.assertIn('Both quotas low',output)
  self.assertEqual(output.count('Shift+M'),1)
  self.assertNotIn('\x1b',output)

 def test_tty_dashboard_rewrites_lines_in_place_and_keeps_footer(self):
  self.ui.ansi_output=True
  self.ui.available=True
  stream=io.StringIO()
  with patch('sys.stdout',stream),patch('console_ui.shutil.get_terminal_size',return_value=os.terminal_size((100,32))):
   self.ui.write_messages(INPUT,timestamp='12:40')
   count=self.ui.live_lines
   self.assertGreater(count,6)
   self.ui.write_messages(INPUT,timestamp='12:41')
   self.assertEqual(self.ui.live_lines,count)
   self.ui.clear_footer()
  data=stream.getvalue()
  self.assertIn(f'\x1b[{count}A\r\x1b[J',data)
  self.assertIn('\x1b[91m',data)
  self.assertIn('Shift+M',data)
  self.assertIn('97.0% used',data)

 def test_only_meaningful_events_remain_in_scrollback(self):
  self.ui.ansi_output=True
  out=io.StringIO()
  msgs=INPUT+['AUTO-HANDOFF CREATED claude interactive tmux window @1']
  with patch('sys.stdout',out),patch('console_ui.shutil.get_terminal_size',return_value=os.terminal_size((100,32))):
   self.ui.write_messages(msgs,timestamp='12:40')
   self.ui.write_messages(msgs,timestamp='12:41')
  self.assertEqual(out.getvalue().count('EVENT   AUTO-HANDOFF'),1)

 def test_alert_remains_visible_even_if_api_failed(self):
  msgs=['claude  TELEMETRY UNAVAILABLE (63s): HTTPError: 429; action=checkpoint; no quota guessed']+INPUT[3:]
  out=io.StringIO()
  with patch('sys.stdout',out),patch('console_ui.shutil.get_terminal_size',return_value=os.terminal_size((100,32))):
   self.ui.ansi_output=True
   self.ui.write_messages(msgs,timestamp='12:40')
  text=out.getvalue()
  self.assertIn('telemetry unavailable',text)
  self.assertIn('Quota API issue',text)
  self.assertIn('429',text)

 def test_narrow_terminal_never_wraps(self):
  self.ui._extract(INPUT)
  for columns in (35,60,75,100,130):
   lines=self.ui._lines(columns)
   self.assertTrue(all(len(s)<columns for s,_ in lines),columns)

 def test_normal_state_is_not_reported_as_active_agent(self):
  msgs=['claude limiting weekly: 29.0% used (71.0% left) — normal',
        '         session 18.0% used, 82.0% left (metric $.five_hour.used_percent)',
        '         weekly  29.0% used, 71.0% left (metric $.weekly.used_percent)']
  self.ui._extract(msgs)
  self.assertEqual(self.ui.stats['claude']['state'],'normal')
  self.assertEqual(self.ui.stats['claude']['windows']['weekly']['left'],71)
  self.assertNotIn('working',self.ui._pending()[0].lower())


if __name__=='__main__':unittest.main()
