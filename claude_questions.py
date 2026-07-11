"""Read-only question detection for the managed Claude TUI.

Claude can display USER_DECISION_REQUEST while it keeps working, so a Stop
hook alone does not reliably notify the user. Monitor only the managed pane's
*visible* lines; never scrape the entire scrollback or type into the agent.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path

from alerts import detect_question, register_question, request_path
from capacity import clean as strip_ansi
from engine import atomic_write_json, load_json
from runner import latest_worker, session_name, tmux_exists


def path(root):
    return Path(root) / '.agent-watchdog' / 'claude-questions.json'


def visible_question(screen):
    if not isinstance(screen, str):
        return None
    # tmux -J joins wrapped output so long questions remain one logical line.
    # The visible window, not history, is the authoritative source.
    lines = strip_ansi(screen).splitlines()[-36:]
    question, origin = detect_question('\n'.join(lines))
    if not question:
        return None
    return question, origin


def read_managed(root, *, run=subprocess.run):
    worker = latest_worker(root)
    if not (worker.get('agent') == 'claude' and worker.get('type') == 'tmux'
            and worker.get('outcome') == 'launched'
            and worker.get('session') == session_name(root)
            and re.fullmatch(r'@\d+', str(worker.get('window', '')))):
        return None, 'not_managed_claude'
    if not tmux_exists(worker['session']):
        return None, 'tmux_unavailable'
    try:
        panes = run(['tmux', 'list-panes', '-t', worker['window'], '-F', '#{pane_id}'],
                    capture_output=True, text=True, timeout=5)
        ids = (panes.stdout or '').splitlines() if panes.returncode == 0 else []
        if len(ids) != 1 or not re.fullmatch(r'%\d+', ids[0].strip()):
            return None, 'pane_unverified'
        capture = run(['tmux', 'capture-pane', '-p', '-J', '-t', ids[0].strip()],
                      capture_output=True, text=True, timeout=5)
        if capture.returncode:
            return None, 'capture_unavailable'
        return capture.stdout, 'observed'
    except (OSError, subprocess.TimeoutExpired):
        return None, 'capture_unavailable'


def check(root, config, *, now=None, run=subprocess.run):
    """Record a new visible question; polling alert sender handles ntfy retries.

    Keep an episode latch while the same text remains visible. Otherwise an
    answered question sitting in the terminal would be resurrected every 20s.
    """
    now = time.time() if now is None else now
    storage = path(root)
    old = load_json(storage, {}) or {}
    screen, status = read_managed(root, run=run)
    if status == 'not_managed_claude':
        if old.get('visible'):
            atomic_write_json(storage, {**old, 'visible': False, 'status': status,
                                        'last_checked_at': now})
        return None
    if status != 'observed':
        return None  # An unavailable pane does not mean a question was answered.
    parsed = visible_question(screen)
    if not parsed:
        if old.get('visible'):
            misses = int(old.get('misses', 0)) + 1
            state = {**old, 'misses': misses, 'last_checked_at': now}
            if misses >= 2:
                state.update({'visible': False, 'status': 'not_visible'})
            atomic_write_json(storage, state)
        return None
    question, origin = parsed
    digest = hashlib.sha256(question.encode('utf-8')).hexdigest()[:24]
    if old.get('visible') and old.get('signature') == digest:
        atomic_write_json(storage, {**old, 'last_seen_at': now,
                                    'last_checked_at': now, 'misses': 0})
        return None
    # A Stop hook may have registered/alerted before this scan. Conversely a
    # genuine user may already have answered while the old text remains visible.
    # Do not reactivate that already-answered question on first observation.
    previous = load_json(request_path(root), {}) or {}
    already_answered = (previous.get('agent') == 'claude'
                        and previous.get('question') == question
                        and previous.get('status') in ('answered', 'acknowledged')
                        and old.get('signature') != digest)
    fresh = False
    if not already_answered:
        fresh = register_question(root, 'claude', question, source='claude_live_' + origin)
    state = {'visible': True, 'signature': digest, 'status': 'question_visible',
             'first_seen_at': now, 'last_seen_at': now, 'misses': 0,
             'registered': bool(fresh), 'question_preview': question[:100]}
    atomic_write_json(storage, state)
    if fresh:
        return 'CLAUDE QUESTION serviceERVED: queued for desktop/phone alerts'
    return None
