"""Validated provider reset timestamps and host-local display formatting.

Absolute API reset instants are authoritative; never infer a reset instant from
quota consumption, or simply add 5 hours / 7 days to the current time.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_RESET_NAMES = {
    'reset_at', 'resets_at', 'reset_time', 'resets_time', 'reset_timestamp',
    'resets_timestamp', 'next_reset', 'next_reset_at', 'next_reset_time',
    'next_reset_timestamp', 'reset_date', 'resets_on',
    'resetat', 'resetsat', 'nextreset', 'nextresetat', 'resettimestamp',
}
_RELATIVE_RESET_SECONDS = {'reset_in_seconds', 'resets_in_seconds', 'seconds_until_reset'}
_WIN_TZ = {
    'gmt standard time': 'Europe/London',
    'w. europe standard time': 'Europe/Berlin',
    'romance standard time': 'Europe/Paris',
    'central europe standard time': 'Europe/Budapest',
    'eastern standard time': 'America/New_York',
    'central standard time': 'America/Chicago',
    'mountain standard time': 'America/Denver',
    'pacific standard time': 'America/Los_Angeles',
    'us mountain standard time': 'America/Phoenix',
    'aus eastern standard time': 'Australia/Sydney',
    'tokyo standard time': 'Asia/Tokyo',
    'india standard time': 'Asia/Kolkata',
    'utc': 'Etc/UTC',
}


def _normal(key):
    return re.sub(r'[^a-z0-9]+', '_', str(key).lower()).strip('_')


def parse_reset_timestamp(raw, *, now=None):
    """Return epoch seconds from a timezone-qualified ISO or Unix timestamp.

    Reject ambiguous naive wall times, booleans, unreasonable epochs, and
    numbers that resemble percentages or a small interval without units.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    timestamp = None
    if isinstance(raw, (int, float)):
        timestamp = float(raw)
    elif isinstance(raw, str):
        value = raw.strip()
        if re.fullmatch(r'\d{10}(?:\.\d+)?|\d{13}', value):
            timestamp = float(value)
        else:
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00').replace('z', '+00:00'))
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    return None
                timestamp = parsed.timestamp()
            except (ValueError, OverflowError):
                return None
    if timestamp is None or not math.isfinite(timestamp):
        return None
    if timestamp >= 1e12:
        timestamp /= 1000  # Javascript / Unix milliseconds
    # A 5h or 7d reset is expected to be reasonably close to the reading.
    # Don't accept bizarre future dates or unrelated historical timestamp fields.
    when = time.time() if now is None else float(now)
    if not when - 24 * 3600 <= timestamp <= when + 15 * 86400:
        return None
    return timestamp


def _scope_window(path):
    """Import lazily to avoid coupling the usage percentage parser to UI logic."""
    from usage import window_name
    return window_name(path)


def resets_from_json(obj, metrics, *, now=None):
    """Match per-window reset fields to the selected used-percent metric paths.

    Providers may duplicate data under buckets or nested usage keys. Prefer a
    reset beside the selected usage measurement, never a root/global reset.
    """
    now = time.time() if now is None else now
    candidates = []

    def visit(item, path='$'):
        if isinstance(item, dict):
            for key, value in item.items():
                normalized = _normal(key)
                child_path = f'{path}.{key}'
                if isinstance(value, (dict, list)):
                    visit(value, child_path)
                    continue
                when = None
                if normalized in _RESET_NAMES:
                    when = parse_reset_timestamp(value, now=now)
                elif normalized in _RELATIVE_RESET_SECONDS:
                    # Explicit API countdown, *not* a guessed quota duration.
                    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 15 * 86400:
                        when = now + value
                if when is not None and _scope_window(path) in ('session', 'weekly'):
                    candidates.append((path, when))
        elif isinstance(item, list):
            for index, value in enumerate(item):
                visit(value, f'{path}.{index}')

    visit(obj)
    result = {}
    for name, metric in metrics.items():
        if name not in ('session', 'weekly'):
            continue
        path = metric.get('metric', '')
        if not isinstance(path, str):
            continue
        metric_parent = path.rpartition('.')[0]
        target_components = metric_parent.split('.')
        ranked = []
        for parent, when in candidates:
            if _scope_window(parent) != name:
                continue
            reset_components = parent.split('.')
            match = 0
            for a, b in zip(target_components, reset_components):
                if a != b:
                    break
                match += 1
            # A reset adjacent to a selected metric wins over a sibling alias.
            ranked.append((match, parent == metric_parent, when))
        if ranked:
            ranked.sort(reverse=True)
            result[name] = round(ranked[0][2], 3)
    return result


def _get_zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None


@lru_cache(maxsize=2)
def local_timezone(configured=None):
    """Prefer explicit project setting; otherwise discover Windows' zone in WSL.

    On many WSL installs, /etc/localtime is UTC even when the Windows clock is
    British Summer Time. Check Windows once per watchdog process, not per poll.
    """
    requested = configured if configured and configured != 'auto' else os.environ.get('AGENT_WATCHDOG_TIMEZONE')
    if requested and _get_zone(requested):
        return _get_zone(requested)
    if os.environ.get('WSL_DISTRO_NAME') or os.path.exists('/proc/sys/fs/binfmt_misc/WSLInterop'):
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                     '[System.TimeZoneInfo]::Local.Id'], capture_output=True,
                                    text=True, timeout=3, check=False)
            if result.returncode == 0:
                winid = result.stdout.strip().splitlines()[-1].strip().lower()
                tz = _get_zone(_WIN_TZ.get(winid, ''))
                if tz:
                    return tz
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    return datetime.now().astimezone().tzinfo


def format_reset_local(timestamp, zone, *, now=None):
    """A short, timezone-aware local reset label, or explicit unknown/stale."""
    if timestamp is None:
        return 'reset unknown'
    now = time.time() if now is None else now
    try:
        timestamp = float(timestamp)
        if not math.isfinite(timestamp):
            return 'reset unknown'
        if timestamp < now - 60:
            return 'reset pending refresh'
        local = datetime.fromtimestamp(timestamp, zone)
        today = datetime.fromtimestamp(now, zone)
        if local.date() == today.date():
            return f'reset {local:%H:%M %Z}'
        return f'reset {local:%a %d %b %H:%M %Z}'
    except (ValueError, OverflowError, OSError, TypeError):
        return 'reset unknown'
