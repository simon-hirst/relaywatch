"""Explicit, safe user-initiated continuation of an idle managed interactive agent.

The `resume` command is the ONLY caller; routine polling never types a turn.
The active TUI must be owned by this project, idle with an empty composer,
quota-eligible and not awaiting user input. Never blindly send Enter.
"""
from __future__ import annotations
import re
import subprocess
import time
import uuid
from pathlib import Path
from engine import atomic_write_json, current, load_json
from runner import latest_worker, session_name, tmux_exists
from pause_control import paused
from capacity import clean as strip_ansi, INPUT as codex_input
from alerts import request_path, pending_question, is_auto_stall, dismiss_auto_stall

MESSAGE = ('USER-REQUESTED RESUME: I explicitly ran the watchdog resume CLI for this project. '
           'That command cleared .agent-watchdog/PAUSE_AUTO_CONTINUE. Prior pause messages '
           'in this conversation are historical, not a current instruction. '
           'The UserPromptSubmit watchdog hook independently verifies this resume. '
           'Read docs/HANDOFF.md and the standing agent workflow; continue the next safe local task '
           'while quota permits. If you need a new user decision, stop and ask. '
           'Do not deploy, commit, push, or change live settings without permission.')

RESUME_CONTEXT = (
    'WATCHDOG RUNTIME CONFIRMATION — EXPLICIT USER RESUME VERIFIED. The user ran the '
    'watchdog resume CLI, which cleared .agent-watchdog/PAUSE_AUTO_CONTINUE. '
    'The pause latch is ABSENT at this UserPromptSubmit event. Earlier messages saying '
    '"wait/pause" or "still paused" are superseded for automation by this explicit '
    'user action. Do not ask the user to run resume again unless a NEW pause occurs. '
    'You may continue safe local tasks within verified quota; do not skip genuinely '
    'unanswered user decisions, commit, push, or deploy without authorization.'
)


def state_path(root):
    return Path(root)/'.agent-watchdog'/'resume-delivery.json'


def mark_received(root, agent, prompt):
    """Confirm only a *recent* resume created by the user's local CLI command."""
    if ' '.join(str(prompt or '').split()) != MESSAGE or paused(root):
        return False
    path=state_path(root)
    state=load_json(path,{}) or {}
    if (state.get('agent') != agent
            or state.get('status') not in ('typing','submitting','confirmed')
            or not isinstance(state.get('at'), (float, int))
            or not 0 <= time.time()-state['at'] < 180):
        return False
    atomic_write_json(path,{**state,'status':'confirmed','confirmation':'UserPromptSubmit','confirmed_at':time.time()})
    return True


def reports_stale_pause(message):
    """Agent's own refusal to work because it *believes* a historical pause remains."""
    if not isinstance(message, str):
        return False
    t=message.lower()
    # Do not interpret a task discussing/testing the pause feature as a refusal.
    return bool(re.search(
        r"(?m)^\s*(?:still paused\b|i(?:'m| am|’m) still paused\b|"
        r"i(?:'ve| have) stopped.{0,150}\b(?:paused|pause)\b|"
        r"(?:the )?watchdog (?:says|reports) (?:you(?:'ve| have)? )?paused\b|"
        r"automation (?:is|remains) paused\b|"
        r"i(?:'m| am|’m) not running.{0,100}\b(?:paused|pause)\b)",t))


def note_stale_pause(root, agent):
    """Log a refusal and leave the CLI idle; never spawn another Stop-hook turn."""
    path=state_path(root)
    state=load_json(path,{}) or {}
    if state.get('agent') == agent:
        atomic_write_json(path, {**state, 'status':'agent_still_thinks_paused',
                                 'refusal_at':time.time(),
                                 'note':'Pause latch is absent; autonomous Stop-hook retries suppressed.'})


# Claude Code does not keep its startup banner on screen after a long
# conversation. v3.7 required that banner and rejected valid idle composers.
# Verify the *live composer and its mode/status footer*, not old scrollback.
CLAUDE_EMPTY = re.compile(r'^\s*[❯›>]\s*(?:$|Try\s+[\"\'“‘].+)', re.I)
CLAUDE_MODE = re.compile(
    r'(?:bypass permissions|accept edits|plan mode|shift\s*\+\s*tab|'
    r'permissions on|permissions off|normal mode|default mode|Claude Code)', re.I)
CLAUDE_BUSY = re.compile(
    r'(?:esc|escape|ctrl\+c|ctrl-c)\s+to\s+interrupt|'
    r'(?<!\w)(?:thinking|working|generating|compacting)\s*(?:\.{2,}|…)|'
    r'\b(?:running in background|press esc to cancel)\b', re.I)
CLAUDE_FOOTER = re.compile(r'(?:^\s*$|^[─━═\-\s]+$|'
    r'\b(?:bypass permissions|accept edits|plan mode|shift\s*\+\s*tab|'
    r'permissions on|permissions off|install gh|PR status|\? for shortcuts|'
    r'tokens? remaining|context left|\d+% remaining)\b|'
    r'^\s*\[agent-handoff[^\n]*\]$)',re.I)


def idle_reason(agent, screen):
    """(is_idle, diagnostic); never rely solely on stale transcript prompts.

    Only tmux's currently visible pane should be supplied to this function.
    Project/session ownership and open question checks happen in resume_existing.
    """
    screen=strip_ansi(screen)
    lines=screen.splitlines()
    if agent=='claude':
        # The composer is at the *foot* of the current TUI. A historical
        # transcript line starting with ❯ farther up is not an idle prompt.
        candidates=[i for i,line in enumerate(lines) if CLAUDE_EMPTY.match(line)]
        if not candidates:
            if any(re.match(r'^\s*[❯›>]\s*\S',l) for l in lines[-14:]):
                return False,'Claude composer contains existing text'
            return False,'no recognisable empty Claude composer'
        i=candidates[-1]
        if len(lines)-i > 12:
            return False,'empty prompt found only in older terminal content'
        before='\n'.join(lines[max(0,i-4):i])
        after='\n'.join(lines[i+1:])
        if CLAUDE_BUSY.search(before) or CLAUDE_BUSY.search(after):
            return False,'Claude is showing active work or an interrupt control'
        if any(not CLAUDE_FOOTER.search(line) for line in lines[i+1:] if line.strip()):
            return False,'Claude has activity below its input composer'
        # A valid mode/footer is stronger than a banner that scrolls off after
        # one turn. A banner is still acceptable when its footer is hidden.
        if not (CLAUDE_MODE.search('\n'.join(lines[-15:])) or
                'Claude Code' in screen):
            return False,'Claude mode footer/identity not visible'
        if draft_visible(agent,screen):
            return False,'continuation draft is already in the composer'
        return True,'empty Claude composer and mode footer verified'
    # Codex has a persistent "Ask Codex" placeholder in its idle composer.
    tail='\n'.join(lines[-8:])
    if ('Codex' in screen or 'Ask Codex' in tail) and codex_input.search(tail):
        return True,'empty Codex composer verified'
    return False,'no recognisable empty Codex composer'


def idle(agent, screen):
    return idle_reason(agent,screen)[0]


def draft_visible(agent, screen):
    """Match the current typed draft next to the composer, not a past chat."""
    screen=strip_ansi(screen)
    lines=screen.splitlines()[-20:]
    prefix=MESSAGE[:39]
    suffix=MESSAGE[-50:]
    for i,line in enumerate(lines):
        # -J joins soft wraps; narrow terminals may retain a few hard wraps.
        if not re.match(r'^\s*[❯›>]\s*'+re.escape(prefix),line):
            continue
        tail=' '.join(' '.join(lines[i:i+9]).split())
        if suffix in tail and tail.index(prefix)<tail.index(suffix):
            if agent=='claude' or 'Codex' in screen or 'Ask Codex' in screen:
                return True
    return False

def resume_existing(root, config, *, run=subprocess.run, sleep=time.sleep, check_only=False):
    root=Path(root).resolve()
    if paused(root):return 'NOT RESUMED: user pause is still active.'
    worker=latest_worker(root)
    agent=worker.get('agent')
    if agent not in ('claude','codex') or worker.get('type')!='tmux' or worker.get('outcome')!='launched':
        return 'UNPAUSED: no watchdog-managed interactive agent is recorded. No prompt sent.'
    if worker.get('session')!=session_name(root) or not re.fullmatch(r'@\d+',str(worker.get('window',''))):
        return 'UNPAUSED: managed session identity could not be verified. No prompt sent.'
    question=pending_question(root)
    if question.get('agent') == agent:
        if is_auto_stall(question):
            if check_only:
                return (f'RESUMABLE STALE BLOCKER: {agent} has a watchdog-generated '
                        'no-progress alert from a previous auto-continuation. '
                        'An explicit resume will acknowledge it and inspect the live terminal. '
                        'No prompt sent.')
            dismiss_auto_stall(root,agent)
        else:
            return (f'UNPAUSED: {agent} has an unanswered user question/decision: '
                    f'{question.get("question", "(no details)")}. '
                    'Reply in the agent terminal. No automatic continuation sent.')
    # A question for the previous provider must not block the *current* worker.
    # Keep it in the status report; do not erase it as if answered.

    # Only a new trustworthy reading allows another turn. Do not guess quota.
    info=current(root,agent,config,max_age=0)
    if (info.get('stale') or info.get('stage')!='normal'
            or not isinstance(info.get('remaining_percent'),(int,float))
            or info['remaining_percent']<=config.get('prepare_remaining_pct',20)):
        return f'UNPAUSED: {agent} has no verified safe quota (>20% remaining required). No prompt sent.'
    if not tmux_exists(worker['session']):
        return 'UNPAUSED: managed tmux session has exited. No prompt sent.'
    window=worker['window']
    try:
        panes=run(['tmux','list-panes','-t',window,'-F','#{pane_id}'],capture_output=True,text=True,timeout=5)
        ids=(panes.stdout or '').splitlines() if panes.returncode==0 else []
        if len(ids)!=1 or not re.fullmatch(r'%\d+',ids[0].strip()):
            return 'UNPAUSED: cannot uniquely identify managed tmux pane. No prompt sent.'
        pane=ids[0].strip()

        def capture():
            cp=run(['tmux','capture-pane','-p','-J','-t',pane],capture_output=True,text=True,timeout=5)
            return cp.stdout if cp.returncode==0 else None
        first=capture()
        if not first:
            return f'UNPAUSED: cannot capture {agent} terminal. No prompt sent.'
        verified,reason=idle_reason(agent,first)
        if not verified:
            return f'UNPAUSED: {agent} is busy or its empty input cannot be verified ({reason}). No prompt sent.'
        sleep(0.6)
        second=capture()
        if not second:
            return f'UNPAUSED: cannot recapture {agent} terminal. No prompt sent.'
        verified,reason=idle_reason(agent,second)
        if not verified:
            return f'UNPAUSED: {agent} is busy or its empty input cannot be verified ({reason}). No prompt sent.'
        if first!=second:
            return f'UNPAUSED: {agent} terminal content changed between checks. No prompt sent.'
        if check_only:
            return f'READY TO RESUME {agent}: empty input, quota, and session identity verified. No prompt sent.'
        if paused(root):return 'NOT RESUMED: new user pause detected. No prompt sent.'
        # A user may answer the pending question while we inspect the TUI.
        latest_question=pending_question(root)
        if latest_question.get('agent')==agent:
            return ('UNPAUSED: agent now awaits user input: '
                    f'{latest_question.get("question","(no details)")}. No prompt sent.')
        token=uuid.uuid4().hex
        path=state_path(root)
        atomic_write_json(path,{'agent':agent,'token':token,'status':'typing','pane':pane,'at':time.time()})
        sent=run(['tmux','send-keys','-t',pane,'-l','--',MESSAGE],capture_output=True,text=True,timeout=5)
        if sent.returncode:
            return 'UNPAUSED: tmux rejected continuation text. Check the agent terminal.'
        sleep(0.9)  # Let bracketed paste settle before a separate Enter.
        third=capture()
        if not third or not draft_visible(agent,third):
            return 'UNPAUSED: could not verify continuation draft. Check the agent terminal; no Enter sent.'
        atomic_write_json(path,{**load_json(path,{}),'status':'submitting'})
        for attempt in range(3):
            if paused(root):return 'NOT RESUMED: pause reactivated during submission.'
            state=load_json(path,{}) or {}
            if state.get('status')=='confirmed':
                return f'RESUMED {agent}: continuation confirmed by UserPromptSubmit hook.'
            if attempt:
                screen=capture()
                if not screen or not draft_visible(agent,screen):break
            press=run(['tmux','send-keys','-t',pane,'Enter'],capture_output=True,text=True,timeout=5)
            if press.returncode:return 'UNPAUSED: tmux rejected Enter; continuation may remain in the composer.'
            sleep(1.0)
            if (load_json(path,{}) or {}).get('status')=='confirmed':
                return f'RESUMED {agent}: continuation confirmed by UserPromptSubmit hook.'
        # A CLI may not have the latest prompt hook loaded. Do not claim that
        # send-keys proves delivery and never retry without visible exact draft.
        state=load_json(path,{}) or {}
        if state.get('status')=='confirmed':return f'RESUMED {agent}: continuation confirmed by UserPromptSubmit hook.'
        screen=capture()
        status='unconfirmed'
        atomic_write_json(path,{**state,'status':status})
        if screen and draft_visible(agent,screen):
            return f'UNPAUSED: continuation is still typed in {agent}; press Enter manually. No duplicate was sent.'
        return f'UNPAUSED: continuation sent to {agent}, but submission was not confirmed by its hook. Check the terminal.'
    except (OSError,subprocess.TimeoutExpired) as exc:
        return f'UNPAUSED: tmux interaction failed ({type(exc).__name__}: {exc}); check the agent terminal.'
