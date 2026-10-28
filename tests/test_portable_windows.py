"""Native Windows coverage for portable pieces; managed CLI supervision runs in WSL2."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from usage import metrics_from_json, window_name
from reset_times import parse_reset_timestamp, resets_from_json
from cli_paths import find_cli


class NativeWindowsSmokeTests(unittest.TestCase):
    def test_quota_parser_is_platform_neutral(self):
        records = metrics_from_json({
            'five_hour': {'used_percent': 82.0},
            'weekly': {'used_percent': 19.0},
        })
        self.assertTrue(any(window_name(item.path) == 'session' and item.used_percent == 82.0 for item in records))
        self.assertTrue(any(window_name(item.path) == 'weekly' and item.used_percent == 19.0 for item in records))

    def test_reset_timestamp_uses_actual_provider_time(self):
        now = 1793000000.0
        self.assertEqual(parse_reset_timestamp(now + 3600, now=now), now + 3600)
        self.assertIsNone(parse_reset_timestamp('2026-10-27 16:30:00', now=now))

    def test_provider_parser_never_estimates_reset_time(self):
        self.assertEqual(resets_from_json({'five_hour': {'used_percent': 90}},
                         {'session': {'metric': '$.five_hour.used_percent'}}, now=1793000000), {})

    def test_missing_cli_is_safe(self):
        with self.assertRaises(ValueError):
            find_cli('../malicious')

    def test_windows_entrypoints_and_wsl_docs(self):
        self.assertTrue((ROOT / 'start-windows.ps1').is_file())
        self.assertTrue((ROOT / 'install-windows.ps1').is_file())
        self.assertIn('WSL2', (ROOT / 'README.md').read_text(encoding='utf-8'))

    def test_reconstructed_history_not_misrepresented(self):
        self.assertTrue((ROOT / 'HISTORY.md').is_file())
        self.assertIn('reconstruct', (ROOT / 'HISTORY.md').read_text(encoding='utf-8').lower())


if __name__ == '__main__':
    unittest.main()
