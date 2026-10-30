"""Advisory-only Antigravity questions and 10%-remaining weekly handoff warning.

Agy hooks report ask_question/ask_permission before their tools run. Hooks
never approve permissions, answer questions, or change execution flow.
Native permission cards do not have a dedicated event, so a narrowly scoped
read-only tmux fallback detects unmistakable *visible* approval prompts.

The weekly Argon warning is ONE episode per verified reset_time, only when
the *Argon group's own weekly bucket* reports <=10% remaining and a managed
Gemini session is active. The next PreInvocation injects a single advisory
ephemeralMessage telling the agent to prepare docs/HANDOFF.md. No 20%
warning, no automatic CLI switching, no tmux key injection.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from alerts import send_event
from capacity import clean as strip_ansi
from engine import atomic_write_json, load_json
from gemini_session import (gemini_session_name, record_path, blocks_auto_handoff)
from runner import latest_worker, session_name
from gemini_usage import read as read_quota
from pause_control import paused

RETRY_SECONDS = 120
ALERT_REPEAT = 90
HANDOFF_MESSAGE = (
    'RelayWatch verified Gemini 4 Argon has 10% or less of its WEEKLY quota '
    'remaining. At your next safe checkpoint, write/update docs/HANDOFF.md '
    'with completed work, current task, files changed, tests, blockers, and '
    'specific next steps. Also create a dated docs/session-reports/ summary. '
    'Do not begin additional large tasks or exhaust the weekly allowance. '
    'Do not switch agents, end this CLI, commit, deploy, or grant permissions '
    'automatically. Tell the user when the handoff is saved.'
)


def alert_path(root):
    return Path(root)/'.agent-watchdog'/'gemini-attention.json'


def warning_path(root):
    return Path(root)/'.agent-watchdog'/'gemini-weekly-warning.json'


def _managed(root, *, active=blocks_auto_handoff):
    record=load_json(record_path(root),{}) or {}
    if record.get('agent')!='gemini' or record.get('outcome')!='launched':
        return False
    if record.get('session')==gemini_session_name(root):
        return bool(active(root))
    # Automatic fallback uses the existing Claude/Codex managed tmux session.
    # Never notify on a stale record for an earlier worker/window.
    worker=latest_worker(root)
    return (record.get('session')==session_name(root)
            and worker.get('session')==session_name(root)
            and worker.get('agent')=='gemini'
            and worker.get('type')=='tmux'
            and worker.get('outcome')=='launched'
            and worker.get('window')==record.get('window')
            and bool(active(root)))


def _emit(root, config, title, body, record, now, *, sender=send_event):
    """Per-channel retry ledger: already sent channels never repeat."""
    alerts=config.get('alerts') or {}
    channels={'desktop':bool(alerts.get('desktop', True)),
              'phone':bool((alerts.get('phone') or {}).get('enabled',False))}
    ledger=record.get('channels') or {}
    due={key:yes and not (ledger.get(key) or {}).get('sent')
         and now-(ledger.get(key) or {}).get('attempted_at',-1e20)>=RETRY_SECONDS
         for key,yes in channels.items()}
    if not any(due.values()):
        return None, record
    safe_config={**config,'alerts':{**alerts,
        'desktop':due['desktop'],
        'phone':{**(alerts.get('phone') or {}),'enabled':due['phone']}}}
    try:
        results=sender(root,safe_config,title,body,body)
    except Exception:
        results={}
    for key,yes in due.items():
        if yes:
            result=results.get(key,'failed') if isinstance(results,dict) else 'failed'
            ledger[key]={'sent':result=='sent','attempted_at':now}
    return results, {**record,'channels':ledger}


def _signature(kind, identifier):
    return hashlib.sha256(json.dumps([kind,identifier],sort_keys=True,
             ensure_ascii=True,default=str).encode('utf-8')).hexdigest()[:28]


def _workspace_matches(root, payload):
    paths=payload.get('workspacePaths')
    if not isinstance(paths,list) or not paths:
        return False
    try:
        target=Path(root).resolve()
        return any(isinstance(p,str) and Path(p).resolve()==target for p in paths)
    except (OSError,ValueError):
        return False


def notify_question(root, config, payload, *, now=None, sender=send_event,
                    active=blocks_auto_handoff):
    """Notify for explicit Antigravity interactive question/permission tools."""
    if not isinstance(payload,dict) or not _workspace_matches(root,payload):
        return None
    if not _managed(root,active=active):
        return None
    tool=payload.get('toolCall') or {}
    name=tool.get('name') if isinstance(tool,dict) else None
    if name not in ('ask_question','ask_permission'):
        return None
    now=time.time() if now is None else now
    kind='question' if name=='ask_question' else 'permission'
    # Session/step IDs distinguish separate questions, while raw tool
    # arguments, questions, secrets, paths and names are never persisted.
    identifier=[payload.get('conversationId'),payload.get('stepIdx'),name,
                tool.get('args')]
    key=_signature(kind, identifier)
    state=load_json(alert_path(root),{}) or {}
    history=state.get('events') if isinstance(state.get('events'),dict) else {}
    history={k:v for k,v in history.items() if isinstance(v,dict)
             and isinstance(v.get('at'),(int,float)) and 0<=now-v['at']<=86400}
    record=history.get(key,{})
    if record.get('at') is None:
        record={'at':now,'kind':kind}
    title=('Antigravity needs an answer' if kind=='question'
           else 'Antigravity needs permission')
    body=('Open the managed Antigravity session in the terminal or '
          'Antigravity Remote Control on your phone to respond. '
          'RelayWatch never approves or answers automatically.')
    results,record=_emit(root,config,title,body,record,now,sender=sender)
    history[key]=record
    atomic_write_json(alert_path(root),{'events':dict(list(history.items())[-120:]),
                 'last_hook_at':now})
    return results


def _weekly_episode(quota):
    if not isinstance(quota,dict) or not quota.get('fresh'):
        return None
    groups=quota.get('groups') or {}
    argon=groups.get('Gemini 4 Argon') if isinstance(groups,dict) else None
    weekly=argon.get('weekly') if isinstance(argon,dict) else None
    if not isinstance(weekly,dict):
        return None
    remaining=weekly.get('remaining_fraction')
    reset=weekly.get('reset_time')
    identifier=weekly.get('id')
    if (not isinstance(remaining,(int,float)) or isinstance(remaining,bool)
            or not 0<=remaining<=0.10 or not isinstance(reset,str)
            or not isinstance(identifier,str) or not identifier):
        return None
    return _signature('argon_weekly_10pct',[identifier,reset]), remaining*100


def warn_at_ten(root, config, quota, *, now=None, sender=send_event,
                active=blocks_auto_handoff):
    """One weekly episode; notify desktop and ntfy separately with retries."""
    if paused(root) or not _managed(root,active=active):
        return None
    event=_weekly_episode(quota)
    if event is None:
        return None
    key,percent=event
    now=time.time() if now is None else now
    state=load_json(warning_path(root),{}) or {}
    if state.get('episode')!=key:
        state={'episode':key,'at':now,'channels':{},'injected':False}
    body=(f'Argon weekly quota is at {percent:.1f}% remaining (10% threshold). '
          'Prepare and save docs/HANDOFF.md and a session report at the next '
          'safe checkpoint. No automatic handoff or model switch.')
    results,state=_emit(root,config,'Antigravity: prepare weekly handoff',
                        body,state,now,sender=sender)
    atomic_write_json(warning_path(root),state)
    return ('GEMINI WEEKLY HANDOFF WARNING at <=10% remaining: '
            +str(results)) if results else None


def inject_handoff_note(root, payload, *, active=blocks_auto_handoff,
                        quota_reader=read_quota):
    """One advisory message per weekly episode, at an actual model turn.

    A quota warning cannot safely inject into an active composer. The official
    PreInvocation ephemeral-message hook sends the warning when the next model
    invocation begins. If no invocation occurs, the desktop/phone warning
    remains the sole notification.
    """
    if not isinstance(payload,dict) or not _workspace_matches(root,payload):
        return {}
    if not str(payload.get('modelName','')).startswith('gemini-4-argon-'):
        return {}
    if not _managed(root,active=active) or paused(root):
        return {}
    event=_weekly_episode(quota_reader(root))
    if event is None:
        return {}
    state=load_json(warning_path(root),{}) or {}
    if state.get('episode')!=event[0] or state.get('injected'):
        return {}
    atomic_write_json(warning_path(root),{**state,'injected':True,
                                         'injected_at':time.time()})
    return {'injectSteps':[{'ephemeralMessage':HANDOFF_MESSAGE}]}


# Agy may ask for approval through its permission engine without invoking the
# explicit ask_permission tool. Only flag unmistakable *visible* TUI cards,
# not any question mark or historic model text; best-effort, not exhaustive.
_APPROVAL_TITLE=re.compile(r'(?i)\b(permission (?:required|request|needed)|'
                            r'approval (?:required|needed)|requires your approval|'
                            r'subagent (?:needs|requests) (?:permission|approval))\b')
_CHOICES=re.compile(r'(?i)\b(allow once|always allow|deny|reject|approve)\b')


def visible_prompt(screen):
    if not isinstance(screen,str):
        return None
    lines=strip_ansi(screen).splitlines()[-22:]
    part='\n'.join(lines)
    if _APPROVAL_TITLE.search(part) and len(set(_CHOICES.findall(part.lower())))>=2:
        return _signature('visible_permission',part[-1500:])
    return None


def check_visible(root,config, *, now=None, run=subprocess.run,
                  sender=send_event, active=blocks_auto_handoff):
    """Read only the managed Gemini tmux pane; no keystrokes or transcript."""
    root=Path(root).resolve()
    if not _managed(root,active=active):
        return None
    record=load_json(record_path(root),{}) or {}
    window=record.get('window')
    if not re.fullmatch(r'@\d+',str(window or '')):
        return None
    try:
        res=run(['tmux','list-panes','-t',window,'-F','#{pane_id}'],
                capture_output=True,text=True,timeout=5)
        ids=(res.stdout or '').splitlines() if res.returncode==0 else []
        if len(ids)!=1 or not re.fullmatch(r'%\d+',ids[0].strip()):
            return None
        pane=run(['tmux','capture-pane','-p','-J','-t',ids[0].strip()],
                 capture_output=True,text=True,timeout=5)
        if pane.returncode:
            return None
    except (OSError,subprocess.TimeoutExpired):
        return None
    now=time.time() if now is None else now
    key=visible_prompt(pane.stdout)
    storage=alert_path(root)
    state=load_json(storage,{}) or {}
    episode=state.get('visible') if isinstance(state.get('visible'),dict) else {}
    if not key:
        if episode.get('active'):
            misses=episode.get('misses',0)+1
            episode={**episode,'misses':misses,'active':misses<2}
            atomic_write_json(storage,{**state,'visible':episode})
        return None
    if episode.get('active') and episode.get('key')==key:
        return None
    # A native question hook may already have alerted; avoid double alerts
    # when the same permission card becomes visible immediately afterward.
    last=state.get('last_hook_at')
    if isinstance(last,(int,float)) and 0<=now-last<20:
        atomic_write_json(storage,{**state,'visible':{'key':key,'active':True,
                                      'misses':0,'at':now}})
        return None
    record={'at':now,'kind':'permission','channels':{}}
    result,record=_emit(root,config,'Antigravity permission prompt visible',
        'A permission or subagent approval card is visible in Antigravity. '
        'Open the terminal or Remote Control and answer it; no approval was given.',
        record,now,sender=sender)
    atomic_write_json(storage,{**state,'visible':{'key':key,'active':True,
                               'misses':0,'at':now,'channels':record['channels']}})
    return 'GEMINI VISIBLE APPROVAL: '+str(result) if result else None


def handle(root, config, event, payload, *, active=blocks_auto_handoff,
           sender=send_event):
    if event=='PreToolUse':
        try:
            notify_question(root,config,payload,active=active,sender=sender)
        except Exception:
            pass
        # Crucial: "allow" delegates to the usual Antigravity permission
        # engine. NEVER return auto_approve, ask, deny or a changed argument.
        return {'decision':'allow'}
    if event=='PreInvocation':
        try:
            return inject_handoff_note(root,payload,active=active)
        except Exception:
            return {}
    return {}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--project',required=True,type=Path)
    parser.add_argument('--event',required=True,choices=('PreToolUse','PostToolUse','PreInvocation'))
    args=parser.parse_args()
    try:
        raw=sys.stdin.read(256*1024+1)
        if len(raw)>256*1024:
            raise ValueError('oversized hook input')
        payload=json.loads(raw)
        config=load_json(args.project/'.agent-watchdog'/'config.json',{}) or {}
        result=handle(args.project,config,args.event,payload)
    except Exception:
        result={'decision':'allow'} if args.event=='PreToolUse' else {}
    print(json.dumps(result))


if __name__=='__main__':
    main()
