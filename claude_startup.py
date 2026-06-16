"""Verified initial user-turn delivery into a watchdog-managed Claude tmux pane.

`tmux send-keys` returning 0 only means bytes reached the terminal. Claude Code can
keep the text in its composer after swallowing the first Enter. Do not report
success until its native UserPromptSubmit hook confirms the *exact* first turn.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

INITIAL_MESSAGE = (
    'Read CLAUDE.md, docs/WORKFLOW.md and docs/HANDOFF.md. '
    'Give me a concise takeover summary (completed, current, next, blockers), '
    'then continue with the next safe local task automatically. Follow the '
    'watchdog quota/handoff rules. Do not deploy, commit or push without permission.'
)

_IDLE_LINE = re.compile(r'^\s*[❯›>]\s*(?:Try\s|Ask\s|Type\s|$)', re.I)
_SHELL = re.compile(r'(?:\w+@\S+[:$]|\[watchdog\]\s+Agent exited)', re.I)

def looks_idle(screen: str) -> bool:
    if 'Claude Code' not in screen:
        return False
    lines = [s.strip() for s in screen.splitlines() if s.strip()]
    if any(_SHELL.search(s) for s in lines[-4:]):
        return False
    return any(_IDLE_LINE.match(s) for s in lines[-7:])


def draft_visible(screen: str) -> bool:
    """Only retry Enter when the exact handoff is still in the visible composer.

    The `-J` tmux capture joins wrapped lines. A stale user turn farther up the
    screen must not cause a retry; require the end of the message near the foot.
    """
    if 'Claude Code' not in screen:
        return False
    tail = ' '.join(screen.splitlines()[-16:])
    text = ' '.join(tail.split())
    return (INITIAL_MESSAGE[:42] in text and INITIAL_MESSAGE[-51:] in text
            and not _SHELL.search(text[-180:]))


def state_file(root: Path) -> Path:
    return root / '.agent-watchdog' / 'claude-prompt-delivery.json'


def save_state(root: Path, obj: dict) -> None:
    path = state_file(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    tmp.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def load_state(root: Path) -> dict:
    try:
        return json.loads(state_file(root).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def mark_received(root: Path, prompt: str) -> bool:
    """Called only by Claude's UserPromptSubmit hook, not by terminal injection."""
    if not isinstance(prompt, str) or ' '.join(prompt.split()) != INITIAL_MESSAGE:
        return False
    state = load_state(Path(root))
    if not state.get('token') or state.get('status') not in ('typing', 'submitting', 'confirmed'):
        return False
    state.update(status='confirmed', confirmed_at=time.time(), confirmation='UserPromptSubmit')
    save_state(Path(root), state)
    return True


def tmux(argv, *, run=subprocess.run):
    return run(['tmux', *argv], capture_output=True, text=True, timeout=5)


def deliver(root: Path, pane: str, *, max_seconds: int=65, interval: float=0.75,
            run=subprocess.run, sleep=time.sleep, now=time.monotonic) -> str:
    """Verify the composer, type once, then retry *Enter only* up to three times.

    Never send another copy of the text, and never press Enter once the initial
    message disappears from the composer. When the hook can't confirm submission,
    report an honest failure and let the user intervene.
    """
    if not re.fullmatch(r'%\d+', pane):
        return 'unsafe tmux pane identifier; no text injected'
    root = Path(root).resolve()
    token = uuid.uuid4().hex
    save_state(root, {'status': 'waiting', 'pane': pane, 'token': token, 'started': time.time()})
    deadline = now() + max_seconds
    previous = None
    stable = 0

    def capture():
        try:
            cp = tmux(['capture-pane', '-p', '-J', '-t', pane], run=run)
            return cp.stdout or '' if cp.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    def is_current():
        return load_state(root).get('token') == token

    def fail(reason, status='not_delivered'):
        state = load_state(root)
        if state.get('token') == token:
            if state.get('status') == 'confirmed':
                return 'confirmed'
            save_state(root, {**state, 'status': status, 'reason': reason})
        return f'not delivered: {reason}'

    # Wait until a stable, identified *empty* Claude composer is visible.
    while now() < deadline:
        if not is_current():
            return 'superseded by a newer Claude launch'
        screen = capture()
        if screen is None:
            return fail('tmux pane unavailable')
        if looks_idle(screen):
            stable = stable + 1 if screen == previous else 1
            if stable >= 2:
                break
        else:
            stable = 0
        previous = screen
        sleep(interval)
    else:
        return fail('interactive Claude composer not verified before timeout')

    if not is_current():
        return 'superseded by a newer Claude launch'
    save_state(root, {**load_state(root), 'status': 'typing'})
    try:
        typed = tmux(['send-keys', '-t', pane, '-l', '--', INITIAL_MESSAGE], run=run)
        if typed.returncode:
            return fail('tmux rejected literal handoff text')
    except (OSError, subprocess.TimeoutExpired):
        return fail('tmux could not send handoff text')

    # Let terminal paste detection and the TUI render settle; immediately
    # sending Enter can insert a newline instead of submitting the draft.
    sleep(0.9)
    if not is_current():
        return 'superseded by a newer Claude launch'
    screen = capture()
    if screen is None or not draft_visible(screen):
        return fail('typed text could not be verified in Claude composer')

    save_state(root, {**load_state(root), 'status': 'submitting', 'attempts': 0})
    for attempt in range(1, 4):
        if now() >= deadline:
            return fail('submission timed out')
        state = load_state(root)
        if state.get('token') != token:
            return 'superseded by a newer Claude launch'
        if state.get('status') == 'confirmed':
            return 'confirmed'
        # Never retry a submitted or changed draft. On the first attempt we
        # verified it above; subsequent tries require fresh visual evidence.
        if attempt > 1:
            screen = capture()
            if screen is None or not draft_visible(screen):
                break
        try:
            key = tmux(['send-keys', '-t', pane, 'Enter'], run=run)
            if key.returncode:
                return fail('tmux rejected Enter')
        except (OSError, subprocess.TimeoutExpired):
            return fail('tmux could not send Enter')
        state = load_state(root)
        if state.get('token') == token and state.get('status') != 'confirmed':
            save_state(root, {**state, 'attempts': attempt})
        # Pause so the TUI can submit and the UserPromptSubmit hook can run.
        # If the first Enter was swallowed, the same draft remains visible.
        for _ in range(4):
            sleep(0.4)
            state = load_state(root)
            if state.get('token') != token:
                return 'superseded by a newer Claude launch'
            if state.get('status') == 'confirmed':
                return 'confirmed'
        if attempt < 3:
            screen = capture()
            if screen is None or not draft_visible(screen):
                break

    # A submitted prompt may briefly disappear before the hook can persist the
    # receipt. Give it a bounded grace period; do not send any extra keystrokes.
    for _ in range(6):
        if now() >= deadline:
            break
        sleep(0.5)
        state = load_state(root)
        if state.get('token') != token:
            return 'superseded by a newer Claude launch'
        if state.get('status') == 'confirmed':
            return 'confirmed'
    return fail('Claude UserPromptSubmit hook did not confirm the handoff; check the visible terminal')


def main():
    if len(sys.argv) != 3:
        print('Usage: claude_startup.py <project-root> <%tmux-pane-id>', file=sys.stderr)
        return 2
    result = deliver(Path(sys.argv[1]), sys.argv[2])
    if result != 'confirmed':
        print('[watchdog] Claude startup message ' + result, file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
