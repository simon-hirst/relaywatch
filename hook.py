#!/usr/bin/env python3
"""Claude Code / Codex lifecycle hook. No third-party dependencies."""
from __future__ import annotations
import argparse,json,sys,time
from pathlib import Path
from engine import PROMPT_HANDOFF, PROMPT_PREP, atomic_write_json, current, handoff_current, load_json
from quota_behavior import premature_quota_wait, premature_wait_correction, work_policy
from runner import mark_ready, latest_worker, tmux_exists, session_name
from alerts import detect_question, register_question, clear_question, request_path, is_automatic_continuation, is_auto_stall, notify_pending_question
from capacity import record_manual_turn, record_tool_progress, ERROR as CAPACITY_ERROR
from quota_outage import directive as outage_directive, fresh_handoff as outage_fresh, qualifies as outage_qualifies, read as outage_read
from session_summary import current_summary, instructions as summary_instructions
from claude_startup import mark_received as confirm_claude_first_message
from pause_control import paused, activate, is_pause_request, MESSAGE as PAUSE_MESSAGE
from codex_questions import question_open as codex_question_open
from daybreak_watch import detect as detect_daybreak_refusal, note as note_daybreak_refusal, completed_work as daybreak_completed_work, current as daybreak_current, user_prompt as daybreak_user_prompt
from background_wait import (detect_background_wait, note as note_background_wait,
                             finish as finish_background_wait, waiting_for as background_waiting_for,
                             reports_background_completion)
from resume_agent import (mark_received as confirm_manual_resume, RESUME_CONTEXT,
                          reports_stale_pause, note_stale_pause)


def native_claude_decision(payload):
    """Summarize native Claude dialogs without selecting or authorizing options.

    PreToolUse:AskUserQuestion fires before Claude opens its multiple-choice
    terminal UI. ExitPlanMode can likewise request a human plan decision.
    Unrelated tools and malformed requests must not trigger user alerts.
    """
    if not isinstance(payload, dict):
        return None
    tool = payload.get('tool_name')
    if tool == 'AskUserQuestion':
        data = payload.get('tool_input')
        questions = data.get('questions') if isinstance(data, dict) else None
        texts = []
        if isinstance(questions, list):
            for item in questions[:4]:
                q = item.get('question') if isinstance(item, dict) else None
                if isinstance(q, str) and q.strip():
                    texts.append(' '.join(q.split())[:300])
        if texts:
            return ' | '.join(texts)[:500], 'claude_native_AskUserQuestion'
        return 'Claude opened an interactive question picker. Respond in the Claude terminal.', 'claude_native_AskUserQuestion'
    if tool == 'ExitPlanMode':
        return 'Claude is requesting approval of its plan. Review the plan in the Claude terminal.', 'claude_native_ExitPlanMode'
    return None


def hook(root,agent,event,session_id, payload=None):
    config=load_json(Path(root)/'.agent-watchdog'/'config.json')
    if not config:return {}
    # A user's explicit pause beats quota instructions, Stop-hook auto-continuation,
    # even future model-generated "continue" prompts. A CLI resume is required.
    if event=='UserPromptSubmit' and is_pause_request((payload or {}).get('prompt')):
        activate(root,source=f'{agent}:UserPromptSubmit')
        return {'continue': False, 'stopReason': PAUSE_MESSAGE}
    if paused(root):
        if event=='Stop':
            return {'continue': False, 'stopReason': PAUSE_MESSAGE}
        if event=='PreToolUse':
            return {'continue': False, 'stopReason': PAUSE_MESSAGE,
                    'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                        'permissionDecision': 'deny',
                        'permissionDecisionReason': PAUSE_MESSAGE}}
        if event=='SessionStart':
            return {'hookSpecificOutput': {'hookEventName':'SessionStart',
                    'additionalContext': PAUSE_MESSAGE}}
        # Also refuse auto-injected messages while paused. Never interpret
        # an ordinary 'continue' as authorization to clear the pause flag.
        if event=='UserPromptSubmit':
            return {'continue': False, 'stopReason': PAUSE_MESSAGE}
        if event in ('PostToolUse',):
            return {'continue': False, 'stopReason': PAUSE_MESSAGE}
        return {}
    # Native question UIs are tool calls, not assistant Stop messages.
    # Catch them before quota polling or long-running processing: no permit
    # decision is returned, no answer is chosen, and Claude's UI stays in control.
    if agent == 'claude' and event in ('PreToolUse', 'PostToolUse'):
        tool_name = (payload or {}).get('tool_name')
        if tool_name in ('AskUserQuestion', 'ExitPlanMode'):
            worker = latest_worker(root)
            owned = (worker.get('type') == 'tmux' and worker.get('agent') == 'claude'
                     and worker.get('outcome') == 'launched'
                     and worker.get('session') == session_name(root)
                     and bool(session_id) and tmux_exists(worker['session']))
            if owned and event == 'PreToolUse':
                decision = native_claude_decision(payload)
                if decision:
                    question, source = decision
                    # Register and deliver through the existing deduped ledger.
                    # Failed channels are still retryable by the poller.
                    if register_question(root, 'claude', question, source=source):
                        try:
                            notify_pending_question(root, config)
                        except Exception:
                            pass
            if owned and event == 'PostToolUse':
                pending = load_json(request_path(root), {}) or {}
                if (pending.get('agent') == 'claude'
                        and pending.get('status') == 'open'
                        and pending.get('source') == 'claude_native_' + tool_name):
                    clear_question(root, 'claude')
            # Don't add unrelated quota guidance to the interaction dialog.
            return {}
    resume_confirmed=False
    if event=='UserPromptSubmit':
        resume_confirmed=confirm_manual_resume(root,agent,(payload or {}).get('prompt',''))
    if event=='UserPromptSubmit' and agent=='claude':
        # Confirm promptly, even if quota telemetry is currently unavailable.
        confirm_claude_first_message(root, (payload or {}).get('prompt', ''))
    # Touch activity for Codex proactive queue only on real activity.
    if agent=='codex' and session_id and event in ('PreToolUse','PostToolUse','UserPromptSubmit'):
        path=Path(root)/'.agent-watchdog'/'active-codex.json'
        active=load_json(path,{}) or {}
        active={sid:ts for sid,ts in active.items() if time.time()-ts<600}
        active[str(session_id)]=time.time()
        atomic_write_json(path,active)
    info=current(root,agent,config)
    condition=info.get('stage')
    worker_for_outage=latest_worker(root)
    managed_here=(worker_for_outage.get('type')=='tmux' and worker_for_outage.get('agent')==agent and worker_for_outage.get('outcome')=='launched')
    outage_text=outage_directive(root,agent) if managed_here else ''
    outage_pause=managed_here and outage_qualifies(root,agent)
    # A fresh *healthy* measurement supersedes even a lingering outage state.
    if not info.get('stale') and condition in ('normal','prepare','handoff'):
        outage_text=''
        outage_pause=False
    # A tool call used to checkpoint a still-running measurement is NOT its
    # completion. Clear a background wait only on a genuine user message or a
    # later assistant report confirming the external job finished.
    if event=='UserPromptSubmit' and not is_automatic_continuation((payload or {}).get('prompt')):
        if finish_background_wait(root,agent,session_id,event='user_message') and session_id:
            # The previous idle Stop was an intentional background wait, not
            # evidence of a loop. Reset no-progress accounting on real activity.
            progress_path=Path(root)/'.agent-watchdog'/'auto-continue-state.json'
            progress=load_json(progress_path,{}) or {}
            progress.pop(f'{agent}:{session_id}',None)
            atomic_write_json(progress_path,progress)
    if event=='UserPromptSubmit' and agent=='codex':
        daybreak_context = daybreak_user_prompt(root,(payload or {}).get('prompt','')) if managed_here else None
        record_manual_turn(root,agent,payload)
        if daybreak_context and not info.get('stale') and condition == 'normal':
            return {'hookSpecificOutput': {
                'hookEventName': 'UserPromptSubmit', 'additionalContext': daybreak_context}}
    if event=='PostToolUse' and agent=='codex':
        record_tool_progress(root,agent)
    if event=='Stop':
        # Never blindly toggle persistent Daybreak settings or select a model
        # inside a TUI. An explicit human-confirmed recovery is required.
        if agent=='codex':
            last=str((payload or {}).get('last_assistant_message') or '')
            if detect_daybreak_refusal(last):
                note_daybreak_refusal(root,origin='Codex Stop hook',fresh_stop=True)
            if daybreak_completed_work(root,last):
                return {}
        # A stale pause belief is NOT a completed milestone. Do not create an
        # infinite Stop-hook 'continue' loop, wasting the user's quota. If the
        # pause marker were genuinely active, the early paused() branch would
        # already have returned continue:false.
        if reports_stale_pause((payload or {}).get('last_assistant_message')):
            note_stale_pause(root,agent)
            return {}
        # Capacity errors aren't successful turns. Let the polling watchdog
        # retry with backoff instead of spawning Stop-hook continuation loops.
        if agent=='codex' and CAPACITY_ERROR.search(str((payload or {}).get('last_assistant_message') or '')):
            return {}
        # Agents explicitly mark questions requiring input. An open blocker stops
        # the auto-continuation hook so a human can respond without fighting a loop.
        question, origin = detect_question((payload or {}).get('last_assistant_message'))
        if question:
            # Push immediately from the native Stop hook. Polling also retries
            # failed channels; the shared delivery ledger prevents duplicates.
            # A stopped agent must not depend on an active watchdog poller for
            # the user's first question notification.
            register_question(root, agent, question, source=origin)
            # The normal poller will send/retry regardless. Send directly only
            # from a verified, live watchdog-owned terminal; a bare hook call in
            # an unrelated CLI or an offline test must never post to ntfy.
            if (managed_here and session_id
                and worker_for_outage.get('session') == session_name(root)
                and tmux_exists(worker_for_outage['session'])):
                try:
                    notify_pending_question(root, config)
                except Exception:
                    pass  # Delivery must never break the agent's Stop hook.
        if (condition=='handoff' and not info.get('stale')) or outage_pause:
            # Never switch on the strength of missing telemetry alone. Require
            # a fresh verified handoff AND a dated agent-authored summary.
            ready_to_switch=(handoff_current(root,info) if condition=='handoff' and not info.get('stale') else outage_fresh(root,agent))
            since=(info.get('prep_at') or info.get('triggered_at')) if condition=='handoff' and not info.get('stale') else outage_read(root,agent).get('escalated_at')
            summary=current_summary(root,agent,since)
            if ready_to_switch and summary:
                mark_ready(root,agent,session_id,Path(root)/'docs'/'HANDOFF.md')
                return {}
            if payload and payload.get('stop_hook_active'):return {}
            instruction=(PROMPT_HANDOFF if condition=='handoff' and not info.get('stale') else outage_text)
            missing=(' Handoff not fresh.' if not ready_to_switch else '') + (' Session summary missing.' if not summary else '')
            return {'decision':'block','reason':instruction+' '+summary_instructions(agent)+missing}
        if agent=='codex' and daybreak_current(root).get('phase') in ('needs_switch','needs_restore'):
            # This is not a quota handoff, authorization or model auto-switch.
            return {}
        # An asynchronous background task is neither completed work nor a
        # question for the user. Do not spin the Stop hook, count no-progress
        # retries, or create a false "agent has a question" notification.
        # At the 90% threshold above, the final handoff still takes priority.
        background=detect_background_wait((payload or {}).get('last_assistant_message'))
        if background and managed_here and condition in ('normal','prepare') and not info.get('stale'):
            note_background_wait(root,agent,session_id,background)
            # A preceding Stop may have recorded a watchdog-generated stall.
            # Reclassify ONLY that synthetic alert; real approvals stay open.
            stalled_notice=load_json(request_path(root),{}) or {}
            if stalled_notice.get('agent')==agent and is_auto_stall(stalled_notice):
                atomic_write_json(request_path(root),{
                    **stalled_notice,'status':'acknowledged',
                    'acknowledged_by':'background_wait_detected',
                    'acknowledged_at':time.time()})
            # A background wait must not count toward the no-progress ceiling.
            progress_path=Path(root)/'.agent-watchdog'/'auto-continue-state.json'
            progress=load_json(progress_path,{}) or {}
            progress.pop(f'{agent}:{session_id}',None)
            atomic_write_json(progress_path,progress)
            return {}
        # The waiting latch survives interim checkpoint/tool invocations. If
        # the next final message lacks an explicit marker, don't wake Claude
        # into a polling loop. A completion report or an actual user prompt
        # clears it; quota handoff above still takes precedence.
        if managed_here and background_waiting_for(root,agent,session_id):
            if reports_background_completion((payload or {}).get('last_assistant_message')):
                finish_background_wait(root,agent,session_id,event='reported_completed')
            else:
                return {}
        pending=load_json(request_path(root),{}) or {}
        if pending.get('status')=='open' and pending.get('agent')==agent:
            return {}
        # A question may be waiting in Codex's interactive queue even while
        # it was still working. Do not auto-inject a new task at Stop until the
        # user has had a chance to answer the queued decision.
        if agent=='codex' and codex_question_open(root):
            return {}
        # A normal completed task is not a quota handoff. Codex/Claude Stop
        # hooks support decision=block to generate a continuation turn, and
        # therefore prevent the interactive session from going idle after T05.
        # Only act for the watchdog-owned provider, not unrelated agents.
        if (not config.get('auto_continue_turns',True)
            or condition not in ('normal','prepare')
            or info.get('stale')
            or (Path(root)/'.agent-watchdog'/'PAUSE_AUTO_CONTINUE').exists()):
            return {}
        worker=latest_worker(root)
        if worker.get('type')!='tmux' or worker.get('agent')!=agent or worker.get('outcome')!='launched':
            return {}
        if not session_id:return {}  # Don't continue an unknown session.
        # If a completed turn produces no updated handoff *twice*, stop to avoid
        # unbounded loops on blocked tasks. Agents should update the handoff
        # on every meaningful milestone. This state is local, never committed.
        statefile=Path(root)/'.agent-watchdog'/'auto-continue-state.json'
        state=load_json(statefile,{}) or {}
        key=f'{agent}:{session_id}'
        handoff=Path(root)/'docs'/'HANDOFF.md'
        signature=[handoff.stat().st_mtime_ns,handoff.stat().st_size] if handoff.exists() else None
        prior=state.get(key,{})
        stalled=(prior.get('stalled',0)+1 if prior.get('signature')==signature else 0)
        if stalled>=max(1,int(config.get('max_stalled_auto_continuations',2))):
            # Reset the counter on an explicit future user turn or new handoff.
            state[key]={'signature':signature,'stalled':stalled,'paused_no_progress':True}
            atomic_write_json(statefile,state)
            register_question(root,agent,
                'The agent stopped after repeated continuations without updating the handoff. '
                'Please review the session and give it a next step.',
                source='watchdog_auto_continue_stall')
            return {}
        state[key]={'signature':signature,'stalled':stalled,'paused_no_progress':False}
        atomic_write_json(statefile,state)
        if premature_quota_wait((payload or {}).get('last_assistant_message')):
            return {'decision':'block','reason':premature_wait_correction(info.get('used_percent'))}
        directive=('CONTINUE WITH NEXT TASK. AUTOMATIC WORKFLOW CONTINUATION. A milestone has finished, not the quota window. '
                   'Read docs/HANDOFF.md and immediately work on the next highest-priority '
                   'safe, locally actionable task. Verify changes, keep the handoff current, '
                   'and proceed to further tasks without asking for a new prompt. '
                   'Do not deploy, push, commit, change live state, or perform operations '
                   'requiring my authorisation. If you need approval, state the exact question in '
                   'a USER_DECISION_REQUEST: line; do not perform the gated action. '
                   'If no actionable tasks remain or user input is genuinely needed, explain the blocker and stop. '
                   'If at 80% used, prepare the final handoff and choose only bounded tasks; '
                   'at 90% used, stop development and finish the handoff.')
        return {'decision':'block','reason':directive}
    if event=='SessionStart':
        context=('STANDING REPOSITORY WORKFLOW: Read docs/WORKFLOW.md and docs/HANDOFF.md '
                 'before coding. At a provider handover, first give the user a short summary of '
                 'what is done, in progress, next, and blocked. Verify Git status. '
                 'Then CONTINUE WITH NEXT TASK and keep going through actionable work. '
                 'The watchdog automatically checks usage; never deploy without authorisation. '+work_policy())
        if outage_text:context+=' '+outage_text
        elif condition=='handoff' and not info.get('stale'): context+=' '+PROMPT_HANDOFF
        elif condition=='prepare':context+=' '+PROMPT_PREP
        return {'hookSpecificOutput':{'hookEventName':event,'additionalContext':context}}
    if event=='UserPromptSubmit' and session_id:
        # Explicit user input resumes the managed agent; do not silently reissue the
        # previous question while its answer is already in the incoming prompt.
        worker=latest_worker(root)
        if worker.get('agent')==agent and worker.get('outcome')=='launched':
            if not is_automatic_continuation((payload or {}).get('prompt')):
                clear_question(root,agent)
        statefile=Path(root)/'.agent-watchdog'/'auto-continue-state.json'
        state=load_json(statefile,{}) or {}
        key=f'{agent}:{session_id}'
        if state.get(key,{}).get('paused_no_progress'):
            state.pop(key,None)
            atomic_write_json(statefile,state)
    if event=='UserPromptSubmit' and resume_confirmed:
        # This context is authoritative runtime evidence to the agent, not
        # another model-authored "continue" prompt. An old pause in the
        # conversation is not proof of a *current* user pause.
        return {'hookSpecificOutput': {'hookEventName':'UserPromptSubmit',
                                       'additionalContext':RESUME_CONTEXT}}
    if event not in ('PreToolUse','PostToolUse','UserPromptSubmit'):return {}
    if info.get('stale') and not outage_text:return {}
    if not outage_text and condition not in ('prepare','handoff'):return {}
    # Limit repeated messages to the same session to avoid using up the remaining context.
    notif=Path(root)/'.agent-watchdog'/f'notified-{agent}.json'
    data=load_json(notif,{}) or {}
    unique=str(session_id or 'unknown')
    prior=data.get(unique) or {}
    effective_stage='outage:'+outage_read(root,agent).get('state','') if outage_text else condition
    interval=45 if (condition=='handoff' or outage_text) else 180
    if prior.get('stage')==effective_stage and time.time()-prior.get('timestamp',0)<interval:
        return {}
    data[unique]={'stage':effective_stage,'timestamp':time.time()}
    atomic_write_json(notif,data)
    msg=outage_text if outage_text else (PROMPT_HANDOFF if condition=='handoff' else PROMPT_PREP)
    if outage_text:
        msg+=' [Usage unverified; no percentage inferred.]'
    else:
        msg+=f' [Detected {info.get("used_percent", "?")}% used; limiting metric {info.get("limiting_metric", "unknown")}].'
    return {'hookSpecificOutput':{'hookEventName':event,'additionalContext':msg}}


def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--agent',choices=('claude','codex'),required=True)
    p.add_argument('--project',required=True);args=p.parse_args(argv)
    try:payload=json.loads(sys.stdin.read() or '{}')
    except json.JSONDecodeError:payload={}
    event=payload.get('hook_event_name','')
    try:result=hook(args.project,args.agent,event,payload.get('session_id'),payload)
    except Exception:result={} # Never break an agent on watchdog errors.
    print(json.dumps(result))
if __name__=='__main__':main()
