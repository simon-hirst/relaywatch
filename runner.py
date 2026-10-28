"""Automatic interactive agent handoff with persistent tmux + optional visible Windows Terminal.

A Stop hook acknowledges the finished handoff; the polling watchdog starts the
other provider in a dedicated tmux pty. The user attaches from VS Code; the
CLI remains interactive and remote-control enabled. Auto-opens Windows Terminal
when no tmux client is already attached. No headless fallback.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from engine import atomic_write_json, load_json
from pause_control import paused

START_PROMPT = (
    'AUTOMATIC HANDOFF CONTINUATION. First read AGENTS.md / CLAUDE.md, '
    'docs/WORKFLOW.md and docs/HANDOFF.md. Verify Git status, '
    'handoff details and actual files. BEFORE doing implementation, tell the user '
    'in the interactive session a concise takeover briefing: (1) overall project/phase, '
    '(2) completed milestones with verification status, (3) current/in-progress task, '
    '(4) next two or three priorities, (5) blockers or decisions requiring approval. '
    'If the handoff lists completed work but no concrete next task, first audit the local evidence ' +
    'and rewrite docs/HANDOFF.md with a prioritized list: file paths, exact verification commands, ' +
    'acceptance criteria, and which actions require explicit user approval. Never invent progress. ' +
    'State the exact next safe task, then say CONTINUE WITH NEXT TASK and implement and test '
    'the highest priority unfinished LOCAL task without requiring another user prompt. '
    'After completing that task, CONTINUE AUTOMATICALLY to the next actionable local task. '
    'While usage is below 90% used, DO NOT voluntarily wait for a quota reset if safe tasks remain. '
    'At 80-89% used, prepare your handoff while completing short verifiable tasks; do not start huge jobs. '
    'Do not deliberately burn tokens to reach 90%, and do not ignore real user pauses or blockers. '
    'The watchdog, not the agent, handles waiting for quota recovery after the final handoff. '
    'Keep working through successive tasks while quota permits. Do not end the turn merely '
    'because one milestone passes or because you updated the handoff. Stop when no more safe '
    'local tasks exist, user authorization is required, or the quota handoff threshold is reached. '
    'Do not deploy, push, commit, alter live settings, or execute '
    'external/destructive operations without explicit user permission. '
    'At handoff, write a dated session-specific report under docs/session-reports/ with completed work, verification and next steps. '
    'Keep the shared handoff up to date, follow native usage hooks, and '
    'stop for a verified handoff at 90% usage. If you genuinely need a user answer '
    'and cannot make safe progress on another local task, write a structured blocker '
    'to .agent-watchdog/needs-input.json with keys agent, question, status=open; '
    'end your response with NEEDS_USER_INPUT: followed by the exact question, then wait. '
    'For ANY other approval or permission question, even with safe local tasks left, '
    'emit USER_DECISION_REQUEST: followed by the exact question. Prefer formal CLI '
    'question UI if available. The watchdog will notify the user and prevent '
    'Stop-hook auto-continuation over that pending decision. Never guess a user decision. '
    'If you must stop solely to await a running background watch/test/job, and '
    'the coding CLI is expected to notify on completion, write a final status '
    'WAITING_ON_BACKGROUND: <job name and expected completion signal>. Once the job truly completes, emit BACKGROUND_JOB_COMPLETE: <job and verified result>. This is '
    'NOT a question and must not use NEEDS_USER_INPUT. Do not waste quota '
    'polling or request permission merely because a background task is running. '
    'Keep any separate live-action user approvals pending and do not infer approval. '
    'If current usage is already '
    'at/above the threshold, write the handoff instead of implementing.'
)


def wd(root): return Path(root)/'.agent-watchdog'
def ready_path(root,source): return wd(root)/f'ready-{source}.json'

def session_name(root):
    # Stable, valid and unique per checkout; can be attached from VS Code.
    code=hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:10]
    return f'agent-handoff-{code}'


def mark_ready(root,source,session_id,handoff):
    file=Path(handoff)
    if not file.is_file():return None
    st=file.stat()
    digest=hashlib.sha256(f'{source}\0{session_id}\0{st.st_mtime_ns}\0{st.st_size}'.encode()).hexdigest()[:24]
    event={'event_id':digest,'source':source,'target':'codex' if source=='claude' else 'claude',
           'session_id':str(session_id or ''),'handoff_mtime_ns':st.st_mtime_ns,
           'created_at':time.time()}
    old=load_json(ready_path(root,source),{}) or {}
    # When the explicit acknowledgement is followed by the native Stop hook,
    # they refer to one handoff. Keep the original event ID so only one new
    # worker may ever be launched for that handoff.
    if (old.get('source')==source and old.get('handoff_mtime_ns')==st.st_mtime_ns
            and old.get('event_id')):
        return old
    atomic_write_json(ready_path(root,source),event)
    return event

@contextmanager
def launch_lock(root):
    path=wd(root)/'launch.lock';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as f:
        fcntl.flock(f.fileno(),fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(f.fileno(),fcntl.LOCK_UN)


def pid_running(pid):
    if not isinstance(pid,int) or pid<=0:return False
    try:
        stat=Path(f'/proc/{pid}/stat')
        if stat.exists() and stat.read_text().rsplit(')',1)[-1].strip().startswith('Z'):return False
        os.kill(pid,0);return True
    except (OSError,ValueError):return False


def tmux_exists(session, *, invoke=None):
    fn=invoke or subprocess.run
    try:return fn(['tmux','has-session','-t',f'={session}'],stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL,timeout=5).returncode==0
    except (OSError,subprocess.TimeoutExpired):return False


def wsl_host():
    """True only when running in WSL with Windows executable interop."""
    return 'microsoft' in os.uname().release.lower() or bool(os.environ.get('WSL_DISTRO_NAME'))


def show_agent_window(root, config, agent=None, *, force=False, invoke=None):
    """Attach a visible *new Windows Terminal* to the existing tmux session.

    This does not launch a second agent. Auto mode skips opening a duplicate
    when VS Code or another terminal is already attached. Manual 'show' mode
    always opens a new window. Return a diagnostic rather than losing the agent
    if Windows Terminal, WSL interop or GUI startup is unavailable.
    """
    root=Path(root).resolve()
    session=session_name(root)
    if not tmux_exists(session,invoke=invoke):
        return f'window not opened: tmux session {session} does not exist'
    if not force and not config.get('auto_open_terminal_window',True):
        return 'automatic terminal opening disabled; use -Command show to open manually'
    if not force:
        run=invoke or subprocess.run
        try:
            clients=run(['tmux','list-clients','-t',f'={session}','-F','#{client_tty}'],
                        capture_output=True,text=True,timeout=5)
            if clients.returncode==0 and (clients.stdout or '').strip():
                return 'existing terminal already attached; it will follow the next tmux window'
        except (OSError,subprocess.TimeoutExpired) as exc:
            return f'window not opened: could not verify attached clients ({exc})'
    if not wsl_host():
        script = wd(root) / 'open-agent-window.sh'
        if not script.is_file():
            return 'window not opened: native launcher missing; attach using tmux attach -t ' + session
        agent = agent or latest_worker(root).get('agent','codex')
        runner = invoke or subprocess.run
        try:
            cp = runner(['bash',str(script),session,agent],capture_output=True,text=True,timeout=15)
            if cp.returncode:
                return 'window not opened: ' + (cp.stderr or cp.stdout or 'native terminal unavailable')[:250]
        except (OSError,subprocess.TimeoutExpired) as exc:
            return f'window not opened: native terminal unavailable ({type(exc).__name__}: {exc})'
        return f'terminal opened for {agent}; the tmux session remains attached'
    script=wd(root)/'open-agent-window.ps1'
    if not script.is_file():return 'window not opened: open-agent-window.ps1 missing; reinstall v1.5'
    agent=agent or latest_worker(root).get('agent','codex')
    if agent not in ('claude','codex'):
        return 'window not opened: cannot determine active agent'
    run=invoke or subprocess.run
    try:
        winpath=run(['wslpath','-w',str(script)],capture_output=True,text=True,timeout=5)
        if winpath.returncode: return f'window not opened: wslpath failed: {(winpath.stderr or "")[:180]}'
        script_windows=winpath.stdout.strip()
        if not script_windows:return 'window not opened: wslpath returned empty path'
        distro=config.get('wsl_distro','Debian')
        cp=run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',
                script_windows,'-Session',session,'-Agent',agent,'-Distro',distro],
               capture_output=True,text=True,timeout=15)
        if cp.returncode:
            return f'window not opened: Windows Terminal launch failed: {(cp.stderr or cp.stdout)[:250]}'
    except (OSError,subprocess.TimeoutExpired) as exc:
        return f'window not opened: Windows interop failed: {type(exc).__name__}: {exc}'
    return f'Windows Terminal opened for {agent}; closing it will not stop the agent'


def handoff_is_real(root):
    file=Path(root)/'docs'/'HANDOFF.md'
    if not file.is_file() or file.stat().st_size<180:return False
    return 'Bootstrap placeholder; not a verified implementation handoff' not in file.read_text(encoding='utf-8',errors='replace')


def latest_worker(root):return load_json(wd(root)/'worker.json',{}) or {}


def launch(root,config,event,usage,*,allow_existing_event=False,invoke=None):
    """Start a visible interactive tmux TUI, never codex exec or claude -p.

    Under the same lock, permit replacement of a *watchdog-owned* prior tmux
    session ONLY for a matching verified source Stop-hook event. Otherwise
    refuse to terminate or hijack existing sessions.
    """
    root=Path(root).resolve();target=event.get('target')
    if paused(root):return 'not launched: paused explicitly by the user'
    if target not in ('claude','codex'):return 'not launched: invalid receiving agent'
    if not handoff_is_real(root):return 'not launched: handoff is absent or still installer placeholder'
    if not isinstance(usage,dict) or usage.get('agent')!=target or usage.get('stale') or usage.get('stage') in ('unknown','handoff','prepare'):
        return f'not launched: {target} quota unavailable or insufficient (>20% remaining required)'
    if not event.get('event_id'):return 'not launched: invalid handoff event'
    with launch_lock(root):
        history=load_json(wd(root)/'launch-history.json',{}) or {}
        if event['event_id'] in history and not allow_existing_event:
            return 'not launched: this handoff event has already been handled'
        session=session_name(root);prior=latest_worker(root)
        fn=invoke or subprocess.run
        if not shutil.which('tmux') and invoke is None:
            return 'not launched: tmux is missing (install tmux using your OS package manager)'
        exists=tmux_exists(session,invoke=fn)
        if exists:
            # Do not seize unknown tmux sessions. Only switch when a *source*
            # agent's verified Stop hook has produced a handoff event.
            authorized_switch=(prior.get('type')=='tmux' and prior.get('session')==session
                               and prior.get('agent')==event.get('source')
                               and bool(prior.get('window'))
                               and event.get('verified_stop') is True)
            if not authorized_switch:
                return (f'not launched: interactive session {session} already exists; '
                        'attach to it or close it manually before retrying')
        prompt_file=wd(root)/'next-agent-prompt.txt'
        prompt_file.write_text(START_PROMPT+'\n',encoding='utf-8')
        script=wd(root)/'interactive-agent.sh'
        if not script.is_file():return 'not launched: interactive-agent.sh missing; reinstall v1.4'
        if b'\r\n' in script.read_bytes():
            return 'FAILED launcher preflight: CRLF detected in interactive-agent.sh; reinstall RelayWatch to restore Unix LF endings'
        # Fail before reserving a tmux window if Git/Windows checkout converted
        # the managed Bash entrypoint to CRLF (or if any syntax error exists).
        # A syntactically broken launcher must never be reported as started.
        try:
            checked=fn(['bash','-n',str(script)],capture_output=True,text=True,timeout=6)
        except (OSError,subprocess.TimeoutExpired) as exc:
            return f'FAILED launcher preflight: {type(exc).__name__}: {exc}'
        if checked.returncode:
            return 'FAILED launcher preflight (bash -n): '+str(checked.stderr or checked.stdout or 'invalid Bash syntax')[:300]
        # Bash login environment loads NVM/tool shims; the *outer* bash must
        # survive a fast child exit, so the tmux window remains visible for
        # diagnosis instead of disappearing before we can attach to it.
        import shlex
        inner='bash '+shlex.join([str(script),target,str(root),str(prompt_file)])
        shell_command=(
            f'{inner}; rc=$?; '
            'printf "\\n[watchdog] Agent exited (status %s). Terminal retained for diagnostics.\\n" "$rc"; '
            'exec bash -i'
        )
        if exists:
            # tmux -P prints the new window ID atomically. Never query a
            # potentially already-exited new window in a separate command.
            argv=['tmux','new-window','-d','-P','-F','#{window_id}',
                  '-t',f'={session}:','-n',target,'-c',str(root),'bash','-lic',shell_command]
        else:
            argv=['tmux','new-session','-d','-P','-F','#{window_id}',
                  '-s',session,'-n',target,'-c',str(root),'bash','-lic',shell_command]
        try:
            cp=fn(argv,capture_output=True,text=True,timeout=12)
            if cp.returncode!=0:
                return f'FAILED to start {target} interactive tmux: {(cp.stderr or cp.stdout)[:300]}'
            window=(cp.stdout or '').strip()
            if not window.startswith('@'):
                # Even if tmux somehow did not print the window ID, never
                # claim success; preserve the session for inspection.
                return (f'FAILED: tmux did not return new window ID ({window!r}). '
                        f'Inspect: tmux list-windows -t {session}')
            if exists:
                select=fn(['tmux','select-window','-t',window],capture_output=True,text=True,timeout=6)
                if select.returncode!=0:
                    return f'FAILED to select receiving tmux window: {select.stderr[:200]}'
                # Previous source completed a verified handoff. Only retire
                # its watchdog-owned window, not any unrelated tmux session.
                old=fn(['tmux','kill-window','-t',prior['window']],capture_output=True,text=True,timeout=6)
                if old.returncode!=0:
                    return f'FAILED to retire prior completed tmux window {prior["window"]}: {old.stderr[:200]}'
        except (OSError,subprocess.TimeoutExpired) as exc:
            return f'FAILED to start {target} interactive tmux: {type(exc).__name__}: {exc}'
        worker={'type':'tmux','session':session,'agent':target,'event_id':event['event_id'],
                'source_agent':event.get('source'),'handoff_created_at':event.get('created_at'),
                'started_at':time.time(),'outcome':'launched','pid':None,'window':window}
        atomic_write_json(wd(root)/'worker.json',worker)
        history[event['event_id']]={'target':target,'outcome':'launched','timestamp':worker['started_at'],'session':session}
        atomic_write_json(wd(root)/'launch-history.json',history)
        # Leave source-provider decisions intact until the receiving CLI is
        # actually verified running (a newly opened tmux shell is not enough).
        visible=show_agent_window(root,config,target,invoke=invoke)
        return f'CREATED {target} interactive tmux window {window} in {session}; {visible}; verify CLI startup'


def can_resume(agent, info, min_remaining=20):
    """Require a live, *healthy* quota for the agent that would actually run.

    A missing, unrecognised or stale API response is never interpreted as a reset.
    """
    return (isinstance(info,dict) and info.get('agent')==agent
            and not info.get('stale') and info.get('stage')=='normal'
            and isinstance(info.get('remaining_percent'),(float,int))
            and info['remaining_percent']>min_remaining)


def check_worker_startup(root, *, now=None, detector=None):
    """Verify a real interactive provider process, not merely a tmux shell.

    Deliberately retain a failed terminal for diagnosis and never auto-retry an
    event: an error shell must not be mistaken for a second active coding agent.
    """
    root=Path(root).resolve()
    worker=latest_worker(root)
    if worker.get('type')!='tmux' or worker.get('outcome')!='launched':
        return None
    started=worker.get('started_at')
    if not isinstance(started,(int,float)):
        return None
    if worker.get('startup_status') in ('verified', 'unavailable'):
        return None
    now=time.time() if now is None else now
    # The current process inspector is Linux-/proc-based. macOS has no /proc:
    # do not mislabel a legitimate interactive session as a failed launch.
    # Keep its status explicitly unknown until platform-specific evidence exists.
    if not os.path.isdir('/proc/self'):
        atomic_write_json(wd(root)/'worker.json',
                          {**worker,'startup_status':'unavailable',
                           'startup_checked_at':now,
                           'startup_failure_reason':'/proc process inspection not supported on this host'})
        return 'AGENT STARTUP UNVERIFIED: process inspection unavailable; check interactive terminal manually'
    from session_recovery import detect_managed_agent
    evidence,reason=(detector or detect_managed_agent)(root)
    if (evidence and evidence.get('session')==worker.get('session')
            and evidence.get('window')==worker.get('window')
            and evidence.get('agent')==worker.get('agent')):
        atomic_write_json(wd(root)/'worker.json',
                          {**worker,'startup_status':'verified','startup_verified_at':now})
        from alerts import supersede_handed_off_question
        supersede_handed_off_question(root,worker.get('source_agent'),
                                     worker.get('handoff_created_at'))
        return f'AGENT STARTUP VERIFIED: {worker["agent"]} interactive CLI in {worker["window"]}'
    if now-started>=60 and worker.get('startup_status')!='failed':
        atomic_write_json(wd(root)/'worker.json',
                          {**worker,'startup_status':'failed','startup_checked_at':now,
                           'startup_failure_reason':reason or 'no matching interactive CLI process'})
        return ('AGENT STARTUP NOT VERIFIED: tmux window exists, but no matching '
                f'{worker["agent"]} CLI was found; inspect the terminal. '
                'No automatic replacement launched.')
    return None


def maybe_launch(root,config,usages,*,now=None):
    """Resume the latest unhandled handoff as soon as EITHER provider has quota.

    The opposite provider takes priority when both are ready on the same poll.
    However, if the opposite is exhausted and the previous provider resets first,
    the previous provider resumes its own handoff in a *new* managed tmux window.
    Pending markers do not expire: e.g. both providers can reset overnight.
    """
    if paused(root) or not config.get('auto_launch_next_agent',False):return None
    now=time.time() if now is None else now
    root=Path(root)
    handoff=root/'docs'/'HANDOFF.md'
    if not handoff_is_real(root):return None
    history=load_json(wd(root)/'launch-history.json',{}) or {}
    # Older pending ready markers must not start a second window after a newer
    # handoff has already launched its recipient. This can otherwise restart
    # the same provider immediately using a stale event from the prior session.
    worker=latest_worker(root)
    worker_started=(worker.get('started_at') if worker.get('session')==session_name(root)
                    and worker.get('outcome')=='launched' else None)
    candidates=[]
    for source in ('claude','codex'):
        event=load_json(ready_path(root,source),{}) or {}
        if event.get('source')!=source or not event.get('event_id'):continue
        if event['event_id'] in history:continue
        if (isinstance(worker_started,(int,float))
                and (not isinstance(event.get('created_at'),(int,float))
                     or event['created_at'] <= worker_started)):
            continue
        created=event.get('created_at')
        if not isinstance(created,(int,float)) or now-created<config.get('auto_launch_grace_seconds',10):continue
        # The handoff may receive legitimate edits while awaiting resets.
        # A file older than the verified Stop event, however, isn't sufficient.
        try:
            if handoff.stat().st_mtime_ns<event['handoff_mtime_ns']:continue
        except (OSError,KeyError,TypeError):continue
        candidates.append(event)
    if not candidates:return None
    # Never resurrect an older unhandled marker once a newer Stop event exists.
    event=max(candidates,key=lambda e:e['created_at'])
    source=event['source'];other='codex' if source=='claude' else 'claude'
    eligible=[a for a in (other,source) if can_resume(a,usages.get(a),config.get('prepare_remaining_pct',20))]
    pending_file=wd(root)/'pending-quota.json'
    if not eligible:
        # Remember the pending event for diagnostics across watchdog restarts,
        # but log only when the wait begins or a changed event is observed.
        previous=load_json(pending_file,{}) or {}
        waiting={'event_id':event['event_id'],'source':source,
                 'preferred':other,'status':'waiting',
                 'since':previous.get('since',now) if previous.get('event_id')==event['event_id'] else now}
        if previous.get('event_id')!=event['event_id'] or previous.get('status')!='waiting':
            atomic_write_json(pending_file,waiting)
            return f'WAITING for quota: neither {other} nor {source} has >20% remaining; will resume whichever is ready first'
        return None
    chosen=eligible[0]
    # The preferred recipient is normally the other agent; when that provider
    # has no quota, the reset original provider is a valid recipient too.
    result=launch(root,config,{**event,'target':chosen,'verified_stop':True},usages[chosen])
    if result.startswith('CREATED'):
        atomic_write_json(pending_file,{'event_id':event['event_id'],
                          'source':source,'resumed_by':chosen,'status':'resumed',
                          'resumed_at':time.time()})
    if result.startswith(('CREATED','FAILED')):return result
    # Important failures (e.g. unrelated tmux session) must be visible but not
    # spammed on every 20-second poll; the user can still run `status`.
    previous=load_json(pending_file,{}) or {}
    if previous.get('event_id')!=event['event_id'] or previous.get('reason')!=result:
        atomic_write_json(pending_file,{'event_id':event['event_id'],
                         'source':source,'status':'blocked','reason':result})
        return result
    return None


def explicit_codex(root,config,usage,retry=False):
    file=Path(root)/'docs'/'HANDOFF.md'
    if not handoff_is_real(root):return 'not launched: no real docs/HANDOFF.md yet'
    stat=file.stat()
    key='manual-'+hashlib.sha256(f'claude-to-codex\0{stat.st_mtime_ns}\0{stat.st_size}'.encode()).hexdigest()[:24]
    return launch(root,config,{'event_id':key,'source':'claude','target':'codex'},usage,allow_existing_event=retry)
