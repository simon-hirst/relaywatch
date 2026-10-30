"""Verify and, only when demonstrably idle, recover Argon's initial handoff.

A tmux window/agy process is NOT proof that the model received its first
task. An Antigravity PreInvocation hook is positive evidence. After a grace
period, a one-shot fallback sends the short handoff command ONLY when the
exact managed Gemini pane contains a recognised empty composer, no user
question/approval, and the process identity has been verified. When any
safety precondition is missing it alerts the user instead of typing blindly.
No retries of an attempted send, no background/headless agent, no approvals.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from alerts import send_event
from engine import atomic_write_json, load_json
from pause_control import paused
from runner import latest_worker, session_name, launch_lock, wd
from gemini_session import blocks_auto_handoff
from session_recovery import detect_managed_agent

WAIT_BEFORE_RESCUE = 35
ALERT_AFTER_SECONDS = 75
STARTUP_COMMAND = (
    'RelayWatch automatic handoff: Read AGENTS.md, CLAUDE.md, '
    'docs/WORKFLOW.md and docs/HANDOFF.md. Verify the repository and '
    'give a concise takeover briefing, then CONTINUE WITH NEXT TASK: '
    'implement and test the highest-priority safe unfinished local task. '
    'Keep work moving, honor permissions and user pauses, keep the handoff '
    'current, and prepare a final handoff at 10% Argon weekly quota remaining.'
)

# Agy normally replaces these placeholders as soon as a user starts typing.
# A generic empty prompt arrow is NOT enough to authorize programmatic input.
_EMPTY_COMPOSER = re.compile(
    r'(?i)\b(?:type (?:your |a )?(?:message|prompt)|'
    r'ask (?:anything|a question)|enter (?:a |your )?(?:message|prompt)|'
    r'send (?:a |your )?message)\b'
)
_DANGER_SCREEN = re.compile(
    r'(?i)\b(?:permission (?:required|request|needed)|'
    r'approval (?:required|needed)|requires your approval|'
    r'login required|sign in required|choose (?:your |an? )?account|'
    r'select (?:your |an? )?account|authentication required|'
    r'working(?:\.\.\.)?|thinking(?:\.\.\.)?|processing|generating|'
    r'using tool|running (?:a |the )?tool|'
    r'press (?:enter|y) to (?:approve|authorize)|'
    r'allow once|always allow|deny|reject)\b'
)


def receipt_path(root):
    return wd(root)/'gemini-startup.json'


def start(root, *, event_id, session, window, now=None):
    """Mark handoff window as started, not prompt-delivered."""
    if not re.fullmatch(r'@\d+',str(window)):
        raise ValueError('invalid Gemini window')
    when=time.time() if now is None else now
    atomic_write_json(receipt_path(root),{
        'event_id':event_id,'session':session,'window':window,
        'started_at':when,'state':'awaiting_first_invocation',
        'delivery':'agy interactive positional prompt','attempted':False})
    return when


def gate(root,event_id,*,timeout=20):
    """Ensure worker/receipt ownership exists before launching the Agy CLI.

    This removes the race where PreInvocation fires before RelayWatch has
    written worker.json, causing a genuine first turn to go unacknowledged.
    """
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        state=load_json(receipt_path(root),{}) or {}
        worker=latest_worker(root)
        if (state.get('event_id')==event_id
                and worker.get('event_id')==event_id
                and worker.get('agent')=='gemini'
                and worker.get('window')==state.get('window')
                and worker.get('session')==state.get('session')):
            return True
        time.sleep(0.1)
    return False


def main():
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=('gate',))
    parser.add_argument('--project',type=Path,required=True)
    parser.add_argument('--event-id',required=True)
    args=parser.parse_args()
    if args.command=='gate':
        raise SystemExit(0 if gate(args.project,args.event_id) else 2)


def _matches(root,payload):
    if not isinstance(payload,dict):
        return False
    paths=payload.get('workspacePaths')
    if not isinstance(paths,list):
        return False
    try:
        target=Path(root).resolve()
        return any(isinstance(p,str) and Path(p).resolve()==target for p in paths)
    except (OSError,ValueError):
        return False


def hook(root,event,payload,*,now=None):
    """SessionStart means CLI loaded; PreInvocation means *model started*.

    Do not claim a successful prompt from SessionStart or a tmux process.
    """
    if event not in ('SessionStart','PreInvocation') or not _matches(root,payload):
        return False
    if (event=='PreInvocation' and
            not str(payload.get('modelName','')).startswith('gemini-4-argon-')):
        return False
    root=Path(root)
    worker=latest_worker(root)
    state=load_json(receipt_path(root),{}) or {}
    if (worker.get('agent')!='gemini' or worker.get('session')!=session_name(root)
            or worker.get('event_id')!=state.get('event_id')
            or worker.get('window')!=state.get('window')
            or state.get('state') in ('confirmed','failed')):
        return False
    now=time.time() if now is None else now
    if event=='SessionStart':
        atomic_write_json(receipt_path(root),{**state,'session_seen_at':now})
        return True
    atomic_write_json(receipt_path(root),{**state,'state':'confirmed',
                                         'model_invocation_at':now,
                                         'conversation_id':str(payload.get('conversationId',''))[:100],
                                         'invocation_num':payload.get('invocationNum')})
    return True


def empty_composer(screen):
    """Recognise an explicitly empty composer, never arbitrary shell/UI text."""
    if not isinstance(screen,str):
        return False
    from capacity import clean
    lines=[line.strip() for line in clean(screen).splitlines() if line.strip()][-10:]
    if not lines:
        return False
    text='\n'.join(lines)
    if _DANGER_SCREEN.search(text):
        return False
    # Some UIs show the empty placeholder plus input hints. A prompt already
    # containing the handoff is NOT empty even if the placeholder is elsewhere.
    if 'RelayWatch automatic handoff' in text or 'AUTOMATIC RELAYWATCH QUOTA HANDOFF' in text:
        return False
    return any(_EMPTY_COMPOSER.search(line) for line in lines[-5:])


def _capture(window, *, run=subprocess.run):
    panes=run(['tmux','list-panes','-t',window,'-F','#{pane_id}'],
              capture_output=True,text=True,timeout=5)
    ids=(panes.stdout or '').splitlines() if panes.returncode==0 else []
    if len(ids)!=1 or not re.fullmatch(r'%\d+',ids[0].strip()):
        return None
    screen=run(['tmux','capture-pane','-p','-J','-t',ids[0].strip()],
               capture_output=True,text=True,timeout=5)
    if screen.returncode:
        return None
    return screen.stdout


def _notify(root,config,state,*,sender=send_event):
    if state.get('alerted'):
        return state
    try:
        result=sender(root,config,'Antigravity initial handoff needs attention',
                  'Gemini opened, but RelayWatch could not verify its first model turn. '
                  'Open the managed Antigravity terminal or Remote Control, check '
                  'whether the handoff was already submitted, and if idle send: '
                  'Read docs/HANDOFF.md and continue with the next safe task.',
                  'Gemini opened but its first task is unverified. Check the terminal '
                  'or Antigravity Remote Control. If idle, send: '
                  'Read docs/HANDOFF.md and continue with the next safe task.')
    except Exception:
        result={'desktop':'failed','phone':'failed'}
    return {**state,'alerted':True,
            'alert_delivery':{k:v for k,v in result.items() if k in ('desktop','phone')}}


def monitor(root,config, *, now=None, run=subprocess.run,
            detect=detect_managed_agent, sender=send_event,
            active=blocks_auto_handoff):
    """One-shot verified idle rescue; fail closed, never type during work.

    Serializes with launch_lock. A receipt is persisted BEFORE any TUI input
    so a watchdog restart cannot send it again.
    """
    root=Path(root).resolve()
    if paused(root):
        return None
    state=load_json(receipt_path(root),{}) or {}
    worker=latest_worker(root)
    if (worker.get('agent')!='gemini'
            or worker.get('session')!=session_name(root)
            or not worker.get('event_id') or not worker.get('window')
            or not active(root)):
        return None
    if not state:
        # Adopt a v3.22.0 automatically launched Gemini without disturbing
        # its current terminal. Only an unmistakably empty composer can be
        # rescued; any working/ambiguous session triggers a user alert.
        start(root,event_id=worker['event_id'],session=worker['session'],
              window=worker['window'],now=worker.get('started_at'))
        state=load_json(receipt_path(root),{}) or {}
        atomic_write_json(receipt_path(root),{**state,'adopted_running_session':True})
        state=load_json(receipt_path(root),{}) or {}
    if (state.get('state')=='confirmed' or state.get('alerted')
            or worker.get('event_id')!=state.get('event_id')
            or worker.get('window')!=state.get('window')):
        return None
    when=time.time() if now is None else now
    age=when-state.get('started_at',when)
    if age < WAIT_BEFORE_RESCUE:
        return None
    # Only NEW sessions with a managed startup receipt can be rescued
    # automatically. An already-running v3.22 TUI might have processed
    # an earlier prompt before this version installed its hook, and its
    # empty composer is not proof that the conversation is new.
    # If adopted, alert the user instead of risking duplicate work.
    # Mark one attempted rescue only if verified process and explicit idle
    # composer. Otherwise leave the UI untouched and alert after grace.
    if (not state.get('attempted') and not state.get('alerted')
            and not state.get('adopted_running_session')
            and age>=WAIT_BEFORE_RESCUE):
        with launch_lock(root):
            state=load_json(receipt_path(root),{}) or {}
            worker=latest_worker(root)
            if (state.get('state')=='confirmed' or state.get('attempted')
                    or state.get('alerted')
                    or worker.get('event_id')!=state.get('event_id')
                    or worker.get('window')!=state.get('window')
                    or not active(root)):
                return None
            window=state['window']
            try:
                evidence,_reason=detect(root,expected_window=window)
                verified=(evidence is not None
                          and evidence.get('agent')=='gemini'
                          and evidence.get('session')==session_name(root)
                          and evidence.get('window')==window)
                screen=_capture(window,run=run) if verified else None
                if verified and empty_composer(screen):
                    # Commit intent before typing. Two watcher instances or
                    # restarts must NEVER send a second initial task.
                    atomic_write_json(receipt_path(root),{
                        **state,'attempted':True,
                        'attempted_at':when,'delivery':'tmux one-shot idle rescue',
                        'state':'awaiting_first_invocation'})
                    # Literal keys, not shell interpolation; then submit.
                    typed=run(['tmux','send-keys','-l','-t',window,STARTUP_COMMAND],
                              capture_output=True,text=True,timeout=7)
                    if typed.returncode:
                        raise RuntimeError('tmux literal handoff send failed')
                    submit=run(['tmux','send-keys','-t',window,'Enter'],
                               capture_output=True,text=True,timeout=7)
                    if submit.returncode:
                        raise RuntimeError('tmux handoff submission failed')
                    return 'GEMINI INITIAL HANDOFF SUBMITTED: one-shot verified idle rescue; awaiting model invocation'
            except (OSError,RuntimeError,subprocess.TimeoutExpired):
                # Already-attempted state prevents duplicate submissions.
                pass
    if age>=ALERT_AFTER_SECONDS:
        with launch_lock(root):
            state=load_json(receipt_path(root),{}) or {}
            if state.get('state')=='confirmed' or state.get('alerted'):
                return None
            state=_notify(root,config,state,sender=sender)
            atomic_write_json(receipt_path(root),state)
        return 'GEMINI INITIAL HANDOFF UNVERIFIED: notified desktop/phone; no unsafe duplicate sent'
    return None


if __name__=='__main__':
    main()
