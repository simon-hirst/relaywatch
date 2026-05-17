"""Bounded recovery for Codex 'Selected model is at capacity' TUI failures.

Only targets a watchdog-owned tmux window, *never* arbitrary user terminals.
A retry requires a stable failed screen AND an identifiable idle input prompt.
Does not change the selected model or compromise a running task.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import time
from pathlib import Path

from engine import atomic_write_json, load_json
from alerts import register_question, request_path
from runner import latest_worker, session_name, tmux_exists

ERROR = re.compile(r'selected model is at capacity\s*\.?\s*please try a different model', re.I)
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
# Codex TUI prompt variants. Require an actual input area, not merely an error.
INPUT = re.compile(r'(?mi)^\s*(?:›|❯|>)\s*(?:Ask Codex(?: to do anything)?|Type a message|$)')
CONTINUED = re.compile(r'(?mi)^\s*(?:›|❯|>)\s*(?:continue|resume|read docs/|next task)\b')


def state_path(root): return Path(root)/'.agent-watchdog'/'capacity-recovery.json'


def clean(text):
    return ANSI.sub('',text or '').replace('\r','')


def candidate(text):
    """Return true only for the latest visible failure with the Codex input idle.

    Do not retry old errors that are followed by a new user prompt or task output.
    Require the error near the bottom (the TUI may retain older transcript lines).
    """
    text=clean(text)
    found=list(ERROR.finditer(text))
    if not found:return False
    last=found[-1]
    following=text[last.end():]
    if len(following)>1300 or len(following.splitlines())>25:return False
    # If the user has already requested continuation, give it time to execute.
    if CONTINUED.search(following):return False
    # Only press Enter into a provably blank prompt in the visible UI.
    # Requiring the prompt after the failure prevents sending to an ongoing run.
    return bool(INPUT.search(following))


def snapshot(window,*,run=subprocess.run):
    cp=run(['tmux','capture-pane','-p','-J','-t',window,'-S','-180'],
           capture_output=True,text=True,timeout=6)
    if cp.returncode:
        return None
    return clean(cp.stdout)


def record_manual_turn(root,agent,payload=None):
    """Manual input wins; don't mistake OUR injected retry for a manual turn."""
    if agent!='codex':return
    path=state_path(root)
    state=load_json(path,{}) or {}
    payload=payload if isinstance(payload,dict) else {}
    content=str(payload.get('prompt') or payload.get('user_prompt') or '')
    if 'WATCHDOG_CAPACITY_RETRY' in content:
        return
    # Some Codex versions omit prompt text in UserPromptSubmit hooks. A
    # short interval around OUR guarded injection is not proof of manual input.
    if not content and time.time()-state.get('sending_at',0)<30:
        return
    state.update({'suppressed_until':time.time()+120,
                  'attempts':0,'alerted':False,'observed_at':None,
                  'screen_digest':None,'last_activity':'manual input'})
    atomic_write_json(path,state)


def record_tool_progress(root,agent):
    """A real successful tool invocation proves the previous failed turn recovered."""
    if agent!='codex':return
    path=state_path(root)
    state=load_json(path,{}) or {}
    if state.get('attempts') or state.get('alerted'):
        state.update({'attempts':0,'alerted':False,'observed_at':None,
                      'screen_digest':None,'last_activity':'real tool progress'})
        atomic_write_json(path,state)


def check(root, config, usages, *,now=None,run=subprocess.run):
    opts=config.get('capacity_recovery',{})
    if not opts.get('enabled',True):return None
    root=Path(root)
    worker=latest_worker(root)
    if worker.get('agent')!='codex' or worker.get('type')!='tmux' or worker.get('outcome')!='launched':return None
    window=worker.get('window'); session=worker.get('session')
    if not isinstance(window,str) or not re.fullmatch(r'@[0-9]+',window):return None
    if session!=session_name(root):return None
    info=usages.get('codex') or {}
    if info.get('stage') not in ('normal','prepare') or info.get('stale'):return None
    if (root/'.agent-watchdog'/'PAUSE_AUTO_CONTINUE').exists():return None
    if not tmux_exists(session,invoke=run):return None
    pending=load_json(request_path(root),{}) or {}
    if pending.get('status')=='open':return None

    now=time.time() if now is None else now
    key=str(worker.get('event_id') or worker.get('started_at'))+':'+window
    path=state_path(root)
    state=load_json(path,{}) or {}
    if state.get('worker_key')!=key:
        state={'worker_key':key,'attempts':0,'observed_at':None,'last_retry_at':0,
               'suppressed_until':0,'last_activity':'new worker'}
    if now<state.get('suppressed_until',0):return None
    try:screen=snapshot(window,run=run)
    except (OSError,subprocess.TimeoutExpired):return None
    if screen is None:return None
    if not candidate(screen):
        # Keep attempt count for subsequent repeated server failures in this worker.
        if state.get('observed_at') is not None:
            state.update({'observed_at':None,'screen_digest':None,'last_activity':'no idle capacity error'})
            atomic_write_json(path,state)
        return None

    # Screen must be unchanged through two polls before interacting with it.
    digest=hashlib.sha256(screen.encode('utf-8',errors='replace')).hexdigest()
    if state.get('screen_digest')!=digest:
        state.update({'screen_digest':digest,'observed_at':now,'last_activity':'observing capacity error'})
        atomic_write_json(path,state)
        return 'Codex capacity error detected; checking that its input is idle before retry'
    stable=max(10,int(opts.get('stable_seconds',20)))
    if now-state.get('observed_at',now)<stable:return None
    max_retries=max(0,min(5,int(opts.get('max_retries',2))))
    if state['attempts']>=max_retries:
        if not state.get('alerted'):
            register_question(root,'codex',
                'Codex is repeatedly reporting that its selected model is at capacity. '
                'The watchdog exhausted safe retries. Check the Codex terminal; try a different model '
                'or ask it to continue again when the capacity issue clears.')
            state.update({'alerted':True,'last_activity':'retry budget exhausted'})
            atomic_write_json(path,state)
            return f'Codex model capacity: {max_retries} retries exhausted; desktop/phone alert requested'
        return None
    delays=opts.get('delays_seconds',[30,90])
    if not isinstance(delays,list) or not all(isinstance(x,(int,float)) for x in delays):delays=[30,90]
    wait=float(delays[min(state['attempts'],len(delays)-1)]) if delays else 30
    if now-state.get('last_retry_at',0)<max(15,wait):return None
    # Re-read immediately before injecting to guard against new activity during
    # processing. Refuse to send into a changed screen or changed tmux window.
    if not tmux_exists(session,invoke=run):return None
    try:confirmed=snapshot(window,run=run)
    except (OSError,subprocess.TimeoutExpired):return None
    if confirmed!=screen or not candidate(confirmed):return None
    message=('[WATCHDOG_CAPACITY_RETRY] Continue the existing task from where the capacity error interrupted you. '
             'Read docs/HANDOFF.md if needed; do not redo completed steps. '
             'Keep working through the next locally actionable tasks; do not deploy, commit, or push.')
    # Record injection intent BEFORE typing because the UserPromptSubmit hook
    # may fire immediately when Enter is delivered. This prevents an auto-retry
    # from incorrectly resetting its own bounded retry counter.
    state['sending_at']=time.time()
    atomic_write_json(path,state)
    try:
        sent=run(['tmux','send-keys','-t',window,'-l','--',message],
                 capture_output=True,text=True,timeout=5)
        if sent.returncode:return f'Capacity auto-retry unable to type into tmux: {str(sent.stderr)[:160]}'
        enter=run(['tmux','send-keys','-t',window,'Enter'],
                  capture_output=True,text=True,timeout=5)
        if enter.returncode:return f'Capacity auto-retry unable to submit: {str(enter.stderr)[:160]}'
    except (OSError,subprocess.TimeoutExpired) as exc:
        return f'Capacity auto-retry failed: {type(exc).__name__}: {exc}'
    state.update({'attempts':state['attempts']+1,'last_retry_at':now,
                  'observed_at':None,'screen_digest':None,'last_activity':'automated continue sent'})
    atomic_write_json(path,state)
    return f'Codex capacity: safe automatic continue sent (attempt {state["attempts"]}/{max_retries})'
