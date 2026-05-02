"""Conservative response to missing usage telemetry; never guesses percentages.

No automatic handoff is issued merely because an API is down. Instead the
managed source agent is told by its lifecycle hooks to checkpoint and, after
sustained outage, stop with a verified handoff. When hooks cannot run the human
is notified. Only a fresh handoff plus a real acknowledgement may switch.
"""
from __future__ import annotations
import time
from pathlib import Path
from engine import load_json, atomic_write_json


def path(root, agent):
    return Path(root) / '.agent-watchdog' / f'outage-{agent}.json'


def read(root, agent):
    obj=load_json(path(root, agent), {}) or {}
    return obj if obj.get('agent')==agent else {}


def failed(root, agent, config, *, now=None):
    now=time.time() if now is None else now
    prior=read(root,agent)
    start=prior.get('first_failed_at',now) if prior.get('state') in ('grace','checkpoint','pause') else now
    age=max(0,now-start)
    checkpoint=max(15,int(config.get('telemetry_checkpoint_seconds',60)))
    pause=max(checkpoint+15,int(config.get('telemetry_pause_seconds',180)))
    state='pause' if age>=pause else ('checkpoint' if age>=checkpoint else 'grace')
    data={'agent':agent,'state':state,'first_failed_at':start,'last_checked_at':now,
          'age_seconds':round(age,1),'checkpoint_at':start+checkpoint,
          'pause_at':start+pause}
    if prior.get('state')=='pause' and prior.get('first_failed_at')==start:
        data['escalated_at']=prior.get('escalated_at',start+pause)
    elif state=='pause':
        data['escalated_at']=now
    atomic_write_json(path(root,agent),data)
    return data, prior.get('state')!=state


def restored(root,agent):
    obj=read(root,agent)
    if obj.get('state') not in ('grace','checkpoint','pause'):
        return False
    atomic_write_json(path(root,agent),{'agent':agent,'state':'healthy','recovered_at':time.time()})
    return True


def directive(root,agent):
    data=read(root,agent)
    state=data.get('state')
    if state=='checkpoint':
        return ('USAGE TELEMETRY OUTAGE: The usage API is unavailable; do not guess your remaining quota. '
                'Immediately checkpoint current work and update docs/HANDOFF.md. '
                'Only undertake a short safe verification step, not a large new task. '
                'At your next lifecycle checkpoint, re-evaluate telemetry. ')
    if state=='pause':
        return ('SUSTAINED USAGE TELEMETRY OUTAGE: Stop new implementation now. '
                'Complete docs/HANDOFF.md and a dated docs/session-reports/ entry; '
                'verify both, then acknowledge ready with the watchdog as your last tool action. '
                'Do not guess a usage percentage or continue building until telemetry recovers. ')
    return ''


def qualifies(root,agent):
    data=read(root,agent)
    return data.get('state')=='pause' and isinstance(data.get('escalated_at'),(int,float))


def fresh_handoff(root,agent):
    if not qualifies(root,agent):return False
    target=Path(root)/'docs'/'HANDOFF.md'
    try:
        return target.stat().st_size>180 and target.stat().st_mtime>=read(root,agent)['escalated_at']-5
    except (OSError,ValueError,KeyError):return False
