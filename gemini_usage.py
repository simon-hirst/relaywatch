"""Verified Antigravity CLI quota reporting for Gemini 4 Argon.

Runs only the documented zero-turn /usage command on agy >=1.1.12.
Never uses undocumented auth tokens or conflates separate Argon quotas
with the general Gemini Models five-hour or weekly allowances.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import subprocess
import time
from pathlib import Path

from engine import atomic_write_json, load_json
from gemini_session import cli_path

MIN_AGY_VERSION = (1, 1, 12)
DEFAULT_INTERVAL_SECONDS = 120
FRESH_SECONDS = 300
GROUPS = ('Gemini 4 Argon', 'Gemini Models', 'Claude and GPT models')
WINDOWS = ('weekly', '5h')
VERSION = re.compile(r'(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)')


def path(root):
    return Path(root) / '.agent-watchdog' / 'gemini-usage.json'


def parse_version(output):
    match = VERSION.search(output if isinstance(output, str) else '')
    if not match:
        raise ValueError('Could not verify Antigravity CLI version')
    version = tuple(map(int, match.groups()))
    if version < MIN_AGY_VERSION:
        raise ValueError('Antigravity CLI must be >=1.1.12 for safe no-turn /usage; update agy')
    return '.'.join(match.groups())


def _fraction(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError('Quota remaining_fraction must be a number')
    if not 0 <= value <= 1:
        raise ValueError('Quota remaining_fraction must be between 0 and 1')
    return float(value)


def _reset(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Quota reset_time missing')
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Invalid quota reset_time') from exc
    if stamp.tzinfo is None:
        raise ValueError('Quota reset_time must include timezone')
    return stamp.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def parse(output):
    """Parse ONLY command.data.groups; no TSV response or inferred buckets.

    Particularly on Pro, Argon has a separate weekly allowance and no
    Argon five-hour bucket in the observed response. Other groups have
    five-hour quotas, but they must NOT be attributed to Argon.
    """
    raw = json.loads(output) if isinstance(output, str) else output
    if not isinstance(raw, dict) or raw.get('status') != 'SUCCESS':
        raise ValueError('Antigravity /usage did not report success')
    command = raw.get('command')
    if not isinstance(command, dict) or command.get('name') != 'usage':
        raise ValueError('Expected a verified /usage command response')
    if raw.get('num_turns') != 0:
        raise ValueError('Antigravity /usage unexpectedly started a model turn')
    usage = raw.get('usage')
    if not isinstance(usage, dict) or usage.get('total_tokens') != 0:
        raise ValueError('Antigravity /usage unexpectedly consumed or omitted token usage data')
    data = command.get('data')
    groups = data.get('groups') if isinstance(data, dict) else None
    if not isinstance(groups, list) or not groups:
        raise ValueError('Antigravity /usage returned no structured quota groups')
    parsed = {}
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError('Malformed quota group')
        name = group.get('name')
        if name not in GROUPS:
            continue
        if name in parsed:
            raise ValueError('Duplicate quota group')
        buckets = group.get('buckets')
        if not isinstance(buckets, list):
            raise ValueError('Quota group has no buckets list')
        values = {}
        for bucket in buckets:
            if not isinstance(bucket, dict):
                raise ValueError('Malformed quota bucket')
            window = bucket.get('window')
            if window not in WINDOWS:
                continue
            if window in values:
                raise ValueError('Duplicate quota window within group')
            ident = bucket.get('id')
            if not isinstance(ident, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', ident):
                raise ValueError('Malformed quota bucket ID')
            remaining = _fraction(bucket.get('remaining_fraction'))
            reset = _reset(bucket.get('reset_time'))
            values[window] = {
                'id': ident, 'window': window,
                'remaining_fraction': remaining,
                'remaining_percent': round(100 * remaining, 4),
                'used_percent': round(100 * (1 - remaining), 4),
                'reset_time': reset}
        parsed[name] = values
    if not parsed:
        raise ValueError('No recognised Antigravity quota groups')
    # Intentionally permit no Argon group for account tiers where it is not
    # separately listed; callers display unknown rather than borrow another.
    return parsed


def read(root, *, now=None):
    now=time.time() if now is None else now
    state=load_json(path(root), {}) or {}
    measured=state.get('measured_at')
    groups=state.get('groups')
    groups=groups if isinstance(groups,dict) else {}
    fresh=(isinstance(measured, (int, float)) and
           0 <= now-measured <= FRESH_SECONDS and
           bool(groups))
    return {'fresh': bool(fresh), 'measured_at': measured,
            'last_attempt_at': state.get('last_attempt_at'),
            'error': state.get('error'),
            'cli_version': state.get('cli_version'),
            'groups': groups}


def refresh(root, config=None, *, now=None, force=False, run=None, locate=None):
    """Bounded polling and error retention without inferring missing limits."""
    root=Path(root).resolve()
    now=time.time() if now is None else now
    config=config or {}
    raw_interval=config.get('gemini_usage_poll_seconds', DEFAULT_INTERVAL_SECONDS)
    interval=raw_interval if isinstance(raw_interval, (int, float)) and not isinstance(raw_interval, bool) else DEFAULT_INTERVAL_SECONDS
    interval=max(60, min(3600, interval))
    previous=load_json(path(root), {}) or {}
    last=previous.get('last_attempt_at')
    if not force and isinstance(last, (int, float)) and 0 <= now-last < interval:
        return read(root, now=now)
    executor=run or subprocess.run
    try:
        binary=locate() if locate else cli_path()
        if not binary:
            raise RuntimeError('agy CLI missing from WSL; install and sign into Antigravity')
        version_result=executor([binary, '--version'],capture_output=True,text=True,timeout=6)
        if version_result.returncode:
            raise RuntimeError('agy --version failed')
        cli_version=parse_version((version_result.stdout or '')+' '+(version_result.stderr or ''))
        result=executor([binary,'-p','/usage','--output-format','json'],
                        capture_output=True,text=True,timeout=15)
        if result.returncode:
            raise RuntimeError('agy /usage exited unsuccessfully')
        parsed=parse(result.stdout)
        state={'measured_at':now, 'last_attempt_at':now,
               'cli_version':cli_version, 'groups':parsed,'error':None}
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        # Keep the last good reading, but persist no CLI stderr or prompt text.
        state={**previous,'last_attempt_at':now,
               'error':type(exc).__name__+': '+str(exc)[:140]}
    atomic_write_json(path(root),state)
    return read(root,now=now)
