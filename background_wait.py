"""Recognise and track an agent that is awaiting an already-running job.

There is no user approval implied by waiting. Crucially, a checkpoint tool call
is NOT evidence that the asynchronous job has finished: its notification (or a
human instruction) is the thing that should restart the workflow.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from engine import atomic_write_json, load_json

FORMAL = re.compile(r'(?im)^\s*(?:[-*]\s+)?(?:\*\*)?WAITING_ON_BACKGROUND(?:\*\*)?\s*:\s*(\S[^\r\n]*)')

# Require an identifiable running process, not merely an agent claiming to
# have nothing to do. These include the real test-host service measurement wording.
ASYNC_ACTIVITY = re.compile(
    r'(?i)\b(?:'
    r'background\s+(?:watch|job|task|process|run|measurement|command|test)|'
    r'(?:ongoing|running|guarded)\s+(?:watch|measurement|test|run|process)|'
    r'watch(?:er)?\s+(?:is\s+)?still\s+(?:running|waiting)|'
    r'arm\s*\d+\s+is\s+still\s+being\s+measured|'
    r'(?:\w+(?:[- ]\w+){0,3}\s+)?(?:run|test|measurement|watch)\s+'
    r'(?:is\s+)?still\s+(?:in\s+progress|running|measuring|being\s+measured)|'
    r'(?:run|test|measurement|watch)\s+(?:began|started)\s+(?:at|around)\s+\d|'
    r'(?:run|test|measurement|watch)\b.{0,125}?\b(?:finishes|completes|ends)\s+'
    r'(?:around|at|by)\s+\d{1,2}(?::\d{2})?)'
)

WAIT_FOR_RESULT = re.compile(
    r'(?i)\b(?:'
    r'wait(?:ing)?\s+(?:for|until)|'
    r'until\s+(?:it|the\s+(?:job|run|test|watch|measurement)|its\s+(?:watch|result))\s+'
    r'(?:finish(?:es)?|complet(?:es|ion)|return(?:s)?)|'
    r'(?:no\s+other\s+local\s+work|nothing\s+(?:else|more)\s+(?:safe|actionable|local)|'
    r'nothing\s+to\s+do\s+until)|'
    r'when\s+the\s+(?:background\s+)?(?:watch|job|task|run|measurement)\s+'
    r'(?:reports|completes|finishes)|'
    r'watch\s+(?:is\s+)?still\s+waiting|'
    r'(?:i(?:\s+will|\s+shall|\x27ll)?\s+report\s+(?:back\s+)?from\s+its\s+watch)|'
    r'(?:its|the)\s+result\s+(?:decides|determines)\s+(?:the\s+)?next|'
    r'(?:expected|due|scheduled)\s+to\s+finish\s+(?:around|at|by)\s+\d|'
    r'nothing\s+else\s+may\s+use\s+(?:the|that|this)\s+(?:host|server)\s+meanwhile)\b'
)

BAD_WAIT = re.compile(r'(?i)\b(?:quota\s+reset|rate\s+limit\s+reset|user\s+(?:approval|permission|answer|decision)|waiting\s+for\s+(?:your|user)\s+(?:approval|permission|reply|response))\b')

# Strong signals that a *previously tracked* job has completed. A final
# response containing these signals can leave the waiting state and follow
# the normal continuation policy; otherwise an ambiguous status remains held.
COMPLETED = re.compile(
    r'(?i)(?:^|[\s.])(?:BACKGROUND_JOB_COMPLETE\s*:\s*\S+|'
    r'(?:the\s+|its\s+)?(?:background\s+)?(?:watch|job|run|test|measurement|process)\s+'
    r'(?:has\s+|is\s+)?(?:finished|completed|returned|concluded)|'
    r'(?:results?|completion\s+notice|completion\s+notification)\s+'
    r'(?:have\s+|has\s+|are\s+|is\s+)?(?:arrived|returned|received|ready|in\s+hand|show|confirm|indicate)|'
    r'(?:geometry|restoration)\s+(?:was\s+|is\s+|has\s+been\s+)?(?:verified|confirmed)|'
    r'(?:watch|measurement)\s+(?:has\s+)?(?:reported|returned))\b'
)


def _visible(message):
    if not isinstance(message, str):
        return ''
    lines = []
    fenced = False
    for raw in message.splitlines():
        line = raw.strip()
        if line.startswith(('```', '~~~')):
            fenced = not fenced
            continue
        if fenced or line.startswith(('>', '|')):
            continue
        lines.append(raw)
    return '\n'.join(lines)


def detect_background_wait(message):
    """Return a short description when a final assistant message reports a live wait."""
    clean = _visible(message)
    if not clean.strip() or BAD_WAIT.search(clean) or re.search(r"(?i)\b(?:do\s+not|don't|never)\s+wait\s+for\b", clean):
        return None
    marker = FORMAL.search(clean)
    if marker:
        return marker.group(1).strip()[:300]
    # Ignore quoted examples of old status text in otherwise unrelated prose.
    natural = re.sub(r'["“][^"”\n]{0,600}["”]', '', clean)
    if ASYNC_ACTIVITY.search(natural) and WAIT_FOR_RESULT.search(natural):
        return 'Background job in progress: ' + ASYNC_ACTIVITY.search(natural).group(0)[:100]
    return None


def reports_background_completion(message):
    """Only a positive completion report clears a *persisting* background wait."""
    clean = _visible(message)
    return bool(clean and COMPLETED.search(clean) and not detect_background_wait(clean))


def path(root):
    return Path(root)/'.agent-watchdog'/'background-wait.json'


def note(root, agent, session_id, description, *, now=None):
    previous = load_json(path(root), {}) or {}
    same = (previous.get('agent') == agent and previous.get('session_id') == session_id
            and previous.get('status') == 'waiting')
    timestamp = time.time() if now is None else now
    atomic_write_json(path(root), {
        'agent': agent, 'session_id': session_id, 'status': 'waiting',
        'description': description[:300],
        'started_at': previous.get('started_at', timestamp) if same else timestamp,
        'last_seen_at': timestamp})


def finish(root, agent, session_id=None, *, event='agent_activity'):
    previous = load_json(path(root), {}) or {}
    if (previous.get('status') != 'waiting' or previous.get('agent') != agent
            or (session_id and previous.get('session_id') and previous.get('session_id') != session_id)):
        return False
    atomic_write_json(path(root), {**previous, 'status': 'finished',
                                   'finished_at': time.time(), 'finished_by': event})
    return True


def waiting_for(root, agent, session_id=None):
    previous = load_json(path(root), {}) or {}
    if (previous.get('status') != 'waiting' or previous.get('agent') != agent
            or (session_id and previous.get('session_id') and previous.get('session_id') != session_id)):
        return {}
    return previous


def current(root):
    state = load_json(path(root), {}) or {}
    worker = load_json(Path(root)/'.agent-watchdog'/'worker.json', {}) or {}
    if (state.get('status') != 'waiting' or worker.get('agent') != state.get('agent')
            or worker.get('outcome') != 'launched'):
        return {}
    return state
