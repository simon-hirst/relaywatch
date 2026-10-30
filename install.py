#!/usr/bin/env python3
"""Idempotent repository installer; never clobbers existing agent instructions."""
from __future__ import annotations
import argparse,hashlib,json,re,shlex,shutil,sys,secrets
from pathlib import Path

SOURCE=Path(__file__).resolve().parent
BEGIN='<!-- BEGIN AGENT-HANDOFF-WATCHDOG (managed) -->'
END='<!-- END AGENT-HANDOFF-WATCHDOG (managed) -->'

CLAUDE_SECTION=f'''{BEGIN}
**Standing multi-agent workflow:** Before doing any development, read and follow
`docs/WORKFLOW.md` and then `docs/HANDOFF.md`. Verify Git/diff/test
state, resume the next unfinished task, and obey the usage watchdog lifecycle
hooks. At 80% usage prepare the handoff; at 90% stop new implementation and
finish the handoff, write a dated session summary in `docs/session-reports/`, and explicitly acknowledge completion with
`python3 .agent-watchdog/watchdog.py ready --agent <your-agent>` as the last tool action.
The Stop hook is an additional automatic acknowledgement. If the user says PAUSE/WAIT/HOLD/STOP, suspend automatic continuation and tool calls; a human must explicitly run `python3 .agent-watchdog/watchdog.py resume` to lift the pause. When a fresh resume is confirmed by the UserPromptSubmit hook and the pause marker is absent, PRIOR pause messages are historical and do not prohibit work. Never treat an auto-injected continue as a resume. When the quota is healthy and local tasks remain, CONTINUE WITH NEXT TASK
through successive tasks without stopping after each milestone. Never wait for a quota reset while verified usage is below 90% and safe bounded work remains. At 80-89% used, prepare the handoff while completing small safe tasks. Don't burn tokens to hit 90%; at 90% stop coding and yield. Only the watchdog waits for quota recovery after the handoff. First report
completed / current / next / blockers at every agent handover. If genuinely
blocked and unable to do another safe task, write the structured blocker
file `.agent-watchdog/needs-input.json` and end your response with
`NEEDS_USER_INPUT: <exact question>` to request desktop and phone alerts.
For ANY other approval or decision, including while you continue safe LOCAL
work, emit a standalone `USER_DECISION_REQUEST: <exact question>` line in your
user-visible response. Prefer a native formal question UI when supported, but
always emit that marker too. Do not bury requests as "Decision for you: ..."
or assume that continuing local work answers the question. The watchdog will
alert and hold its Stop-hook auto-continuation until the user responds.
If waiting for an already-running background job/watch to report, without
needing human action, use `WAITING_ON_BACKGROUND: <job and expected signal>`
instead of `NEEDS_USER_INPUT:`. This is NOT a permission request. The Stop
hook will let that wait stand rather than force polling or send a false question alert.
Once the result is verified, write `BACKGROUND_JOB_COMPLETE: <job and observed result>` to release the wait. Never deploy,
push, or change live settings without
explicit authorization. Keep existing project-specific restrictions.
{END}
'''

SHARED='''# Claude Code ↔ Codex persistent workflow

This file governs work by Claude Code and Codex in this repository.

## Startup — every session
1. Read `docs/HANDOFF.md`; follow links to the current source plan or archived handoff.
2. Inspect `git status`, recent changes and touched files. Confirm what is implemented, what was verified, and what remains. Do not trust historical claims blindly.
3. At the start of EACH provider handover, give the user a concise, user-visible plan summary BEFORE modifying files: what is complete (with verification), what is in progress, which task is next, what follows it, and any blockers/permissions. Then say **Continue with next task** and do so.
4. Continue successive highest-priority actionable items without asking the user to restate an existing plan. Once verified, continue to the next actionable item automatically while quota allows; a task milestone alone is not a stop reason.
5. A direct user pause/hold/wait/stop request takes precedence over all automatic continuation instructions, even if the agent has healthy quota. `.agent-watchdog/PAUSE_AUTO_CONTINUE` is a persistent pause latch; automatic work, tool calls and handoff launches remain blocked until the user explicitly runs `python3 .agent-watchdog/watchdog.py resume` (or the Windows wrapper). A model-generated continuation can never remove it. After an explicit CLI resume and a hook-confirmed message, earlier pause messages are superseded; do not demand that the user resume again unless the marker is present or a new pause occurs. Already-running commands may need manual interruption.
6. Follow existing architecture and safety constraints. Never discard other agents' changes.

## Limits and automatic handoff
- The WSL-local watchdog reads `http://localhost:3790/get` for Claude and `http://localhost:3789/get` for Codex.
- At **20% capacity remaining** (80% used), start preparing and updating the shared handoff and dated summary. Keep performing SHORT safe verifiable local work while above the 90%-used handoff threshold; do not start long tasks. Do NOT wait for the quota to reset just to conserve capacity. Never waste tokens artificially to hit the threshold.
- At **10% capacity remaining** (90% used), stop feature work, update and verify handoff files, then run `python3 .agent-watchdog/watchdog.py ready --agent codex` (or `claude`) as your LAST tool command and end the turn. The Stop hook also acknowledges completion. Do NOT signal ready until the handoff is complete. If a substantial task would risk exhausting the remaining quota, focus on small verification and handoff upkeep rather than idly waiting for a reset.
- Native lifecycle hooks supply threshold instructions automatically (only when they execute). The standalone watchdog also polls, and at 80% AND 90% attempts to queue explicit instructions into an unambiguously identified watchdog-managed Codex thread. Quota logs alone do NOT mean delivery succeeded; check the queue result, and trust project hooks.
- If Claude telemetry fails, never invent percentages. After 60 seconds of outage checkpoint the handoff and avoid long new tasks. After 180 seconds stop new implementation, update the handoff AND a dated session summary, acknowledge ready only when both are verified, and allow the healthy provider to take over. Native hooks enforce this when they run; if no hooks fire, the watchdog alerts the user rather than force-switching. A restored API immediately cancels the outage instructions.
- Use the most restrictive **verified** session or weekly usage measurement. The JSON parser does not infer units from arbitrary bare numbers.
- Below 90% used, if safe local work remains, keep working rather than voluntarily parking the agent until a quota reset. Quota recovery waiting belongs to the polling watchdog only AFTER the verified 90% handoff; if blocked on a real decision, paused by the user, or telemetry is unverified, respect those safeguards.
- Re-check limits at milestones; don't rely on these hooks as an exact hard quota enforcement mechanism.

## Handoff: `docs/HANDOFF.md`
Update **this same file** instead of creating new `NEXT-CLAUDE-*` / `NEXT-CODEX-*` entry points.
Archive a timestamped snapshot in `docs/session-handoffs/` when completing the final handoff.
Write a **separate session work report** under `docs/session-reports/YYYY-MM-DDTHH-MM-SSZ-claude.md` or `...-codex.md` for EACH completed session. Summarise exactly what THIS agent did during its turn, files changed, verified tests, known failures, next steps; link to the shared handoff. Keep it concise and redact secrets. The Stop hook requires a fresh summary at final handoff.
Your handoff MUST include:
1. Current goal, scope, constraints, acceptance criteria.
2. What was implemented, touched files, decisions and reasons.
3. **Verified** test commands/results, failures, unknowns (never invent checks).
4. Current Git branch/commit, uncommitted state, unfinished operations.
5. Reproducible bugs, failed approaches, blockers and dependencies.
6. Ordered next tasks with exact files/commands/verification guidance.
7. Security and operational constraints, including project-specific requirements if relevant.
8. Which provider should resume next; first instructions for that agent.
9. The next 3–5 prioritized LOCAL tasks, each with exact file paths or commands,
   measurable acceptance criteria, and whether explicit permission is required.
   If no safe tasks remain, list the actual blockers rather than inventing work.

Keep the handoff thorough but navigable. Distinguish facts from assumptions. Verify the saved files exist. Avoid automatic commits, pushes, destructive cleanups, changing settings or deployments without authorization. Do not expose API keys/credentials in handoff documents.

## When you need the human
- Prefer an independent safe task instead of waiting if one exists. An approval
  request can be made *before* such work, but does not authorise the action.
- **Every** permission/approval/choice question for the user, even if you can
  continue other safe local tasks, must include a standalone line:
  `USER_DECISION_REQUEST: <exact question>` (one concrete question, no secrets).
  Prefer the CLI's formal question UI when supported, but still emit the marker
  or write the structured question file so the watchdog can alert. The Stop
  hook will not auto-continue over an unanswered decision. Never hide a
  permission question only inside a progress paragraph.
- If **no safe local progress** is possible without a decision, write `.agent-watchdog/needs-input.json` as UTF-8 JSON: `{"agent":"claude|codex","question":"one specific question","status":"open"}`. No credentials or secrets. The watchdog will notify desktop and the subscribed phone topic.
- End the final response with one literal line: `NEEDS_USER_INPUT: <your exact question>` so the Stop hook can also record the blocker if you cannot write the file. This marker denotes a blocking decision; the general `USER_DECISION_REQUEST:` marker covers approval requests made while other local work remains.
- If an external/background job is running, and no safe independent work is available,
  report `WAITING_ON_BACKGROUND: <the job and expected completion signal>` in your final response.
  This is **not** a human question: never use `NEEDS_USER_INPUT` for waiting on a job.
  Don't repeatedly poll or consume quota; allow the CLI's background completion notification
  to resume the work. Once confirmed, report `BACKGROUND_JOB_COMPLETE: <job and observed result>`
  so the Stop hook can distinguish completion from a checkpoint tool call. Keep user approval for subsequent external operations separate.
- Do not continue automatically while an unresolved blocker is open. Wait for a reply in the interactive/remote session; when it arrives, the hook marks the request answered and work resumes.
- The notification cannot make the user reply from ntfy directly: it points to the active Claude/Codex session.

## Parallel work
Claude and Codex should not edit the same checkout simultaneously without coordination. Preserve uncommitted changes. If multiple sessions are active, do not assume the most recent file modification belongs to you. Never deploy or alter production settings as a side effect of a handoff.

## When done
Update the handoff as needed even if quotas are healthy, run targeted tests, inspect diff, and report exact status. A new session should need only: `continue`.
'''

DECISION_PROTOCOL = """<!-- BEGIN WATCHDOG-DECISION-PROTOCOL (managed) -->
## Formal user decisions and watchdog alerts
Whenever you ask the user to authorise an action, approve a change, or choose
between options, emit a standalone user-visible line:
`USER_DECISION_REQUEST: <one exact question>`.
Use `NEEDS_USER_INPUT: <one exact question>` instead when no independent safe
work is possible and the decision blocks further work. Do this even if a
native interactive question tool is used. Never hide a decision only in prose
such as "Decision for you: can I ...?". Do not take the gated action until the
user explicitly approves it. Safe local work may continue during the same
turn, but Stop-hook auto-continuation will not bury an unanswered decision.
The watchdog recognises unambiguous plain-language decision headings as a
fallback and notifies the user, but the formal marker is the reliable path.
A background test/run/watch that is still executing is NOT a user decision.
When you must await its own completion notification, emit
`WAITING_ON_BACKGROUND: <job name and expected signal>` instead of
`NEEDS_USER_INPUT:`. After the job actually completes, emit
`BACKGROUND_JOB_COMPLETE: <job name and verified result>` so the watchdog
can release the wait latch without guessing from a checkpoint or incidental tool call. Don't manufacture a question to avoid waiting and don't
spin on status checks. An existing user approval requirement stays separate.
## Authorized security work and temporary Codex Daybreak model selection
If Codex refuses a cybersecurity task with "This content can’t be shown",
DO NOT simply treat it as completed work and don't repeatedly auto-continue.
The watchdog can detect and notify about this restriction, but it cannot
safely toggle an interactive `/daybreak` (which is an ON/OFF toggle) or select
`/model` for you. For work already authorized within the approved Daybreak
scope, ask the human to enable `/daybreak` if OFF and temporarily select
`gpt-6-sol` at MAX reasoning effort.
The next real user-submitted prompt inside Codex (including from the phone)
automatically releases the watchdog's acknowledgment latch; no PowerShell
command is required. This does NOT establish Daybreak eligibility, prove the
CLI setting is ON, or authorize new work. A repeat refusal must stop the loop. Do not extend this
approval to other work, external systems, deployments or pilot changes.
After the security task, explicitly output
`DAYBREAK_TASK_COMPLETE: <what was completed and verified>` so the watchdog
requests restoration, and do not start unrelated work until the human confirms
that `/daybreak` was turned OFF and `/model` was returned to the earlier choice.
The user can confirm in the Codex conversation from their phone with
`Daybreak is off and previous model/effort restored`; PowerShell is not required.
The watchdog stores only the saved launch preset; the human must check any
in-session overrides and restore the ACTUAL previous model and effort.
<!-- END WATCHDOG-DECISION-PROTOCOL (managed) -->"""


def update_decision_protocol(path):
    """Upgrade existing workflow documents without rewriting user content."""
    original = path.read_text(encoding='utf-8')
    begin = '<!-- BEGIN WATCHDOG-DECISION-PROTOCOL (managed) -->'
    end = '<!-- END WATCHDOG-DECISION-PROTOCOL (managed) -->'
    if begin in original:
        if end not in original:
            raise ValueError(f'Unclosed decision protocol in {path}')
        updated = re.sub(re.escape(begin)+r'.*?'+re.escape(end),
                         DECISION_PROTOCOL.strip(), original, flags=re.S)
    else:
        updated = original.rstrip()+'\n\n'+DECISION_PROTOCOL+'\n'
    if updated != original:
        path.write_text(updated, encoding='utf-8')

HANDOFF='''# Active agent handoff

**State:** Bootstrap placeholder; not a verified implementation handoff yet. The receiving agent must inspect the repository before treating any plan as done.

## Original plan / historical context
{legacy}

## Current objective and acceptance criteria
- Determine from the original plan and current Git state; write concrete criteria here.

## Verified completed work
- No implementation verification performed by the installer.

## Current repository state
- Agent: verify branch, latest relevant commit, staged/unstaged/untracked files, and their owners.

## Tests and failures
- No project tests run by the installer; capture actual checks here.

## Remaining tasks (priority order)
1. Read the original plan/historical handoff referenced above.
2. Confirm repository state and identify exact next implementation steps.
3. Implement and verify the highest-priority task, updating this handoff.

## Operational restrictions
- No deployment/push/production changes without explicit authorization.
- Keep all original system-specific requirements intact.

## Next receiving agent
- Read `docs/WORKFLOW.md` and this file, verify claims, and continue.
'''

README_POINTER='''# Agent handoff watchdog

This folder was installed by the portable Claude Code ↔ Codex watchdog package.
Run `python3 .agent-watchdog/watchdog.py run` from the repo root to poll continuously.
The native hooks also check quotas without the external polling process.
Run `python3 .agent-watchdog/watchdog.py inspect --agent claude` (or `codex`)
to see JSON *paths*, without exposing token values. Edit `config.json` to set an
explicit `used_pct_path` if automatic detection is inconclusive.
Use `python3 .agent-watchdog/watchdog.py once` as a quick health check.
Run `python3 .agent-watchdog/watchdog.py notify-setup` for Android ntfy topic.
Run `python3 .agent-watchdog/watchdog.py notify-test` to test desktop and phone.
'''


def append_managed(path,block):
    path.parent.mkdir(parents=True,exist_ok=True)
    original=path.read_text(encoding='utf-8') if path.exists() else ''
    if BEGIN in original:
        if END not in original: raise ValueError(f'Unclosed managed block in {path}, refusing to edit')
        updated=re.sub(re.escape(BEGIN)+r'.*?'+re.escape(END),block.strip(),original,flags=re.S)
    else:updated=original.rstrip()+'\n\n'+block if original else block
    if original!=updated:path.write_text(updated,encoding='utf-8')


def merge_hook_file(path,hook_cmd,events):
    if path.exists():
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or not isinstance(data.get('hooks',{}),dict):raise ValueError(f'Invalid existing hooks file {path}')
    else:data={'hooks':{}}
    hooks=data['hooks']
    for event in events:
        entries=hooks.setdefault(event,[])
        # Preserve other hooks and any other settings.
        matching=[e for e in entries if isinstance(e,dict) and any('agent-watchdog/hook.py' in h.get('command','') for h in e.get('hooks',[]) if isinstance(h,dict))]
        for e in matching:entries.remove(e)
        entries.append({'hooks':[{'type':'command','command':hook_cmd,'timeout':15}]})
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')


def merge_gemini_hooks_file(path, hook_cmd):
    """Install only RelayWatch's named Antigravity workspace hook.

    Preserve unrelated workspace hooks and never edit global permissions.
    Agy's hook schema is a named dictionary, unlike Claude's hooks object.
    """
    if path.exists():
        data=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data,dict):
            raise ValueError(f'Invalid existing Antigravity hooks file {path}')
    else:
        data={}
    key='relaywatch-gemini-attention'
    prior=data.get(key)
    if prior is not None and (
            not isinstance(prior,dict)
            or '.agent-watchdog/gemini_attention.py' not in json.dumps(prior)):
        raise ValueError(f'Existing Antigravity hook {key} belongs to another integration')
    def entry(event, matcher=None):
        command=hook_cmd+' --event '+event
        obj={'hooks':[{'type':'command','command':command,'timeout':12}]}
        if matcher is not None:
            obj['matcher']=matcher
        return obj
    matcher=r'^(ask_question|ask_permission)$'
    data[key]={
        'PreToolUse':[entry('PreToolUse',matcher)],
        'PostToolUse':[entry('PostToolUse',matcher)],
        'PreInvocation':[entry('PreInvocation')]}
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n',
                    encoding='utf-8')


def historical_handoff(root):
    files=[]
    for p in root.glob('docs/*.md'):
        if p.name in ('HANDOFF.md','WORKFLOW.md'):continue
        n=p.name.lower()
        if any(w in n for w in ('handoff','next-claude','next-codex','founders-polish')):files.append(p)
    if not files:
        for p in root.glob('*.md'):
            if any(w in p.name.lower() for w in ('handoff','next-claude','next-codex')):files.append(p)
    files.sort(key=lambda p:p.stat().st_mtime,reverse=True)
    if files:return '\n'.join('- Historical file to verify: `'+str(p.relative_to(root))+'`' for p in files[:5])
    return '- No historical handoff found; identify the project plan from existing docs and Git history.'


def install(project):
    root=project.resolve()
    if not root.is_dir():raise ValueError(f'Project folder does not exist: {root}')
    dest=root/'.agent-watchdog';dest.mkdir(exist_ok=True)
    for fname in ('engine.py','hook.py','usage.py','watchdog.py','runner.py','alerts.py','desktop-alert.ps1','interactive-agent.sh','claude_startup.py','open-agent-window.ps1','open-agent-window.sh','model_picker.py','capacity.py','session_recovery.py','quota_outage.py','session_summary.py','console_ui.py','cli_paths.py','codex_steer.py','reset_times.py','pause_control.py','resume_agent.py','quota_behavior.py','codex_questions.py','claude_questions.py','claude_attention.py','gemini_session.py','gemini_usage.py','gemini_attention.py','background_wait.py','daybreak_watch.py','update_supervisor.py','VERSION'):
        shutil.copy2(SOURCE/fname,dest/fname)
        # A Windows Git checkout may turn Bash LF endings into CRLF; this
        # makes 'set -o pipefail' fail before a managed agent can start.
        # Normalize *only* managed shell scripts before computing the ledger.
        if fname.endswith('.sh'):
            script=dest/fname
            raw=script.read_bytes()
            if b'\r\n' in raw:
                script.write_bytes(raw.replace(b'\r\n',b'\n'))
    # Updater uses this ledger to avoid overwriting locally modified managed code.
    code_files = ('engine.py','hook.py','usage.py','watchdog.py','runner.py','alerts.py',
                  'desktop-alert.ps1','interactive-agent.sh','claude_startup.py',
                  'open-agent-window.ps1','open-agent-window.sh','model_picker.py',
                  'capacity.py','session_recovery.py','quota_outage.py','session_summary.py',
                  'console_ui.py','cli_paths.py','codex_steer.py','reset_times.py',
                  'pause_control.py','resume_agent.py','quota_behavior.py',
                  'codex_questions.py','claude_questions.py','claude_attention.py','gemini_session.py','gemini_usage.py','gemini_attention.py','background_wait.py',
                  'daybreak_watch.py','update_supervisor.py','VERSION')
    ledger = {name: hashlib.sha256((dest / name).read_bytes()).hexdigest() for name in code_files}
    (dest/'managed-files.json').write_text(json.dumps(ledger,indent=2,sort_keys=True)+'\n',encoding='utf-8')
    (dest/'README.md').write_text(README_POINTER,encoding='utf-8')
    config_file=dest/'config.json'
    if not config_file.exists():
        config={'agents':{
           'claude':{'url':'http://localhost:3790/get','used_pct_path':None},
           'codex':{'url':'http://localhost:3789/get','used_pct_path':None}},
           'prepare_remaining_pct':20,'handoff_remaining_pct':10,
           'unrestricted_mode':False, 'cache_seconds':20,'stale_grace_seconds':120,
           'telemetry_checkpoint_seconds':60,'telemetry_pause_seconds':180,
           'enable_codex_queue':True,'active_session_seconds':120,
           'auto_launch_next_agent':False,'auto_launch_gemini_fallback':True,'auto_launch_grace_seconds':10,
           'auto_open_terminal_window':True,'wsl_distro':__import__('os').environ.get('WSL_DISTRO_NAME','Debian'),
           'auto_continue_turns':True, 'max_stalled_auto_continuations':2,
           'capacity_recovery': {'enabled':True,'stable_seconds':20,'max_retries':2,'delays_seconds':[30,90]},
           'alert_retry_seconds':120, 'alerts': {'desktop':True,'phone':{'enabled':False,'server':'https://ntfy.sh', 'topic':'relaywatch-'+secrets.token_hex(16),'include_question':False}}}
        config_file.write_text(json.dumps(config,indent=2)+'\n',encoding='utf-8')
    # Existing local config must be upgraded non-destructively.
    config=json.loads(config_file.read_text(encoding='utf-8'))
    changed=False
    for key,value in [('auto_launch_next_agent',False),('auto_launch_gemini_fallback',True),('auto_launch_grace_seconds',10),('handoff_alert_after_seconds',300),('auto_open_terminal_window',True),('wsl_distro','Debian'),('auto_continue_turns',True),('max_stalled_auto_continuations',2),('alert_retry_seconds',120),('telemetry_checkpoint_seconds',60),('telemetry_pause_seconds',180),('capacity_recovery',{'enabled':True,'stable_seconds':20,'max_retries':2,'delays_seconds':[30,90]})]:
        if key not in config:config[key]=value;changed=True
    if 'alerts' not in config:
        config['alerts']={'desktop':True,'phone':{'enabled':False,'server':'https://ntfy.sh','topic':'relaywatch-'+secrets.token_hex(16),'include_question':False}}
        changed=True
    if changed:config_file.write_text(json.dumps(config,indent=2)+'\n',encoding='utf-8')
    doc=root/'docs';doc.mkdir(exist_ok=True)
    workflow=doc/'WORKFLOW.md'
    if not workflow.exists():workflow.write_text(SHARED,encoding='utf-8')
    update_decision_protocol(workflow)
    auto_note='''
<!-- BEGIN AUTO-AGENT-LAUNCH (managed) -->
## Automatic next-agent launch: interactive / mobile
The watchdog switches between **interactive tmux terminal sessions**, not
`codex exec` or `claude -p`. Codex runs `codex remote-control start`, then
`codex --remote unix:// --cd "$PWD" --model <selected-model> -c model_reasoning_effort=<selected-effort> --yolo`. Claude uses
`claude --model <selected-model> --effort <selected-effort> --dangerously-skip-permissions --rc`. Codex receives its initial prompt as a positional CLI argument. Claude starts with `--rc` without a positional argument (which would be interpreted as a Remote Control session name), then receives a short initial message in its verified idle `tmux` composer. Full rules remain in the project documents. Choose models and efforts at **watchdog launch time** with the interactive menu in `start-watchdog-window.ps1`. Choices are saved in `.agent-watchdog/models.json` and applied to every NEW watchdog-launched agent without running the installer again. The watcher must stay running.
VS Code: Terminal > Run Task > `RelayWatch: Attach agent (Windows WSL)` if
opened in Windows, or `RelayWatch: Attach agent (WSL)` from a WSL workspace.
The terminal can be reattached after reopening the editor. On WSL2 the
watchdog can open Windows Terminal automatically; on Linux/macOS a native
terminal emulator is used when available. Automatic opening is skipped when no
other terminal is attached. Close the window without killing the agent;
use `-Command show` to open the existing agent in a new window on demand.
Only a watchdog-owned, completed old tmux session will be switched. Unrelated
terminals are not forcibly closed. No unattended deploys, pushes, commits,
production changes are authorised.
While a managed agent has verified usage under 90% used, never wait for a quota reset merely to conserve capacity. At 80–89%, prepare handoff materials while continuing small authorised local tasks; at 90% stop and yield. Never burn tokens artificially to reach 90%. If both providers lack quota after verified handoff, the verified handoff remains pending indefinitely. Keep polling; the first provider to regain >20% capacity resumes it, including the original provider after its quota resets. If both are available, prefer the other provider. Never invent quota figures.
At each handover give a concise user-visible summary of completed/in-progress/next/blocked work, then CONTINUE WITH NEXT TASK. At each final handoff create a separate dated agent-authored summary in docs/session-reports/ covering work and verification THIS SESSION.
If Claude or Codex telemetry fails for 60 seconds checkpoint work; after 180 seconds stop for a verified handoff + session summary. This is an uncertainty fallback, NOT a claim that quota is exhausted. Never switch an agent based only on a missing API reading.
An explicit user pause wins over all automation. If paused, use `python3 .agent-watchdog/watchdog.py resume` or `watchdog-windows.ps1 -Command resume` only when the user wants to continue. Press Shift+M in the running watchdog console to change future model/effort presets; model changes do not interrupt the current agent.
Codex may report 'Selected model is at capacity' even with healthy quota. The watchdog conservatively retries a stable, idle Codex prompt up to twice and alerts the user if the problem persists. It never silently changes the selected model or interrupts an active turn.
If a user decision blocks all other safe work, record a needs-input file and signal NEEDS_USER_INPUT: so the watchdog alerts Windows desktop and ntfy on Android.
**Full access risk:** Both requested flags bypass approval prompts. Use only
in trusted projects. Codex remote daemon may fail to propagate --yolo;
verify effective permissions in the CLI if unexpected errors occur.
<!-- END AUTO-AGENT-LAUNCH (managed) -->
'''
    current_workflow=workflow.read_text(encoding='utf-8')
    begin='<!-- BEGIN AUTO-AGENT-LAUNCH (managed) -->'
    end_marker='<!-- END AUTO-AGENT-LAUNCH (managed) -->'
    if begin in current_workflow:
        a=current_workflow.index(begin)
        b=current_workflow.index(end_marker,a)+len(end_marker)
        workflow.write_text(current_workflow[:a]+auto_note.strip()+current_workflow[b:],encoding='utf-8')
    else:
        with workflow.open('a',encoding='utf-8') as f:f.write('\n'+auto_note)
    # Preserve other VS Code tasks and settings. JSONC is left unmodified.
    taskfile=root/'.vscode'/'tasks.json'
    taskfile.parent.mkdir(exist_ok=True)
    tasks={'version':'2.0.0','tasks':[]}
    if taskfile.exists():
        try:
            tasks=json.loads(taskfile.read_text(encoding='utf-8'))
            if not isinstance(tasks,dict) or not isinstance(tasks.get('tasks'),list):
                raise ValueError('Invalid tasks.json')
        except json.JSONDecodeError:
            print('Existing tasks.json uses JSONC; leaving untouched. See README for tmux attach command.')
            tasks=None
    if tasks is not None:
        from runner import session_name
        session=session_name(root)
        attach_cmd=f'cd {shlex.quote(str(root))} && tmux attach-session -t {shlex.quote(session)}'
        common={'isBackground':True,'problemMatcher':[],
                'presentation':{'reveal':'always','focus':True,'panel':'dedicated','clear':False}}
        generated=[
            {'label':'RelayWatch: Attach agent (Windows WSL)', 'type':'process',
             'command':'wsl.exe','args':['-d',config.get('wsl_distro','Debian'),'--','bash','-ic',attach_cmd],**common},
            {'label':'RelayWatch: Attach agent (WSL)', 'type':'process',
             'command':'bash','args':['-ic',attach_cmd],**common},
            {'label':'RelayWatch: Attach agent (Linux / macOS)', 'type':'process',
             'command':'tmux','args':['attach-session','-t',session],**common}
        ]
        original=[t for t in tasks['tasks'] if not (isinstance(t,dict) and t.get('label','').startswith('RelayWatch: Attach agent'))]
        tasks['tasks']=original+generated
        taskfile.write_text(json.dumps(tasks,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    handoff=doc/'HANDOFF.md'
    if not handoff.exists():handoff.write_text(HANDOFF.format(legacy=historical_handoff(root)),encoding='utf-8')
    (doc/'session-handoffs').mkdir(exist_ok=True)
    (doc/'session-reports').mkdir(exist_ok=True)
    append_managed(root/'CLAUDE.md',CLAUDE_SECTION)
    append_managed(root/'AGENTS.md',CLAUDE_SECTION)
    q=shlex.quote
    for agent,path in (('claude',root/'.claude'/'settings.local.json'),('codex',root/'.codex'/'hooks.json')):
        cmd=f'python3 {q(str(dest / "hook.py"))} --agent {agent} --project {q(str(root))}'
        events=('SessionStart','PreToolUse','PostToolUse','UserPromptSubmit','Stop')
        if agent=='claude':
            events+=('PermissionRequest','Notification','Elicitation')
        merge_hook_file(path,cmd,events)
    # Project-scoped Agy hooks. Preserve unrelated user hooks/permission
    # policy and let Agy's own permission engine decide any approval.
    agy_cmd=f'python3 {q(str(dest / "gemini_attention.py"))} --project {q(str(root))}'
    merge_gemini_hooks_file(root/'.agents'/'hooks.json',agy_cmd)
    # Do not forcibly overwrite project-level user model preferences on install.
    # Model/effort choices for *managed* launches are selected at runtime by
    # model_picker.py, stored separately from project/CLI global settings.
    # Existing v1.8 project defaults are left untouched on upgrade.
    ignore=root/'.gitignore'
    block='''# Local RelayWatch runtime (never commit usage/state logs)
.agent-watchdog/config.json
.agent-watchdog/managed-files.json
.agent-watchdog/auto-update.lock
.agent-watchdog/models.json
.agent-watchdog/model-catalog-cache.json
.agent-watchdog/usage-*.json
.agent-watchdog/notified-*.json
.agent-watchdog/active-codex.json
.agent-watchdog/codex-queued.json
.agent-watchdog/missing-handoff-alert.json
.agent-watchdog/ready-*.json
.agent-watchdog/worker.json
.agent-watchdog/launch-history.json
.agent-watchdog/launch.lock
.agent-watchdog/logs/
.agent-watchdog/next-agent-prompt.txt
.agent-watchdog/auto-continue-state.json
.agent-watchdog/PAUSE_AUTO_CONTINUE
.agent-watchdog/pause-state.json
.agent-watchdog/resume-delivery.json
.agent-watchdog/needs-input.json
.agent-watchdog/alert-delivery.json
.agent-watchdog/codex-questions.json
.agent-watchdog/codex-visible-questions.json
.agent-watchdog/claude-questions.json
.agent-watchdog/claude-attention.json
.agent-watchdog/gemini-worker.json
.agent-watchdog/gemini-usage.json
.agent-watchdog/gemini-attention.json
.agent-watchdog/gemini-weekly-warning.json
.agent-watchdog/daybreak-recovery.json
.agent-watchdog/outage-claude.json
.agent-watchdog/outage-codex.json
'''
    old=ignore.read_text(encoding='utf-8') if ignore.exists() else ''
    new_lines=[line for line in block.splitlines() if line and not line.startswith('#') and line not in old.splitlines()]
    if new_lines:
        ignore.write_text(old.rstrip()+'\n\n# Agent watchdog local runtime\n'+'\n'.join(new_lines)+'\n',encoding='utf-8')
    print(f'Installed in {root}')
    print('Launch-time model selector: start-watchdog-window.ps1 (not installed defaults)')
    print('Gemini Argon fallback: automatically eligible after verified Claude/Codex handoff when BOTH lack >20% quota and fresh Argon WEEKLY quota exceeds 10%; requires agy, auto-launch enabled')
    print('Gemini warning: only at <=10% remaining weekly; no 20% warning and no forced switch out of active Argon')
    print('Missing-worker recovery: automatic when a single live CLI is verified in the managed tmux session')
    print('Manual worker recovery: watchdog-windows.ps1 -Command recover-session')
    print('Exited-terminal handoff recovery: -Command ready -Agent codex -ConfirmComplete (verify handoff first)')
    print('Native Claude hooks: .claude/settings.local.json')
    print('Live model changes: press Shift+M in the watchdog console (or use -Command models)')
    print('Telemetry fallback: checkpoint after 60s unavailable; halt for verified handoff after 180s')
    print('Session summaries: docs/session-reports/ (at each completed handoff)')
    print('Native Codex hooks: .codex/hooks.json (Codex must trust this repository)')
    print('Persistent instructions: CLAUDE.md + AGENTS.md + docs/WORKFLOW.md')
    print('Initial handoff: docs/HANDOFF.md (existing handoffs preserved)')
    print('User pause: say PAUSE/WAIT or -Command pause; -Command resume safely continues an idle managed agent; -Command unpause only clears the latch')
    print('Auto-continue after completed tasks: enabled (managed agent Stop hook, with safety guards)')
    print('Final-handoff acknowledgement: native Stop hook OR python3 .agent-watchdog/watchdog.py ready --agent codex|claude')
    print('Codex model-capacity recovery: enabled for idle managed TUI (bounded retries + alerts)')
    print('User-blocker alerts: immediate native Stop alert plus live Claude/Codex question monitors; desktop + ntfy (if enabled)')
    print('Codex Daybreak refusal: notified; phone conversation auto-acknowledges retry (no PowerShell needed; no blind /daybreak injection)')
    print('Phone topic setup: python3 .agent-watchdog/watchdog.py notify-setup')
    print('Notification test: python3 .agent-watchdog/watchdog.py notify-test')
    print('Start watcher: python3 .agent-watchdog/watchdog.py run')
    print('Check quota parser: python3 .agent-watchdog/watchdog.py once')
    print('Auto-start of the next interactive tmux CLI:', 'enabled' if config.get('auto_launch_next_agent') else 'disabled', '(watcher must be running)')
    print('For existing completed Claude handoff: python3 .agent-watchdog/watchdog.py launch-codex')
    print('Terminal: uses Windows Terminal on WSL2 or a native terminal on Linux/macOS when available')
    print('Open current agent now: watchdog-windows.ps1 -Command show')
    print('Attach from VS Code: Terminal > Run Task > RelayWatch: Attach agent (Windows WSL / WSL)')
    print('Install tmux with apt, dnf, pacman or Homebrew for your platform')
    print('Models and effort choices apply to NEW managed agents, not currently running CLI sessions.')
    print('IMPORTANT: Restart Claude and Codex sessions to load updated hooks.')


def main():
    p=argparse.ArgumentParser(description='Install dual-agent handoff watchdog into existing repo')
    p.add_argument('--project',type=Path,default=Path.cwd())
    p.add_argument('--enable-auto-launch',action='store_true',help='opt in to automatic agent launches')
    p.add_argument('--unrestricted',action='store_true',help='opt in to CLI permission-bypass flags (unsafe for untrusted repositories)')
    args=p.parse_args()
    try:
        install(args.project)
        cfg_path=args.project.resolve()/'.agent-watchdog'/'config.json'
        if args.enable_auto_launch or args.unrestricted:
            cfg=json.loads(cfg_path.read_text(encoding='utf-8'))
            if args.enable_auto_launch:cfg['auto_launch_next_agent']=True
            if args.unrestricted:cfg['unrestricted_mode']=True
            cfg_path.write_text(json.dumps(cfg,indent=2)+'\n',encoding='utf-8')
            print('Opt-in settings written to gitignored project configuration.')
    except Exception as exc:print('ERROR:',exc,file=sys.stderr);sys.exit(1)
if __name__=='__main__':main()
