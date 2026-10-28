#!/usr/bin/env python3
"""Independently polls both local usage endpoints; native hooks steer both agents."""
from __future__ import annotations
import argparse,json,re,subprocess,sys,time,urllib.request
from pathlib import Path
from engine import PROMPT_HANDOFF, PROMPT_PREP, load_json, atomic_write_json, update, state_path, current, handoff_current
from usage import paths_only, metrics_from_json
from runner import maybe_launch, check_worker_startup, explicit_codex, latest_worker, pid_running, tmux_exists, show_agent_window, mark_ready, ready_path, session_name, launch_lock, handoff_is_real
from alerts import notify_pending_question, send_event, pending_question, is_auto_stall
from capacity import check as capacity_check, state_path as capacity_state_path
from session_recovery import recover_worker, detect_managed_agent, active_repository_agents
from quota_outage import failed as telemetry_failed, restored as telemetry_restored, qualifies as outage_qualifies, fresh_handoff as outage_fresh, read as outage_read
from session_summary import current_summary, instructions as summary_instructions
from console_ui import ConsoleUI, DashboardUI
from cli_paths import find_cli, guidance, cli_environment
from codex_steer import steer as steer_codex, UncertainDelivery
from pause_control import paused, activate, deactivate, MESSAGE as PAUSE_MESSAGE
from resume_agent import resume_existing
from codex_questions import check as check_codex_questions, question_open as codex_question_open
from claude_questions import check as check_claude_questions
from daybreak_watch import check as check_daybreak, current as daybreak_current, proceed as daybreak_proceed, restored as daybreak_restored, description as daybreak_description

ROOT=Path(__file__).resolve().parent.parent

def handoff_acknowledged_for_usage(root, agent, usage):
    """True when this *same quota episode* already produced a final handoff.

    Quota can remain at 90%+ for hours after the receiving agent starts. A
    verified ready marker must suppress further source-agent instructions until
    the source's quota resets and it reaches a new preparation threshold.
    """
    if usage.get('stage') not in ('prepare', 'handoff') or usage.get('stale'):
        return False
    start = usage.get('triggered_at') if usage.get('stage') == 'handoff' else usage.get('prep_at')
    if not isinstance(start, (int, float)):
        return False
    event = load_json(ready_path(root, agent), {}) or {}
    completed = event.get('created_at')
    return (event.get('source') == agent and bool(event.get('event_id'))
            and isinstance(completed, (int, float)) and completed >= start - 5)


def queue_codex_if_safe(root, config, usage, *, now=None, run=subprocess.run):
    """Steer a verified active turn, or queue advice for the managed Codex thread.

    A successful queue request is not immediate delivery. Never infer a thread
    from the terminal name, duplicate uncertain steers, or override approvals.
    """
    if paused(root) or not config.get('enable_codex_queue', True):
        return None
    # Never steer across a pending human approval. Quota Stop hooks can still
    # complete a safe handoff without authorising the requested operation.
    if pending_question(root) or codex_question_open(root):
        return None
    stage = usage.get('stage')
    if stage not in ('prepare', 'handoff') or usage.get('stale'):
        return None
    # The old provider remains quota-exhausted after a successful switch.
    # Never send it another threshold message for an acknowledged episode.
    if handoff_acknowledged_for_usage(root, 'codex', usage):
        return None
    now = time.time() if now is None else now
    key = usage.get('prep_at') if stage == 'prepare' else usage.get('triggered_at')
    if not key:
        return None
    prior_path = root / '.agent-watchdog' / 'codex-queued.json'
    prior = load_json(prior_path, {}) or {}
    same_event = prior.get('stage') == stage and prior.get('quota_event') == key
    delivery = None
    # A confirmed steer, accepted queue, or ambiguous steer cannot be retried.
    if same_event and (prior.get('status') == 'ok' or str(prior.get('status', '')).startswith('uncertain:')):
        return None
    # Throttle failures/missing IDs too, otherwise the watchdog emits a new
    # warning every 20 seconds and can flood the Codex app server.
    if same_event and now - prior.get('last_attempt', 0) < 90:
        return None

    managed = latest_worker(root)
    if not (managed.get('agent') == 'codex' and managed.get('type') == 'tmux'
            and managed.get('outcome') == 'launched'
            and managed.get('session') and tmux_exists(managed['session'])):
        # Do not queue into unrelated user-managed Codex conversations.
        # Record the failure so logs are meaningful and not emitted each poll.
        result = 'not delivered: no confirmed watchdog-managed Codex session'
        atomic_write_json(prior_path, {'status':result,'session':None,
            'stage':stage,'quota_event':key,'last_attempt':now})
        return 'CODEX THRESHOLD MESSAGE NOT DELIVERED: '+result

    active = load_json(root / '.agent-watchdog' / 'active-codex.json', {}) or {}
    recent = [sid for sid, t in active.items()
              if isinstance(t, (float, int)) and 0 <= now - t <= config.get('active_session_seconds', 120)]
    if len(recent) != 1:
        reason = (f'no unambiguous recent Codex thread ID ({len(recent)} candidates). '
                  'Check Codex /hooks: enable and trust project hooks, then restart Codex if necessary.')
        result = 'not delivered: ' + reason
        thread = None
    else:
        thread = recent[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', thread):
            result = 'not delivered: invalid Codex thread ID'
        else:
            message = PROMPT_PREP if stage == 'prepare' else PROMPT_HANDOFF
            # Existing conversations in this project use the Unix-socket remote
            # daemon; without --remote codex queue may target another server.
            # This poller starts via `wsl.exe ... python3` and does not
            # necessarily inherit NVM's PATH from the interactive Codex shell.
            # Resolve the binary explicitly rather than assuming bare `codex`
            # is available in the poller's process environment.
            try:
                delivered = steer_codex(thread, message)
            except UncertainDelivery as exc:
                # Never queue a second copy when the first may have been steered.
                result = f'uncertain: {exc}'
                delivery = 'unconfirmed'
            else:
                if delivered:
                    result = 'ok'
                    delivery = 'steered'
            executable = find_cli('codex') if delivery is None else None
            if delivery is None and not executable:
                result = 'failed: ' + guidance('codex')
            elif delivery is None:
                cmd = [executable, 'queue', '--remote', 'unix://',
                       '--thread', thread, '--message', message]
                try:
                    cp = run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=12, cwd=root, env=cli_environment(executable))
                    result = ('ok' if cp.returncode == 0 else
                              f'failed (exit {cp.returncode}): {(cp.stderr or cp.stdout)[:220]}')
                    if cp.returncode == 0: delivery = 'queued'
                except (OSError, subprocess.TimeoutExpired) as exc:
                    result = f'failed: {type(exc).__name__}: {exc}'
    atomic_write_json(prior_path, {
        'status': result, 'session': thread, 'stage': stage,
        'quota_event': key, 'last_attempt': now,
        'delivery': delivery,
    })
    prefix = 'PREP (80%)' if stage == 'prepare' else 'FINAL HANDOFF (90%)'
    if result == 'ok':
        if delivery == 'steered':
            return f'CODEX {prefix} MESSAGE STEERED into active turn on thread {thread}'
        return f'CODEX {prefix} MESSAGE QUEUED (NOT IMMEDIATE) to thread {thread} (remote unix://)'
    if result.startswith('uncertain:'):
        return f'CODEX {prefix} DELIVERY UNCONFIRMED; NOT RETRYING: {result}'
    return f'CODEX {prefix} MESSAGE NOT DELIVERED: {result}'


def poll(root,config,*,enable_launch=False):
    messages=[]
    user_paused = paused(root)
    if user_paused:
        messages.append('USER PAUSED: automatic continuation, quota messages and agent launches are suspended; -Command resume to reactivate')
    if enable_launch and not user_paused and not latest_worker(root).get('agent'):
        # Recover lost worker metadata only when a single real interactive CLI
        # is verified inside this checkout's exact tmux session. No injection.
        attempt=recover_worker(root)
        if attempt and attempt.startswith('RECOVERED'):
            messages.append(attempt)
    usages={}
    for agent in ('claude','codex'):
        old=load_json(state_path(root,agent),{}) or {}
        try:
            s=update(root,agent,config)
            if telemetry_restored(root,agent):messages.append(f'{agent.upper()} USAGE TELEMETRY RESTORED; normal quota logic resumes')
            usages[agent]=s
            display_stage=s['stage']
            if handoff_acknowledged_for_usage(root, agent, s):
                receiver=(load_json(root/'.agent-watchdog'/'launch-history.json',{}) or {}).get(
                    (load_json(ready_path(root,agent),{}) or {}).get('event_id'), {}).get('target')
                display_stage='handoff complete' + (f' → {receiver}' if receiver else ' (awaiting next agent)')
            label=f'{agent:6s} limiting {s.get("limiting_window", "other")}: {s["used_percent"]:.1f}% used ({s["remaining_percent"]:.1f}% left) — {display_stage}'
            if s['stage']!=old.get('stage'): label+=' [THRESHOLD CHANGED]'
            messages.append(label)
            for window in ('session','weekly','other'):
                data=s.get('windows',{}).get(window)
                if data:
                    reset_hint=f'; reset_at={data["reset_ts"]}' if data.get('reset_ts') is not None else ''
                    messages.append(f'         {window:7s} {data["used_percent"]:.1f}% used, {data["remaining_percent"]:.1f}% left (metric {data["metric"]}{reset_hint})')
            if agent=='codex':
                queued=queue_codex_if_safe(root,config,s)
                if queued:messages.append('         '+queued)
        except Exception as exc:
            outage,transition=telemetry_failed(root,agent,config)
            messages.append(f'{agent:6s} TELEMETRY UNAVAILABLE ({int(outage["age_seconds"])}s): {type(exc).__name__}: {exc}; action={outage["state"]}; no quota guessed')
            # Never infer a full quota or force-switch from an HTTP failure.
            # Native hooks will tell the active agent to checkpoint/stop.
            if transition and outage['state'] in ('checkpoint','pause'):
                worker=latest_worker(root)
                if worker.get('agent')==agent and worker.get('outcome')=='launched':
                    title=f'{agent.capitalize()} usage telemetry unavailable'
                    note=('Claude/Codex usage could not be verified. Check the active agent terminal; '
                          'the watchdog is requesting a safe checkpoint.' if outage['state']=='checkpoint' else
                          'Usage has remained unavailable. The active agent must complete a verified handoff and session summary before switching.')
                    results=send_event(root,config,title,note,'session usage monitoring needs attention; open the watchdog and agent terminal.')
                    messages.append('TELEMETRY ALERT '+str(results))
    # Claude's interactive first message is injected separately from --rc.
    # If the TUI never became safely idle, surface the failure rather than
    # claiming that a visible Claude window means work started.
    if enable_launch:
        try:
            failure=alert_claude_startup_failure(root,config)
            if failure:messages.append(failure)
        except Exception as exc:
            messages.append('Claude startup prompt alert failed: '+type(exc).__name__+': '+str(exc))
    # Claude may announce a formal user decision mid-turn before Stop fires.
    # Read ONLY the managed live tmux pane; notification uses the existing
    # per-channel retry ledger shared with the Stop hook.
    try:
        event = check_claude_questions(root, config)
        if event: messages.append(event)
    except Exception as exc:
        messages.append('CLAUDE QUESTION MONITOR FAILED: '+type(exc).__name__+': '+str(exc))
    # Codex may show "Queued follow-up inputs" *while still working*.
    # Unlike the Stop-hook marker, detect this directly in its live TUI.
    try:
        alert = check_codex_questions(root, config)
        if alert: messages.append(alert)
    except Exception as exc:
        messages.append('CODEX QUESTION MONITOR FAILED: '+type(exc).__name__+': '+str(exc))
    # Alerting is independent of quota success: unknown usage must not hide a
    # user-visible restriction. Daybreak refusal detection is read-only. Never send unverified /daybreak
    # toggle or /model selections into an interactive Codex composer.
    try:
        alert=check_daybreak(root, config)
        if alert:messages.append(alert)
    except Exception as exc:messages.append('Codex Daybreak monitor failed: '+type(exc).__name__+': '+str(exc))
    # a human-blocked question from the user.
    try:
        alert=notify_pending_question(root,config)
        if alert:messages.append(alert)
    except Exception as exc:messages.append('alert handling failed: '+type(exc).__name__+': '+str(exc))
    if enable_launch and not user_paused:
        try:
            alert=alert_missing_handoff_ack(root,config,usages)
            if alert:messages.append(alert)
        except Exception as exc:messages.append('handoff acknowledgement alert failed: '+type(exc).__name__+': '+str(exc))
        try:
            recovered=capacity_check(root,config,usages)
            if recovered:messages.append('CAPACITY RECOVERY '+recovered)
        except Exception as exc:messages.append('capacity recovery failed: '+type(exc).__name__+': '+str(exc))
    started=maybe_launch(root,config,usages) if enable_launch and not user_paused else None
    if started:messages.append('AUTO-HANDOFF '+started)
    if enable_launch:
        startup=check_worker_startup(root)
        if startup:messages.append(startup)
    return messages



def alert_claude_startup_failure(root, config, *, sender=send_event):
    """Notify once if a Claude TUI is running but startup prompt was not delivered."""
    path=Path(root)/'.agent-watchdog'/'claude-prompt-delivery.json'
    report=load_json(path,{}) or {}
    if report.get('status') not in ('failed','not_delivered') or not report.get('token'):
        return None
    receipt=Path(root)/'.agent-watchdog'/'claude-prompt-alert.json'
    previous=load_json(receipt,{}) or {}
    if previous.get('token')==report['token']:
        return None
    # Do not include project details in the mobile notification.
    title='Claude handoff prompt needs attention'
    result=sender(root,config,title,
                  'Claude opened, but the automatic first message was not submitted. '
                  'Open its terminal and send: Read docs/HANDOFF.md and continue with next task.',
                  'Claude started, but its first handoff instruction was not delivered. Open the agent terminal.')
    atomic_write_json(receipt,{'token':report['token'],'result':result,'at':time.time()})
    return 'CLAUDE FIRST MESSAGE NOT DELIVERED: '+str(report.get('reason','Unknown cause'))


def acknowledge_ready(root, config, agent):
    """Signal the last COMPLETED handoff without requiring a provider Stop hook.

    Only a watchdog-managed, quota-exhausted agent with a fresh handoff may
    acknowledge completion. This never itself starts or kills an agent; the
    independently running watchdog performs the ordinary safe quota check.
    """
    if agent not in ('claude','codex'):
        return 'NOT READY: select --agent claude or --agent codex'
    root=Path(root)
    managed=latest_worker(root)
    if not (managed.get('agent')==agent and managed.get('type')=='tmux'
            and managed.get('outcome')=='launched'
            and managed.get('session')==session_name(root)
            and tmux_exists(managed['session'])):
        return 'NOT READY: no live watchdog-managed '+agent+' terminal'
    info=current(root,agent,config,max_age=0)
    outage=outage_qualifies(root,agent) and (info.get('stale') or info.get('stage')=='unknown')
    if (info.get('stale') or info.get('stage')!='handoff') and not outage:
        return 'NOT READY: '+agent+' has neither reached a verified 90% quota threshold nor completed a sustained telemetry-outage checkpoint'
    since=(outage_read(root,agent).get('escalated_at') if outage else info.get('prep_at') or info.get('triggered_at'))
    if not (outage_fresh(root,agent) if outage else handoff_current(root,info)):
        return 'NOT READY: docs/HANDOFF.md has not been freshly updated for this quota/outage event'
    if not current_summary(root,agent,since):
        return 'NOT READY: a dated session summary is missing in docs/session-reports/. '+summary_instructions(agent)
    event=mark_ready(root,agent,managed.get('event_id') or 'managed',root/'docs'/'HANDOFF.md')
    return ('HANDOFF READY: '+agent+' has acknowledged completed handoff '
            +event['event_id']+'. Keep the watchdog running; it will select the '
            'next eligible agent and launch it after the grace period.')



def acknowledge_exited_handoff(root, config, agent, *, confirm_complete=False, allow_stale=False):
    """Explicit human recovery if a previously managed terminal has vanished.

    This is intentionally NOT performed in the background. A disappeared TUI
    does not establish that the final handoff was finished correctly.
    """
    root=Path(root).resolve()
    if agent not in ('claude','codex'):
        return 'NOT READY: expected --agent claude or codex'
    if not confirm_complete:
        return ('NOT READY: terminal is gone; check docs/HANDOFF.md and Git status, '
                'then retry with -ConfirmComplete only if the work is saved.')
    with launch_lock(root):
        session=session_name(root)
        if tmux_exists(session):
            return ('NOT READY: managed tmux session exists; inspect it or use ordinary ready '
                    'without -ConfirmComplete instead.')
        active=active_repository_agents(root)
        if active:
            return ('NOT READY: a CLI may still be running in this repository: '
                    +', '.join(active[:5])+'. No handoff was marked ready.')
        info=current(root,agent,config,max_age=0)
        if (info.get('stale') or info.get('stage')=='unknown'
                or (info.get('stage')!='handoff' and not allow_stale)):
            return (f'NOT READY: {agent} must have a fresh quota reading at 90%+, '
                    'unless you explicitly opt into -AllowStaleHandoff after verifying the completed handoff.')
        if not handoff_is_real(root):
            return 'NOT READY: no real docs/HANDOFF.md exists to resume.'
        if not handoff_current(root,info) and not allow_stale:
            return ('NOT READY: handoff was not updated during the current quota event. '
                    'Read it and inspect the current diff; if it is still safe to resume '
                    'from, rerun with -ConfirmComplete -AllowStaleHandoff.')
        handoff=root/'docs'/'HANDOFF.md'
        event=mark_ready(root,agent,'human-confirmed-after-exit',handoff)
        # No CLI is killed or launched here. Polling watchdog checks the quota
        # and runs the ordinary duplicate-protected launcher next.
        return (f'HANDOFF READY (manual recovery): {agent} terminal is gone; '
                f'event {event["event_id"]} recorded. '
                'The running watchdog will launch whichever agent has quota. '
                'The receiving agent must re-verify the actual files and Git state.')


def handoff_diagnostics(root,config):
    """Explain *why* no next agent has appeared, without claiming delivery."""
    root=Path(root);worker=latest_worker(root)
    if worker.get('agent') not in ('claude','codex'):
        return ('Handoff diagnostic: no watchdog-managed active agent is recorded. '
                'If its tmux session has exited, verify docs/HANDOFF.md and then use '
                '-Command ready -Agent codex -ConfirmComplete (or claude) to recover. '
                'Do not do this while an agent is still working.')
    source=worker['agent'];other='codex' if source=='claude' else 'claude'
    info=load_json(state_path(root,source),{}) or {}
    stage=info.get('stage')
    if stage=='normal':return f'Handoff diagnostic: {source} still has quota; switching is not expected.'
    if stage=='prepare':return (f'Handoff diagnostic: {source} is at 80-89% (preparation only). '
                                f'{other} will NOT launch until the final 90% handoff is acknowledged.')
    if stage!='handoff':return f'Handoff diagnostic: {source} quota cannot be verified.'
    ready=load_json(ready_path(root,source),{}) or {}
    if not ready.get('event_id'):
        return (f'Handoff diagnostic: {source} is at 90%+ but has NOT acknowledged final handoff. '
                'Check its terminal and Stop hook; once the handoff is complete, run '
                f'python3 .agent-watchdog/watchdog.py ready --agent {source}. '
                'Do not run that while development is still in progress.')
    history=load_json(root/'.agent-watchdog'/'launch-history.json',{}) or {}
    if ready['event_id'] in history:
        return f'Handoff diagnostic: event {ready["event_id"]} has already launched {history[ready["event_id"]].get("target")}; check the terminal.'
    pending=load_json(root/'.agent-watchdog'/'pending-quota.json',{}) or {}
    if pending.get('event_id')==ready['event_id']:
        return 'Handoff diagnostic: '+str(pending.get('status'))+' — '+str(pending.get('reason') or 'waiting for enough quota; see Pending handoff above')
    return (f'Handoff diagnostic: {source} handoff is verified and ready; '
            'the running watchdog should check quotas and switch on the next poll.')


def alert_missing_handoff_ack(root,config,usages,*,now=None,sender=send_event):
    """A quiet, once-per-quota-event alert when the final handoff never arrives.

    The original CLI might still be coding; never attempt to force-stop it or
    start a second worker. Tell the human there is a genuine pending condition.
    """
    now=time.time() if now is None else now
    worker=latest_worker(root)
    agent=worker.get('agent')
    if agent not in ('claude','codex') or worker.get('type')!='tmux' or worker.get('outcome')!='launched':
        return None
    usage=usages.get(agent) or {}
    since=usage.get('triggered_at')
    if (usage.get('stage')!='handoff' or usage.get('stale')
            or not isinstance(since,(int,float)) or now-since<max(120,config.get('handoff_alert_after_seconds',300))):
        return None
    event=load_json(ready_path(root,agent),{}) or {}
    if event.get('event_id') and event.get('created_at',0)>=since-5:
        return None
    path=Path(root)/'.agent-watchdog'/'missing-handoff-alert.json'
    prior=load_json(path,{}) or {}
    key=f'{agent}:{since}'
    if prior.get('key')==key and (prior.get('sent') or now-prior.get('attempted_at',0)<180):
        return None
    title=f'{agent.capitalize()} handoff needs checking'
    body=(f'{agent.capitalize()} is at 90%+ usage but has not acknowledged a completed handoff. '
          'Check its terminal and docs/HANDOFF.md. If finished, the agent can run '
          f'python3 .agent-watchdog/watchdog.py ready --agent {agent}. '
          'The watchdog will not force-switch a possibly active agent.')
    results=sender(root,config,title,body,
                   'An agent reached its handoff threshold but has not confirmed completion. Check the terminal.')
    ok=any(v=='sent' for v in results.values())
    atomic_write_json(path,{'key':key,'sent':ok,'attempted_at':now,'result':results})
    return f'HANDOFF NOT CONFIRMED after {int(now-since)}s: '+', '.join(k+'='+str(v) for k,v in results.items())

def inspect(config,agent):
    url=config['agents'][agent]['url']
    req=urllib.request.Request(url,headers={'Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=3) as f:obj=json.loads(f.read(100001))
    print(f'API JSON paths for {agent} (no values printed):')
    for path,typ in paths_only(obj):print(' ',path,'('+typ+')')
    print('\nDetected usage measurements:')
    for m in metrics_from_json(obj,config['agents'][agent].get('used_pct_path')):
        print(' ',m.path, m.used_percent,'% used')
    if not metrics_from_json(obj,config['agents'][agent].get('used_pct_path')):
        print('  NONE. Set agents.'+agent+'.used_pct_path in .agent-watchdog/config.json to the correct JSON path.')


def main():
    parser=argparse.ArgumentParser(description='Local Claude Code + Codex handoff watchdog')
    parser.add_argument('command',nargs='?',choices=('run','once','status','inspect','launch-codex','ready','recover-session','show','notify-test','notify-setup','pause','resume','resume-check','unpause','daybreak-continue','daybreak-restored','daybreak-status'),default='run')
    parser.add_argument('--agent',choices=('claude','codex'),default='claude')
    parser.add_argument('--interval',type=int,default=20)
    parser.add_argument('--retry',action='store_true',help='retry an already attempted manual Codex launch')
    parser.add_argument('--confirm-complete',action='store_true',help='I verified the final handoff after the managed CLI exited')
    parser.add_argument('--allow-stale-handoff',action='store_true',help='use a handoff older than this quota event after manually inspecting it')
    args=parser.parse_args()
    config=load_json(ROOT/'.agent-watchdog'/'config.json')
    if not config:parser.error(f'No config at {ROOT / ".agent-watchdog/config.json"}; run installer first')
    if args.command=='pause':
        activate(ROOT, source='explicit watchdog command')
        print('PAUSED: automated work, new agent launches and injected continuations disabled. Existing tool executions may still be running; interrupt them separately.')
        return
    if args.command=='resume-check':
        print(resume_existing(ROOT,config,check_only=True))
        return
    if args.command in ('resume','unpause'):
        was_paused=deactivate(ROOT)
        if args.command=='unpause':
            print('UNPAUSED: automation enabled; no agent input sent.' if was_paused else 'ALREADY UNPAUSED: no agent input sent.')
            return
        print('Pause cleared.' if was_paused else 'Pause already inactive.')
        result=resume_existing(ROOT,config)
        if result.startswith('RESUMED '):
            # This was an explicitly user-invoked, hook-confirmed resume. It
            # overrides a prior background-wait latch; an automatic continue
            # or unsuccessful resume must NEVER clear that latch.
            from background_wait import finish as finish_background_wait
            agent=latest_worker(ROOT).get('agent')
            if agent in ('claude','codex'):
                finish_background_wait(ROOT,agent,event='explicit_cli_resume')
        print(result)
        return
    if args.command=='daybreak-continue':
        print(daybreak_proceed(ROOT))
        return
    if args.command=='daybreak-restored':
        print(daybreak_restored(ROOT))
        return
    if args.command=='daybreak-status':
        item=daybreak_current(ROOT)
        if item:
            print('CODEX DAYBREAK RECOVERY: '+str(item.get('phase')))
            print(daybreak_description(ROOT))
            print('Saved watchdog preset (NOT verified active TUI model):',
                  json.dumps(item.get('saved_watchdog_preset', {})))
        else:print('No active Codex Daybreak recovery incident.')
        return
    if args.command=='notify-setup':
        phone=config.get('alerts',{}).get('phone',{})
        print('Phone push server: '+phone.get('server','https://ntfy.sh'))
        print('Subscribe in the ntfy Android app to this topic: '+phone.get('topic','NOT CONFIGURED'))
        print('Phone push enabled: '+str(phone.get('enabled',False)))
        print('Private questions are not included in push bodies by default.')
        print('Allow notifications for ntfy in Android settings; see README.')
        return
    if args.command=='notify-test':
        result=send_event(ROOT,config,'session Watchdog test',
                          'Desktop alerts are configured. Watchdog is ready.',
                          'Phone push test: watchdog is ready.')
        print(json.dumps(result,indent=2))
        return
    if args.command=='inspect':
        inspect(config,args.agent);return
    if args.command=='recover-session':
        result=recover_worker(ROOT)
        print(result or 'No recovery needed: a managed agent is already recorded')
        if result and result.startswith('RECOVERY NOT APPLIED'):sys.exit(1)
        return
    if args.command=='ready':
        if args.confirm_complete:
            result=acknowledge_exited_handoff(ROOT,config,args.agent,confirm_complete=True,allow_stale=args.allow_stale_handoff)
        else:
            result=acknowledge_ready(ROOT,config,args.agent)
        print(result)
        if not result.startswith('HANDOFF READY'):sys.exit(1)
        return
    if args.command=='launch-codex':
        from engine import current
        usage=current(ROOT,'codex',config,max_age=0)
        result=explicit_codex(ROOT,config,usage,retry=args.retry)
        print(result)
        if not result.startswith('CREATED'):sys.exit(1)
        return
    if args.command=='show':
        result=show_agent_window(ROOT,config,force=True)
        print(result)
        if not result.startswith('Windows Terminal opened'):sys.exit(1)
        return
    if args.command=='status':
        print('USER PAUSE: '+('ACTIVE — automation suspended' if paused(ROOT) else 'not active'))
        daybreak=daybreak_current(ROOT)
        if daybreak:
            print('CODEX DAYBREAK RECOVERY:',daybreak.get('phase'))
            print('Daybreak action:',daybreak_description(ROOT))
            print('Saved watchdog preset (not verified active model):',json.dumps(daybreak.get('saved_watchdog_preset',{})))
        queued=load_json(ROOT/'.agent-watchdog'/'codex-questions.json',{}) or {}
        if queued.get('active'):
            print('CODEX QUEUED INTERACTIVE QUESTIONS:',json.dumps({
                'count':queued.get('count'), 'first_seen_at':queued.get('first_seen_at'),
                'last_seen_at':queued.get('last_seen_at'), 'alerts':queued.get('alerts',{})
            },indent=2))
        from background_wait import current as current_background_wait
        waiting=current_background_wait(ROOT)
        if waiting:
            print('BACKGROUND JOB WAIT (not a user question):',json.dumps(waiting,indent=2))
        question=pending_question(ROOT)
        if question:
            print('PENDING USER INPUT:',json.dumps({
                'agent':question.get('agent'),
                'question':question.get('question'),
                'origin':('watchdog auto-continue stall (explicit resume may acknowledge)'
                          if is_auto_stall(question) else question.get('source','unknown/legacy')),
                'created_at':question.get('created_at')},indent=2))
            from alerts import recording_key
            ledger = load_json(ROOT/'.agent-watchdog'/'alert-delivery.json', {}) or {}
            delivery = ledger.get(recording_key(question), {})
            if delivery:
                print('QUESTION NOTIFICATION DELIVERY:', json.dumps({
                    ch: {k: item.get(k) for k in ('sent','attempted_at','result')}
                    for ch, item in delivery.items() if ch in ('desktop','phone')
                }, indent=2))
            else:
                print('QUESTION NOTIFICATION DELIVERY: not yet attempted')
        else:
            print('PENDING USER INPUT: none')
            # Keep a brief delivery diagnostic even after the agent answered:
            # otherwise a missed ntfy alert becomes impossible to investigate.
            ledger = load_json(ROOT/'.agent-watchdog'/'alert-delivery.json', {}) or {}
            if ledger:
                latest = next(reversed(ledger.values()))
                channels = {ch: {k: item.get(k) for k in ('sent','attempted_at','result')}
                            for ch, item in latest.items() if ch in ('desktop','phone')}
                if channels:
                    print('MOST RECENT QUESTION NOTIFICATION:',json.dumps(channels,indent=2))
        active=load_json(ROOT/'.agent-watchdog'/'active-codex.json',{}) or {}
        if active:
            live=[sid for sid,ts in active.items() if isinstance(ts,(int,float)) and time.time()-ts<=config.get('active_session_seconds',120)]
            print('Recent Codex hook-tracked session IDs:',live or '(none)')
        else:print('Recent Codex hook-tracked session IDs: NONE — check /hooks and restart the Codex CLI if required')
        resume_state=load_json(ROOT/'.agent-watchdog'/'resume-delivery.json',{}) or {}
        if resume_state:
            print('Interactive resume:', json.dumps({k:v for k,v in resume_state.items() if k!='token'},indent=2))
        first=load_json(ROOT/'.agent-watchdog'/'claude-prompt-delivery.json',{}) or {}
        if first:print('Claude first-message delivery:', json.dumps({k:v for k,v in first.items() if k!='token'},indent=2))
        delivery=load_json(ROOT/'.agent-watchdog'/'codex-queued.json',{}) or {}
        codex_info=load_json(state_path(ROOT,'codex'),{}) or {}
        if delivery:
            label=('Codex threshold delivery (historical: handoff already acknowledged)'
                   if handoff_acknowledged_for_usage(ROOT,'codex',codex_info)
                   else 'Codex threshold delivery')
            print(label+':',json.dumps(delivery,indent=2))
        capacity=load_json(capacity_state_path(ROOT),{}) or {}
        if capacity:print('Capacity recovery:',json.dumps(capacity,indent=2))
        pending=load_json(ROOT/'.agent-watchdog'/'pending-quota.json',{}) or {}
        if pending:print('Pending handoff:',json.dumps(pending,indent=2))
        worker=latest_worker(ROOT)
        if not worker.get('agent'):
            evidence,reason=detect_managed_agent(ROOT)
            if evidence:
                print('Untracked managed tmux CLI:', json.dumps(evidence, indent=2))
                print('Recovery: run -Command recover-session, then check status again')
            else:
                print('Session recovery inspection:', reason)
        if worker:
            running=tmux_exists(worker['session']) if worker.get('type')=='tmux' else pid_running(worker.get('pid'))
            print('Managed worker:',json.dumps({**worker,'running':running},indent=2))
        for agent in ('claude','codex'):
            s=load_json(state_path(ROOT,agent),{})
            print(agent, json.dumps(s or {'status':'not checked'},indent=2))
            outage=outage_read(ROOT,agent)
            if outage.get('state') in ('checkpoint','pause','grace'):
                print(agent+' TELEMETRY OUTAGE:',json.dumps(outage,indent=2))
        print(handoff_diagnostics(ROOT,config))
        return
    print(f'Watchdog in {ROOT} (Ctrl+C stops watcher; hooks remain installed)',flush=True)
    try:
        with DashboardUI(ROOT) as ui:
            while True:
                messages=poll(ROOT,config,enable_launch=(args.command=='run'))
                if args.command=='once':
                    for item in messages:print(time.strftime('[%Y-%m-%d %H:%M:%S] ')+item,flush=True)
                    break
                ui.write_messages(messages)
                # Read Shift+M while sleeping; polling continues in the same
                # process and settings apply only to future launches.
                end=time.monotonic()+max(5,args.interval)
                while time.monotonic()<end:
                    if ui.read_key(min(.2,max(0,end-time.monotonic()))):
                        try:
                            ui.clear_footer()
                            ok=ui.choose_models()
                            print('Presets changed LIVE. The next agent launch will use them; the active agent is unchanged.' if ok else 'Model selection cancelled; choices unchanged.',flush=True)
                        except Exception as exc:
                            print(f'Model picker failed; old choices retained: {type(exc).__name__}: {exc}',flush=True)
                        ui.show_footer()
                        end=time.monotonic()+1
                    if not ui.available:time.sleep(.15)
    except KeyboardInterrupt:print('\nWatchdog stopped.')
if __name__=='__main__':main()
