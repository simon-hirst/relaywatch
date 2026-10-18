"""v3.4: provider reset metadata displayed in Windows-local clock time."""
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from console_ui import DashboardUI
from usage import get_api_usage, summarise_metrics, metrics_from_json
from reset_times import parse_reset_timestamp, resets_from_json, format_reset_local, local_timezone

T0 = datetime(2026, 10, 26, 14, 30, tzinfo=timezone.utc).timestamp()


class ResetParserTests(unittest.TestCase):
    def test_iso_epoch_and_millisecond_windows(self):
        obj = {'five_hour': {'used_percent': 81, 'resets_at': '2026-10-26T17:00:00Z'},
               'weekly': {'used_percent': 44, 'reset_at': 1793203200000}}
        found = resets_from_json(obj, summarise_metrics(metrics_from_json(obj)), now=T0)
        self.assertEqual(found['session'], datetime(2026, 10, 26, 17, tzinfo=timezone.utc).timestamp())
        self.assertEqual(found['weekly'], datetime(2026, 10, 28, 16, tzinfo=timezone.utc).timestamp())

    def test_nested_alias_matched_to_selected_metric_and_not_other_window(self):
        obj = {
            'five_hour': {'used_percent': 15, 'reset_at': '2026-10-26T15:00:00Z'},
            'weekly': {'used_percent': 5, 'reset_at': '2026-10-27T00:00:00Z'},
            'buckets': {'codex': {'five_hour': {'used_percent': 92, 'resetAt': '2026-10-26T18:00:00Z'},
                                  'weekly': {'used_percent': 28, 'nextReset': '2026-10-29T18:00:00Z'}}}
        }
        windows = summarise_metrics(metrics_from_json(obj))
        found = resets_from_json(obj, windows, now=T0)
        self.assertEqual(found['session'], datetime(2026, 10, 26, 18, tzinfo=timezone.utc).timestamp())
        self.assertEqual(found['weekly'], datetime(2026, 10, 29, 18, tzinfo=timezone.utc).timestamp())

    def test_reset_under_seven_day_path_and_relative_countdown(self):
        obj={'usage': {'five_hour': {'used_percent': 72, 'reset_in_seconds': 3600},
                       'seven_day': {'used_percent': 30, 'reset_time': '2026-10-29T11:30:00+01:00'}}}
        windows=summarise_metrics(metrics_from_json(obj))
        resets=resets_from_json(obj,windows,now=T0)
        self.assertEqual(resets['session'],T0+3600)
        self.assertEqual(resets['weekly'], datetime(2026,10,29,10,30,tzinfo=timezone.utc).timestamp())

    def test_unknown_naive_or_unrelated_does_not_guess(self):
        obj={'five_hour': {'used_percent': 97, 'reset_at': '2026-10-26T17:00:00'},
             'weekly': {'used_percent': 40}, 'updated': '2026-10-26T15:00:00Z',
             'reset_at': '2026-10-26T17:15:00Z'}
        found=resets_from_json(obj,summarise_metrics(metrics_from_json(obj)),now=T0)
        self.assertEqual(found,{})
        self.assertIsNone(parse_reset_timestamp(True,now=T0))
        self.assertIsNone(parse_reset_timestamp(97,now=T0))
        self.assertIsNone(parse_reset_timestamp('tomorrow',now=T0))

    def test_api_contract_preserves_percentages_and_includes_optional_resets(self):
        import unittest.mock as mock
        obj = {'five_hour': {'used_percent': 97, 'resets_at': '2026-10-26T17:00:00Z'},
               'weekly': {'used_percent': 25}}
        class Response:
            headers={}
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def read(self,n): return json.dumps(obj).encode('utf8')
        with mock.patch('reset_times.time.time',return_value=T0), mock.patch('usage.urllib.request.urlopen',return_value=Response()):
            result=get_api_usage('http://localhost:3790/get')
        self.assertEqual(result['remaining_percent'],3)
        self.assertIn('reset_ts',result['windows']['session'])
        self.assertNotIn('reset_ts',result['windows']['weekly'])


class ResetDisplayTests(unittest.TestCase):
    def test_gmt_and_bst_and_weekday_local_time(self):
        zone=ZoneInfo('Europe/London')
        utc=datetime(2026,10,26,15,0,tzinfo=timezone.utc).timestamp()
        self.assertEqual(format_reset_local(utc,zone,now=T0),'reset 15:00 GMT')
        summer=datetime(2027,6,1,15,0,tzinfo=timezone.utc).timestamp()
        summer_now=datetime(2027,6,1,14,0,tzinfo=timezone.utc).timestamp()
        self.assertEqual(format_reset_local(summer,zone,now=summer_now),'reset 16:00 BST')
        weekly=datetime(2026,10,29,15,0,tzinfo=timezone.utc).timestamp()
        self.assertIn('Thu 29 Oct 15:00 GMT',format_reset_local(weekly,zone,now=T0))

    def test_unknown_and_stale_are_explicit(self):
        zone=ZoneInfo('Europe/London')
        self.assertEqual(format_reset_local(None,zone,now=T0),'reset unknown')
        self.assertEqual(format_reset_local(T0-7200,zone,now=T0),'reset pending refresh')

    def test_dashboard_renews_reset_and_keeps_with_left_at_normal_widths(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui=DashboardUI(tmp)
            ui.display_zone=ZoneInfo('Europe/London')
            reset=datetime(2026,10,29,15,0,tzinfo=timezone.utc).timestamp()
            msgs=['claude limiting session: 95.0% used (5.0% left) — handoff',
                  f'         session 95.0% used, 5.0% left (metric $.five_hour.used_percent; reset_at={reset})',
                  '         weekly 35.0% used, 65.0% left (metric $.weekly.used_percent)',
                  'codex limiting weekly: 35.0% used (65.0% left) — normal']
            ui._extract(msgs)
            with patch('reset_times.time.time',return_value=T0):
                for width in (75,90,100,120):
                    rows=ui._lines(width)
                    quota_line=[line for line,_ in rows if 'CLAUDE' in line and '5H' in line][0]
                    self.assertIn('5.0% left',quota_line)
                    self.assertIn('Thu 29 Oct 15:00 GMT',quota_line)
                    self.assertLess(len(quota_line),width)
                self.assertTrue(any('reset unknown' in line for line,_ in ui._lines(100)))

    def test_auto_local_zone_from_windows_wsl_or_override(self):
        with patch('reset_times.os.environ', {'WSL_DISTRO_NAME':'Debian'}):
            with patch('reset_times.os.path.exists',return_value=False):
                with patch('reset_times.subprocess.run') as command:
                    command.return_value.returncode=0
                    command.return_value.stdout='GMT Standard Time\n'
                    local_timezone.cache_clear()
                    self.assertEqual(str(local_timezone('auto')),'Europe/London')
                    self.assertEqual(str(local_timezone('Europe/Paris')),'Europe/Paris')
                    self.assertEqual(command.call_count,1)
                    local_timezone.cache_clear()


if __name__=='__main__': unittest.main()
