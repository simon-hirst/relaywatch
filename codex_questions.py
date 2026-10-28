"""Detect Codex's *live* queued-input indicator and send one alert per episode.

Codex may queue a follow-up question while continuing to work. Its Stop hook
and the explicit NEEDS_USER_INPUT marker do not run at that time, so the
watchdog must inspect the bottom of its own tmux pane. This is read-only:
never select an answer, send keys, or infer that trial/deploy approval exists.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import time
import uuid
from pathlib import Path

from alerts import (_phone_push, _windows_desktop, detect_question,
                    register_question, request_path)
from capacity import clean as strip_ansi
from engine import atomic_write_json, load_json
from runner import latest_worker, session_name, tmux_exists

COUNT = re.compile(r'^\s*[?？]\s*(\d+)\s+questions?\b', re.I)
HEADING = re.compile(r'\bqueued\s+follow[- ]up\s+inputs?\b', re.I)


def path(root):
    return Path(root) / '.agent-watchdog' / 'codex-questions.json'


def visible_path(root):
    return Path(root) / '.agent-watchdog' / 'codex-visible-questions.json'


# A completed Codex turn can ask for a decision without the queued-input
# footer, and formal markers can appear during an ongoing turn.
DIRECT_QUESTION = re.compile(
    r'(?i)^(?:should|shall|may|can) i\b|'
    r'^(?:would|do) you (?:like|want|prefer|approve)\b|'
    r'^which (?:option|approach|one) (?:would|do) you\b|'
    r'^(?:please|could you) (?:confirm|choose|approve)\b'
)
ASSISTANT_START = re.compile(r'^\s*[•●]\s+(?!Ran\b|Explored\b|Working\b|Running\b|Read\b)', re.I)
COMPOSER = re.compile(r'^\s*[›❯]\s*(?:Ask Codex(?:\s*\(.*\))?)?\s*
    """Read only the current TUI footer, never scrollback/transcript history."""
    if not isinstance(screen, str):
        return None
    lines = strip_ansi(screen).splitlines()[-18:]
    for i, line in enumerate(lines):
        if not HEADING.search(line):
            continue
        for candidate in lines[i + 1:i + 5]:
            match = COUNT.match(candidate)
            if match:
                count = int(match.group(1))
                return count if 0 < count < 1000 else None
    return None


def live_count(root, *, run=subprocess.run, include_screen=False):
    """Return (count, diagnostics). None count means absent; errors are unknown."""
    worker = latest_worker(root)
    if not (worker.get('agent') == 'codex' and worker.get('type') == 'tmux'
            and worker.get('outcome') == 'launched'
            and worker.get('session') == session_name(root)
            and re.fullmatch(r'@\d+', str(worker.get('window', '')))):
        return (None, 'not_managed_codex', None) if include_screen else (None, 'not_managed_codex')
    if not tmux_exists(worker['session']):
        return (None, 'tmux_unavailable', None) if include_screen else (None, 'tmux_unavailable')
    try:
        panes = run(['tmux', 'list-panes', '-t', worker['window'], '-F', '#{pane_id}'],
                    capture_output=True, text=True, timeout=5)
        ids = (panes.stdout or '').splitlines() if panes.returncode == 0 else []
        if len(ids) != 1 or not re.fullmatch(r'%\d+', ids[0].strip()):
            return (None, 'pane_unverified', None) if include_screen else (None, 'pane_unverified')
        screen = run(['tmux', 'capture-pane', '-p', '-J', '-t', ids[0].strip()],
                     capture_output=True, text=True, timeout=5)
        if screen.returncode:
            return (None, 'capture_unavailable', None) if include_screen else (None, 'capture_unavailable')
    except (OSError, subprocess.TimeoutExpired):
        return (None, 'capture_unavailable', None) if include_screen else (None, 'capture_unavailable')
    return (queued_count(screen.stdout), 'observed', screen.stdout) if include_screen else (queued_count(screen.stdout), 'observed')


def question_open(root):
    item = load_json(path(root), {}) or {}
    # Only current, explicitly observed queued questions count as active.
    return bool(item.get('active') and item.get('count', 0) > 0)


def check(root, config, *, now=None, run=subprocess.run,
          desktop_sender=_windows_desktop, phone_sender=_phone_push):
    """Poll the owned Codex pane; notify only on a fresh/retryable question.

    Missing tmux access must NOT be interpreted as the user answering. The
    marker is not a global needs-input blocker: Codex can work safely locally
    while waiting for a response to a deployment/approval question.
    """
    now = time.time() if now is None else now
    storage = path(root)
    old = load_json(storage, {}) or {}
    count, status, screen = live_count(root, run=run, include_screen=True)
    if status == 'not_managed_codex':
        if old.get('active'):
            atomic_write_json(storage, {**old, 'active': False, 'status': status,
                                         'last_checked_at': now})
        return None
    if status != 'observed':
        return None  # No authoritative reading; preserve existing state.
    visible = check_visible_decision(root, screen, now=now) if count is None else None
    if count is None:
        # The screen can flicker between drawing states. Two consecutive clean
        # absences, rather than one transient redraw, end an alert episode.
        if old.get('active'):
            misses = int(old.get('misses', 0)) + 1
            next_state = {**old, 'misses': misses, 'last_checked_at': now}
            if misses >= 2:
                next_state.update({'active': False, 'status': 'no_visible_question'})
            atomic_write_json(storage, next_state)
        return visible
    if not old.get('active') or old.get('count') != count:
        state = {'active': True, 'status': 'queued', 'count': count,
                 'episode': uuid.uuid4().hex, 'first_seen_at': now,
                 'last_seen_at': now, 'misses': 0, 'alerts': {}}
    else:
        state = {**old, 'last_seen_at': now, 'last_checked_at': now, 'misses': 0}
    settings = config.get('alerts', {})
    channel_enabled = {'desktop': settings.get('desktop', True),
                       'phone': settings.get('phone', {}).get('enabled', False)}
    retry = max(30, int(config.get('alert_retry_seconds', 120)))
    noun = 'question' if count == 1 else 'questions'
    title = f'Codex needs input ({count} {noun})'
    desktop_text = (f'Codex has {count} queued {noun}. Open its terminal and press '
                    'Shift+Left Arrow to answer. No approval was granted automatically.')
    phone_text = (f'Codex has {count} queued {noun}. Open the Codex session to answer; '
                  'no action was approved automatically.')
    results = {}
    for channel, enabled in channel_enabled.items():
        if not enabled:
            continue
        previous = state['alerts'].get(channel, {})
        if previous.get('sent') or now - previous.get('attempted_at', -1e15) < retry:
            continue
        try:
            result = (desktop_sender(root, title, desktop_text) if channel == 'desktop'
                      else phone_sender(settings, title, phone_text))
        except Exception as exc:
            result = f'unavailable: {type(exc).__name__}: {exc}'
        state['alerts'][channel] = {'sent': result == 'sent',
                                    'attempted_at': now, 'result': result}
        results[channel] = result
    atomic_write_json(storage, state)
    if results:
        return f'CODEX QUEUED USER QUESTION ({count}): ' + ', '.join(f'{c}={v}' for c, v in results.items())
    return visible
, re.I)


def visible_decision(screen):
    """Find requests only in the visible managed Codex TUI, never scrollback.

    Broad question-mark matching is unsafe: stdout and historic instructions
    frequently contain questions. A plain question needs an idle compositor,
    final assistant text and strong user-directed wording.
    """
    if not isinstance(screen, str):
        return None
    lines = strip_ansi(screen).splitlines()[-36:]
    question, origin = detect_question('\n'.join(lines))
    if question:
        return question, 'codex_live_' + origin
    composer = next((i for i in range(len(lines) - 1, max(-1, len(lines) - 5), -1)
                     if COMPOSER.fullmatch(lines[i])), None)
    if composer is None:
        return None
    preceding = lines[max(0, composer - 22):composer]
    if any(re.search(r'(?i)\bworking\s*\(\d+[ms]|\bqueued follow-up inputs\b', line)
           for line in preceding[-5:]):
        return None
    starts = [i for i, line in enumerate(preceding) if ASSISTANT_START.match(line)]
    if not starts:
        return None
    response = preceding[starts[-1]:]
    if any(line.strip().startswith(('│', '└', '~~~', chr(96)*3)) for line in response):
        return None
    final = next((line.strip() for line in reversed(response) if line.strip()), '')
    if final.startswith(('•', '●')):
        final = re.sub(r'^[•●]\s*', '', final)
    if not final.endswith('?') or not DIRECT_QUESTION.search(final):
        return None
    return final[:500], 'codex_live_idle_question'


def check_visible_decision(root, screen, *, now):
    """Record once until the visible question disappears or is answered."""
    storage = visible_path(root)
    old = load_json(storage, {}) or {}
    observed = visible_decision(screen)
    if not observed:
        if old.get('visible'):
            misses = int(old.get('misses', 0)) + 1
            state = {**old, 'misses': misses, 'last_checked_at': now}
            if misses >= 2:
                state.update({'visible': False, 'status': 'not_visible'})
            atomic_write_json(storage, state)
        return None
    question, origin = observed
    signature = hashlib.sha256(question.encode('utf-8')).hexdigest()[:24]
    if old.get('visible') and old.get('signature') == signature:
        atomic_write_json(storage, {**old, 'last_checked_at': now,
                                    'last_seen_at': now, 'misses': 0})
        return None
    previous = load_json(request_path(root), {}) or {}
    already_answered = (previous.get('agent') == 'codex'
                        and previous.get('question') == question
                        and previous.get('status') in ('answered', 'acknowledged', 'superseded')
                        and old.get('signature') != signature)
    fresh = False
    if not already_answered:
        fresh = register_question(root, 'codex', question, now=now, source=origin)
    atomic_write_json(storage, {
        'visible': True, 'signature': signature, 'status': 'question_visible',
        'first_seen_at': now, 'last_seen_at': now, 'misses': 0,
        'registered': bool(fresh), 'question_preview': question[:100]})
    return 'CODEX VISIBLE QUESTION: queued for desktop/phone alerts' if fresh else None


def queued_count(screen):
    """Read only the current TUI footer, never scrollback/transcript history."""
    if not isinstance(screen, str):
        return None
    lines = strip_ansi(screen).splitlines()[-18:]
    for i, line in enumerate(lines):
        if not HEADING.search(line):
            continue
        for candidate in lines[i + 1:i + 5]:
            match = COUNT.match(candidate)
            if match:
                count = int(match.group(1))
                return count if 0 < count < 1000 else None
    return None


def live_count(root, *, run=subprocess.run):
    """Return (count, diagnostics). None count means absent; errors are unknown."""
    worker = latest_worker(root)
    if not (worker.get('agent') == 'codex' and worker.get('type') == 'tmux'
            and worker.get('outcome') == 'launched'
            and worker.get('session') == session_name(root)
            and re.fullmatch(r'@\d+', str(worker.get('window', '')))):
        return (None, 'not_managed_codex', None) if include_screen else (None, 'not_managed_codex')
    if not tmux_exists(worker['session']):
        return (None, 'tmux_unavailable', None) if include_screen else (None, 'tmux_unavailable')
    try:
        panes = run(['tmux', 'list-panes', '-t', worker['window'], '-F', '#{pane_id}'],
                    capture_output=True, text=True, timeout=5)
        ids = (panes.stdout or '').splitlines() if panes.returncode == 0 else []
        if len(ids) != 1 or not re.fullmatch(r'%\d+', ids[0].strip()):
            return (None, 'pane_unverified', None) if include_screen else (None, 'pane_unverified')
        screen = run(['tmux', 'capture-pane', '-p', '-J', '-t', ids[0].strip()],
                     capture_output=True, text=True, timeout=5)
        if screen.returncode:
            return (None, 'capture_unavailable', None) if include_screen else (None, 'capture_unavailable')
    except (OSError, subprocess.TimeoutExpired):
        return (None, 'capture_unavailable', None) if include_screen else (None, 'capture_unavailable')
    return queued_count(screen.stdout), 'observed'


def question_open(root):
    item = load_json(path(root), {}) or {}
    # Only current, explicitly observed queued questions count as active.
    return bool(item.get('active') and item.get('count', 0) > 0)


def check(root, config, *, now=None, run=subprocess.run,
          desktop_sender=_windows_desktop, phone_sender=_phone_push):
    """Poll the owned Codex pane; notify only on a fresh/retryable question.

    Missing tmux access must NOT be interpreted as the user answering. The
    marker is not a global needs-input blocker: Codex can work safely locally
    while waiting for a response to a deployment/approval question.
    """
    now = time.time() if now is None else now
    storage = path(root)
    old = load_json(storage, {}) or {}
    count, status = live_count(root, run=run)
    if status == 'not_managed_codex':
        if old.get('active'):
            atomic_write_json(storage, {**old, 'active': False, 'status': status,
                                         'last_checked_at': now})
        return None
    if status != 'observed':
        return None  # No authoritative reading; preserve existing state.
    if count is None:
        # The screen can flicker between drawing states. Two consecutive clean
        # absences, rather than one transient redraw, end an alert episode.
        if old.get('active'):
            misses = int(old.get('misses', 0)) + 1
            next_state = {**old, 'misses': misses, 'last_checked_at': now}
            if misses >= 2:
                next_state.update({'active': False, 'status': 'no_visible_question'})
            atomic_write_json(storage, next_state)
        return None
    if not old.get('active') or old.get('count') != count:
        state = {'active': True, 'status': 'queued', 'count': count,
                 'episode': uuid.uuid4().hex, 'first_seen_at': now,
                 'last_seen_at': now, 'misses': 0, 'alerts': {}}
    else:
        state = {**old, 'last_seen_at': now, 'last_checked_at': now, 'misses': 0}
    settings = config.get('alerts', {})
    channel_enabled = {'desktop': settings.get('desktop', True),
                       'phone': settings.get('phone', {}).get('enabled', False)}
    retry = max(30, int(config.get('alert_retry_seconds', 120)))
    noun = 'question' if count == 1 else 'questions'
    title = f'Codex needs input ({count} {noun})'
    desktop_text = (f'Codex has {count} queued {noun}. Open its terminal and press '
                    'Shift+Left Arrow to answer. No approval was granted automatically.')
    phone_text = (f'Codex has {count} queued {noun}. Open the Codex session to answer; '
                  'no action was approved automatically.')
    results = {}
    for channel, enabled in channel_enabled.items():
        if not enabled:
            continue
        previous = state['alerts'].get(channel, {})
        if previous.get('sent') or now - previous.get('attempted_at', -1e15) < retry:
            continue
        try:
            result = (desktop_sender(root, title, desktop_text) if channel == 'desktop'
                      else phone_sender(settings, title, phone_text))
        except Exception as exc:
            result = f'unavailable: {type(exc).__name__}: {exc}'
        state['alerts'][channel] = {'sent': result == 'sent',
                                    'attempted_at': now, 'result': result}
        results[channel] = result
    atomic_write_json(storage, state)
    if results:
        return f'CODEX QUEUED USER QUESTION ({count}): ' + ', '.join(f'{c}={v}' for c, v in results.items())
    return None
