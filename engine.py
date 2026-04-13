"""State engine shared between polling watchdog and CLI lifecycle hooks."""
from __future__ import annotations
import json, os, tempfile, time
from pathlib import Path
from usage import get_api_usage

PROMPT_PREP = ('USAGE WATCHDOG: You are at or above 80% used on a limiting quota. '
               'Begin preparing docs/HANDOFF.md AND a dated docs/session-reports/ report now, finish only bounded/verifiable work, '
               'and avoid starting long tasks. Do NOT wait for the quota to reset while safe bounded work remains; '
               '90% USED is the final handoff threshold, not a target to waste tokens reaching. '
               'Archive the final handoff when you stop.')
PROMPT_HANDOFF = ('URGENT USAGE WATCHDOG: You are at or above 90% used on a limiting quota. '
                  'STOP NEW FEATURE WORK. Write/refresh docs/HANDOFF.md and archive it in '
                  'docs/session-handoffs/, including completed changes, exact test outcomes, repo/diff status, '
                  'remaining tasks, and explicit operational restrictions (including any project-specific operational restrictions). '
                  'Do not deploy, commit, push, discard changes, or change production settings without authorization. '
                  'Write a dated, session-specific summary in docs/session-reports/ including completed work, verification, failures and next tasks. '
                  'After verifying the handoff and summary files exist, run `python3 .agent-watchdog/watchdog.py ready --agent <your-agent>` '
                  '(substitute codex or claude) as your LAST tool action, then end your turn. '
                  'This is an explicit completion acknowledgement if the Stop hook fails. '
                  'Do not continue implementation.')


def atomic_write_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd,name = tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,indent=2,sort_keys=True); f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def load_json(path, default=None):
    try: return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,json.JSONDecodeError):return default


def stage(remaining, prepare=20, stop=10):
    if remaining<=stop:return 'handoff'
    if remaining<=prepare:return 'prepare'
    return 'normal'


def state_path(root, agent):return Path(root)/'.agent-watchdog'/f'usage-{agent}.json'


def update(root, agent, config, *, now=None):
    now=time.time() if now is None else now
    opts=config['agents'][agent]
    prior=load_json(state_path(root,agent),{}) or {}
    result=get_api_usage(opts['url'], used_pct_path=opts.get('used_pct_path'))
    value=stage(result['remaining_percent'], config['prepare_remaining_pct'], config['handoff_remaining_pct'])
    prevstage=prior.get('stage','normal')
    # The handoff is relevant to the current quota event (rearm after usage resets).
    # Keep triggered_at stable across polls in the same exhaustion period.
    triggered = prior.get('triggered_at') if prevstage=='handoff' and value=='handoff' else (now if value=='handoff' else None)
    prep_at = (prior.get('prep_at') or now) if value in ('prepare','handoff') and prevstage in ('prepare','handoff') else (now if value in ('prepare','handoff') else None)
    info={**result,'agent':agent,'stage':value,'timestamp':now,'triggered_at':triggered,'prep_at':prep_at}
    atomic_write_json(state_path(root,agent),info)
    return info


def current(root, agent, config, *,max_age=None):
    max_age=config.get('cache_seconds',20) if max_age is None else max_age
    s=load_json(state_path(root,agent))
    if s and time.time()-s.get('timestamp',0)<=max_age:return s
    try:return update(root,agent,config)
    except Exception as exc:
        # No guessing on failure: never treat an unavailable endpoint as 90%.
        if s and s.get('timestamp') and time.time()-s['timestamp']<config.get('stale_grace_seconds',120):
            return {**s,'stale':True,'error':type(exc).__name__+': '+str(exc)}
        return {'agent':agent,'stage':'unknown','error':type(exc).__name__+': '+str(exc),'timestamp':time.time()}


def handoff_current(root,info):
    target=Path(root)/'docs'/'HANDOFF.md'
    if not target.is_file() or target.stat().st_size<180:return False
    # The installer creates a non-authoritative placeholder, not a completed handoff.
    content=target.read_text(encoding='utf-8',errors='replace')[:2000]
    if 'Bootstrap placeholder; not a verified implementation handoff' in content:return False
    t=info.get('prep_at') or info.get('triggered_at')
    # Require a real update since the current prepare/handoff period started.
    return bool(t) and target.stat().st_mtime >= (t-5)
