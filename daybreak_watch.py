"""Watchdog-only assistance for Codex Daybreak access transitions.

Read-only detection and explicit user acknowledgement. Do not type `/daybreak`
blindly: it's a toggle with a persistent default, and slash `/model` is an
interactive picker whose selected model cannot be reliably inferred from a
project preference. Never treat a restriction as approval for cyber activity.
"""
from __future__ import annotations

import re
import subprocess
import time
import uuid
from pathlib import Path

from alerts import _windows_desktop, _phone_push
from capacity import clean as strip_ansi
from engine import load_json, atomic_write_json
from runner import latest_worker, session_name, tmux_exists
from model_picker import saved
from alerts import is_automatic_continuation

RESTRICTION = re.compile(r"this\s+content\s+can(?:['\u2019])?t\s+be\s+shown", re.I)
CYBER = re.compile(r'cybersecurity|daybreak|broader access|authorized security work', re.I)
DAYBREAK_OFF = re.compile(r'daybreak\s+is\s+currently\s+off', re.I)
DONE = re.compile(r'^\s*DAYBREAK_TASK_COMPLETE\s*:\s*\S', re.I | re.M)


def path(root):
    return Path(root) / '.agent-watchdog' / 'daybreak-recovery.json'


def detect(text):
    """Only a current refusal, not a generic mention of Daybreak."""
    if not isinstance(text, str):
        return False
    clean = strip_ansi(text).replace('\r', '')
    if DAYBREAK_OFF.search(clean) and re.search(r'authorized security work|turn it on|next request', clean, re.I):
        return True
    match = RESTRICTION.search(clean)
    return bool(match and CYBER.search(clean[max(0,match.start()-120):match.end()+350]))


def visible_refusal(screen):
    """Never search an unlimited transcript or a historical tmux scrollback."""
    if not isinstance(screen, str):
        return False
    lines = strip_ansi(screen).splitlines()
    # A recent TUI refusal is typically a 3-line message near its composer.
    return detect('\n'.join(lines[-24:]))


def read_managed_codex(root, *, run=subprocess.run):
    worker = latest_worker(root)
    if not (worker.get('agent') == 'codex' and worker.get('type') == 'tmux'
            and worker.get('outcome') == 'launched'
            and worker.get('session') == session_name(root)
            and re.fullmatch(r'@\d+', str(worker.get('window', '')))):
        return None, 'not_managed_codex'
    if not tmux_exists(worker['session']):
        return None, 'tmux_unavailable'
    try:
        p = run(['tmux', 'list-panes', '-t', worker['window'], '-F', '#{pane_id}'],
                capture_output=True, text=True, timeout=5)
        ids = (p.stdout or '').splitlines() if p.returncode == 0 else []
        if len(ids) != 1 or not re.fullmatch(r'%\d+', ids[0].strip()):
            return None, 'pane_unverified'
        result = run(['tmux', 'capture-pane', '-p', '-J', '-t', ids[0].strip()],
                     capture_output=True, text=True, timeout=5)
        if result.returncode:
            return None, 'capture_unavailable'
        return result.stdout, 'observed'
    except (OSError, subprocess.TimeoutExpired):
        return None, 'capture_unavailable'


def current(root):
    state = load_json(path(root), {}) or {}
    return state if state.get('active') else {}


def note(root, *, now=None, origin='codex_refusal', fresh_stop=False):
    now = time.time() if now is None else now
    old = load_json(path(root), {}) or {}
    if old.get('active'):
        # Polling may still see a historical refusal in the tmux pane. Only a
        # NEW native Stop event can classify a real second refusal after the
        # user's next turn, without spinning on stale screen text.
        if fresh_stop and old.get('phase') == 'in_progress':
            atomic_write_json(path(root), {**old, 'phase':'needs_switch',
                'rejected_again_at': now, 'alerts': {}, 'last_seen_at':now})
            return True
        return False
    if old.get('armed') is False:
        return False
    # This is the *saved watchdog preset*, not a claim about the active CLI.
    try:
        preference = dict(saved().get('codex', {}))
    except Exception:
        preference = {}
    state = {'active': True, 'armed': False, 'phase': 'needs_switch',
             'episode': uuid.uuid4().hex, 'started_at': now,
             'source': origin, 'saved_watchdog_preset': preference,
             'alerts': {}, 'last_seen_at': now, 'clear_observations': 0}
    atomic_write_json(path(root), state)
    return True



# Do not use a model-written answer or an injected watchdog continuation as
# proof of permission. A real user-submitted turn in the *current* Codex CLI
# is sufficient to unblock the workflow without a PowerShell acknowledgment;
# Codex still enforces its own Daybreak access on each request.
# Explicit complete user message, not a phrase embedded in instructions,
# quoted output, or a hypothetical question. This is a user assertion,
# never cryptographic proof of the active TUI setting.
RESTORE_CONFIRMATION = re.compile(
    r'(?is)^\s*(?:daybreak is off and (?:the |my )?(?:previous|original) model(?:/effort| and effort)? '
    r'(?:is |are )?restored|'
    r'i (?:have )?turned daybreak off and (?:have )?(?:restored|switched back to) '
    r'(?:the |my )?(?:previous|original) model(?:/effort| and effort)?)\s*[.!]?\s*$'
)



def user_prompt(root, prompt, *, now=None):
    """Implicit phone-friendly acknowledgment on a real Codex user turn.

    This is not evidence Daybreak is on: keep the approved-scope and access
    checks with Codex itself. Restoration still needs explicit human words.
    """
    if is_automatic_continuation(prompt):
        return None
    old = current(root)
    if not old:
        return None
    now = time.time() if now is None else now
    if old.get('phase') == 'needs_switch':
        atomic_write_json(path(root), {**old,
            'phase': 'in_progress', 'acknowledged_at': now,
            'acknowledged_by': 'genuine_codex_user_prompt', 'alerts': {}})
        return ('CODEX DAYBREAK WATCHDOG: Your new user turn releases the watchdog acknowledgment '
                'without a desktop command. This does NOT confirm Daybreak access, model, or '
                'authorization. If you enabled Daybreak for approved security work, complete '
                'only that work, then emit DAYBREAK_TASK_COMPLETE: <result>. If access is '
                'still restricted, report the refusal; do not loop.')
    if old.get('phase') == 'needs_restore':
        if not RESTORE_CONFIRMATION.fullmatch(str(prompt or "")):
            return None
        atomic_write_json(path(root), {**old,
            'active': False, 'phase':'restored_confirmed_in_codex',
            'restored_at': now, 'restored_by':'genuine_codex_user_prompt',
            'alerts': {}})
        return ('CODEX DAYBREAK WATCHDOG: Recorded the user’s in-chat statement that '
                'Daybreak is OFF and the original model/effort was restored. '
                'The watchdog did not independently verify TUI state.')
    return None


def completed_work(root, message, *, now=None):
    if not DONE.search(str(message or '')):
        return False
    old = current(root)
    if not old or old.get('phase') != 'in_progress':
        return False
    atomic_write_json(path(root), {**old, 'phase': 'needs_restore',
                                   'restore_due_at': time.time() if now is None else now,
                                   'alerts': {}})
    return True


def proceed(root, *, now=None):
    """Human asserts they enabled approved Daybreak and selected the model."""
    old = current(root)
    if not old:
        return 'NO DAYBREAK INCIDENT: nothing to acknowledge.'
    if old.get('phase') == 'needs_restore':
        return 'RESTORE REQUIRED: turn Daybreak off and restore the original model before acknowledging restoration.'
    if old.get('phase') == 'in_progress':
        return 'ALREADY ACKNOWLEDGED: complete authorized security task, then restore Daybreak/model.'
    atomic_write_json(path(root), {**old, 'phase': 'in_progress',
                                   'acknowledged_at': time.time() if now is None else now,
                                   'alerts': {}})
    return ('DAYBREAK RECOVERY ACKNOWLEDGED: temporary security workflow allowed for authorized scope only. '
            'When done, use /daybreak to turn it OFF and /model to restore your prior choice. '
            'The watchdog cannot verify those interactive settings automatically.')


def restored(root, *, now=None):
    """Explicit confirmation; never claim we independently verified CLI state."""
    old = current(root)
    if not old:
        return 'NO ACTIVE DAYBREAK INCIDENT: nothing to clear.'
    atomic_write_json(path(root), {**old, 'active': False, 'phase': 'restored_by_user',
                                   'restored_at': time.time() if now is None else now,
                                   'alerts': {}})
    return ('RECORDED USER CONFIRMATION: Daybreak is OFF and the original model/effort are restored. '
            'Actual CLI settings were not independently verified.')


def description(root):
    item = current(root)
    if not item:
        return None
    prior = item.get('saved_watchdog_preset') or {}
    model = prior.get('model', 'unknown')
    effort = prior.get('effort', 'unknown')
    if item.get('phase') == 'needs_switch':
        return ('Codex security restriction — for authorised work, set /daybreak ON and '
                '/model -> GPT-6 Sol / max inside Codex, then retry from Codex/phone. '
                'No desktop acknowledgment needed.')
    if item.get('phase') == 'needs_restore':
        return (f'RESTORE CODEX: /daybreak OFF, /model to prior model/effort '
                f"(saved preset {model}/{effort}); then tell Codex: "
                "Daybreak is off and previous model/effort restored")
    return (f'Temporary Codex Daybreak mode acknowledged — restore /daybreak OFF '
            f'and previous model/effort when security task finishes (saved preset: {model}/{effort})')


def alert_if_needed(root, config, *, now=None,
                    desktop_sender=_windows_desktop, phone_sender=_phone_push):
    item = current(root)
    if not item:
        return None
    now = time.time() if now is None else now
    phase = item.get('phase')
    if phase not in ('needs_switch', 'needs_restore'):
        return None
    title = ('Codex security task: Daybreak needs attention' if phase == 'needs_switch'
             else 'Codex security task: restore model and Daybreak')
    desktop_message = (description(root) or '')[:580]
    phone_message = ('Codex hit a Daybreak restriction. Open the managed terminal; '
                     'only use it for approved security work.' if phase == 'needs_switch'
                     else 'Restore Daybreak OFF and your original Codex model/effort. '
                          'Confirm from your phone in Codex: Daybreak is off and previous model/effort restored.')
    settings = config.get('alerts', {})
    enabled = {'desktop': settings.get('desktop', True),
               'phone': settings.get('phone', {}).get('enabled', False)}
    retry = max(30, int(config.get('alert_retry_seconds', 120)))
    outcomes = {}
    for channel, active in enabled.items():
        if not active:
            continue
        previous = item.get('alerts', {}).get(channel, {})
        if previous.get('sent') or now-previous.get('attempted_at', -1e15) < retry:
            continue
        try:
            result = (desktop_sender(root, title, desktop_message) if channel == 'desktop'
                      else phone_sender(settings, title, phone_message))
        except Exception as exc:
            result = f'unavailable: {type(exc).__name__}: {exc}'
        item.setdefault('alerts', {})[channel] = {'sent': result == 'sent',
                                                  'result': result, 'attempted_at': now}
        outcomes[channel] = result
    if outcomes:
        atomic_write_json(path(root), item)
        return 'CODEX DAYBREAK ACTION: ' + ', '.join(f'{k}={v}' for k,v in outcomes.items())
    return None


def check(root, config, *, now=None, run=subprocess.run,
          desktop_sender=_windows_desktop, phone_sender=_phone_push):
    now = time.time() if now is None else now
    screen, status = read_managed_codex(root, run=run)
    if status == 'observed':
        is_refusal = visible_refusal(screen)
        item = load_json(path(root), {}) or {}
        if is_refusal:
            note(root, now=now)
        elif item.get('armed') is False:
            # Do not treat one terminal redraw as clearing the old refusal.
            misses = int(item.get('clear_observations', 0)) + 1
            atomic_write_json(path(root), {**item, 'clear_observations': misses,
                                           'armed': misses >= 2})
    return alert_if_needed(root, config, now=now,
                           desktop_sender=desktop_sender, phone_sender=phone_sender)
