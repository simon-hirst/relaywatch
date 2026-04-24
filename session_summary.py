"""Require a real agent-authored session digest before a final handoff."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime, timezone

REQUIRED=('completed','verification','next')


def directory(root): return Path(root)/'docs'/'session-reports'


def new_filename(agent):
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')+f'-{agent}.md'


def current_summary(root,agent,since):
    if not isinstance(since,(float,int)) or since<=0:return None
    for item in sorted(directory(root).glob(f'*-{agent}.md'),key=lambda x:x.stat().st_mtime,reverse=True):
        try:
            if item.stat().st_mtime<since-5 or item.stat().st_size<180:continue
            body=item.read_text(encoding='utf-8',errors='replace').lower()
            if all(word in body for word in REQUIRED):return item
        except OSError:pass
    return None


def instructions(agent):
    return ('Write a concise, factual session-specific Markdown summary at '
            f'`docs/session-reports/{new_filename(agent)}` (create directory if needed). '
            'Include work completed THIS SESSION, relevant changed files, verification commands '
            'and results, unresolved issues, and the next tasks. Link to docs/HANDOFF.md '
            'and do not expose secrets. The Stop hook requires this summary before handoff.')
