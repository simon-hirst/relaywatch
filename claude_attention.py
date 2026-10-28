"""Low-noise, notification-only alerts for native Claude Code attention events.

These events include tool permission requests, MCP elicitation dialogs, and
background-agent/team prompts. They are deliberately NOT recorded as
needs-input.json: a prompt may be answered, denied, or auto-resolved without
a user turn, and must not block RelayWatch's quota handoff state machine.
Never transmit a tool command, environment, URL, or form values in alerts.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from alerts import send_event
from engine import atomic_write_json, load_json

ATTENTION_TYPES = {
    'permission_prompt': ('permission', 'Claude needs permission'),
    'agent_needs_input': ('subagent', 'Claude background agent needs input'),
    'elicitation_dialog': ('elicitation', 'Claude MCP form needs input'),
    'elicitation_url_dialog': ('elicitation', 'Claude MCP authentication needs input'),
}
REPEAT_SECONDS = 75
FALLBACK_DUPLICATE_SECONDS = 20
MAX_AGE_SECONDS = 24 * 3600


def path(root):
    return Path(root) / '.agent-watchdog' / 'claude-attention.json'


def describe(event, payload):
    """Classify an explicit event; return a safe title, body and fingerprint.

    The fingerprint may hash sensitive arguments, but the persisted ledger
    stores only this digest, never actual tool_input/message/form content.
    """
    if not isinstance(payload, dict):
        return None
    kind = None
    title = None
    identifier = ''
    if event == 'PermissionRequest':
        kind, title = 'permission', 'Claude needs tool permission'
        tool = payload.get('tool_name')
        if isinstance(tool, str) and re.fullmatch(r'[A-Za-z][\w:-]{0,79}', tool):
            title += f' ({tool[:44]})'
        identifier = json.dumps([tool, payload.get('tool_input')],
                                sort_keys=True, default=str, ensure_ascii=True)[:10000]
    elif event == 'Notification':
        label = payload.get('notification_type')
        result = ATTENTION_TYPES.get(label)
        if not result:
            return None
        kind, title = result
        # Native notification is sometimes a delayed fallback for the same
        # PermissionRequest. Its raw message may include private details.
        identifier = str(label)
    elif event == 'Elicitation':
        kind, title = 'elicitation', 'Claude MCP server needs input'
        identifier = json.dumps([
            payload.get('mcp_server_name'), payload.get('elicitation_id'),
            payload.get('mode'), payload.get('message')],
            sort_keys=True, default=str, ensure_ascii=True)[:10000]
    else:
        return None
    session = str(payload.get('session_id') or '')[:128]
    signature = hashlib.sha256(json.dumps(
        [session, kind, identifier], sort_keys=True).encode('utf-8')).hexdigest()[:28]
    body = ('Claude Code is requesting your attention. Open the managed Claude '
            'session on your computer or, if Remote Control is active, '
            'in the Claude mobile app. Nothing was approved automatically.')
    return kind, title, body, signature


def forward(root, config, event, payload, *, now=None, sender=send_event):
    """Forward event without replying to Claude or mutating approval state.

    Return the channel result for diagnostics; failed channels can be retried
    if the same event is emitted again. Successful duplicate prompts in a
    short window are suppressed, as are delayed permission notifications.
    """
    info = describe(event, payload)
    if info is None:
        return None
    kind, title, body, signature = info
    now = time.time() if now is None else now
    target = path(root)
    state = load_json(target, {}) or {}
    events = state.get('events', {})
    if not isinstance(events, dict):
        events = {}
    events = {key: value for key, value in events.items()
              if isinstance(value, dict) and isinstance(value.get('at'), (int, float))
              and now - MAX_AGE_SECONDS < value['at'] <= now}
    if (event == 'Notification' and payload.get('notification_type') == 'permission_prompt'
            and state.get('last_permission_sent_session') == payload.get('session_id')
            and isinstance(state.get('last_permission_sent_at'), (int, float))
            and 0 <= now - state['last_permission_sent_at'] < FALLBACK_DUPLICATE_SECONDS):
        return None
    prior = events.get(signature, {})
    if (prior.get('sent') and 0 <= now - prior.get('at', -1e15) < REPEAT_SECONDS):
        return None
    # Alerts are advisory: a desktop "sent" means launch was attempted, not
    # proof Windows displayed a toast. The user must still answer in Claude.
    results = sender(root, config, title, body, body)
    if not isinstance(results, dict):
        results = {'error': 'unexpected alert-sender result'}
    delivered = any(value == 'sent' for value in results.values())
    events[signature] = {'at': now, 'sent': delivered,
                         'channels': {key: str(val)[:160] for key, val in results.items()}}
    state = {'events': dict(list(events.items())[-120:]),
             'last_kind': kind, 'last_seen_at': now,
             'last_permission_sent_at': state.get('last_permission_sent_at'),
             'last_permission_sent_session': state.get('last_permission_sent_session')}
    if event == 'PermissionRequest' and delivered:
        state.update({'last_permission_sent_at': now,
                      'last_permission_sent_session': payload.get('session_id')})
    atomic_write_json(target, state)
    return results
