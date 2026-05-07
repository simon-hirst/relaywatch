"""User-owned, persistent pause latch for the automatic agent workflow.

Only a deliberate CLI `resume` removes the latch: model-generated `continue`
messages must NEVER clear it. The historical sentinel filename is retained.
"""
from __future__ import annotations
import re
import time
from pathlib import Path
from engine import atomic_write_json

NAME = 'PAUSE_AUTO_CONTINUE'
MESSAGE = ('AUTOMATION PAUSED BY USER. Do not continue tasks, call tools, or '
           'start another agent. To resume, the user must run watchdog.py resume '
           '(or watchdog-windows.ps1 -Command resume).')


def flag(root):
    return Path(root) / '.agent-watchdog' / NAME


def paused(root):
    return flag(root).is_file()


def is_pause_request(prompt):
    """Catch short, standalone stop/pause commands, not discussion about pauses."""
    if not isinstance(prompt, str):
        return False
    text = re.sub(r'\s+', ' ', prompt.strip()).lower()
    if not text or len(text) > 160:
        return False
    if re.fullmatch(r'(?:wait[ ,.!-]+)?(?:pause|hold|stop|wait)(?:[.! ]*)', text):
        return True
    return bool(re.match(
        r'^(?:wait[ ,.!-]+)?(?:pause|hold|stop)(?:\s+(?:everything|all|working|development|the agent|auto(?:matic)?[ -]?(?:continue|work)|until\b|for\b)|[!.])',
        text))


def activate(root, source='manual'):
    path=flag(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # File remains compatible with older builds; never overwrite project files.
    path.touch(exist_ok=True)
    atomic_write_json(path.parent/'pause-state.json', {'source': source, 'at': time.time()})
    return path


def deactivate(root):
    path=flag(root)
    existed=path.exists()
    path.unlink(missing_ok=True)
    (path.parent/'pause-state.json').unlink(missing_ok=True)
    return existed
