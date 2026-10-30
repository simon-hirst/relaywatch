"""Opt-in, conservative Antigravity CLI (Gemini 4 Argon) session support.

Google currently publishes model quotas through the interactive /usage panel,
not a stable, documented machine-readable quota API. This module deliberately
does NOT fabricate percentages or join the Claude/Codex automatic rotation.
An explicit command opens an interactive managed Gemini session, only when
no other managed TUI/process is running in this checkout.
"""
from __future__ import annotations
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from engine import atomic_write_json, load_json
from pause_control import paused
from runner import (handoff_is_real, launch_lock, latest_worker,
                    session_name, show_agent_window, tmux_exists, wd)
from session_recovery import active_repository_agents

MODEL_IDS = {'high': 'gemini-4-argon-high',
             'medium': 'gemini-4-argon-medium'}
DEFAULT_MODEL = 'high'


def cli_path(*, which=shutil.which):
    """Find the optional agy executable without launching or authenticating it.

    Windows Terminal/WSL watchdog shells may omit ~/.local/bin from PATH.
    Installed does NOT imply authenticated, licensed or model access.
    """
    path=os.pathsep.join((str(Path.home()/'.local/bin'),
                          str(Path.home()/'.npm-global/bin'),
                          str(Path.home()/'.bun/bin'),
                          os.environ.get('PATH','')))
    return which('agy', path=path)


def automatic_eligible(quota, *, min_weekly_remaining=10):
    """Only a fresh, directly reported Argon weekly bucket can fund auto fallback.

    Gemini has no assumed Pro five-hour limit. Never infer Argon availability
    from the general Gemini or Antigravity Claude/GPT buckets.
    """
    if not isinstance(quota, dict) or quota.get('fresh') is not True:
        return False
    groups=quota.get('groups')
    argon=groups.get('Gemini 4 Argon') if isinstance(groups, dict) else None
    week=argon.get('weekly') if isinstance(argon, dict) else None
    if not isinstance(week, dict):
        return False
    fraction=week.get('remaining_fraction')
    return (isinstance(week.get('id'), str) and bool(week['id'])
            and isinstance(week.get('reset_time'), str) and bool(week['reset_time'])
            and isinstance(fraction, (int,float)) and not isinstance(fraction,bool)
            and min_weekly_remaining/100 < fraction <= 1)


def auto_prompt():
    """Agent instructions for a *verified* Claude/Codex Stop-hook handoff."""
    return (
        'AUTOMATIC RELAYWATCH QUOTA HANDOFF to Gemini 4 Argon. Claude and '
        'Codex currently lack sufficient quota; the source agent completed '
        'its verified handoff. Read AGENTS.md, CLAUDE.md, docs/WORKFLOW.md, '
        'and docs/HANDOFF.md first. Check Git status and actual files, then '
        'give a concise takeover briefing and CONTINUE WITH NEXT SAFE TASK. '
        'Complete authorized local implementation and tests without waiting '
        'for Claude/Codex quotas merely to save Gemini quota. Respect all '
        'existing operational and user approval requirements. Never infer '
        'approval for commits, pushes, deployments or production changes. '
        'RelayWatch monitors the separate Argon WEEKLY quota. There is NO '
        '20% Gemini preparation warning. At 10% weekly remaining or below, '
        'write a verified docs/HANDOFF.md plus dated docs/session-reports/ '
        'summary at the next safe checkpoint; do not switch or exit until '
        'safe work is saved. Keep Remote Control on for phone questions. '
        'Do not terminate the managed session or interrupt active work '
        'because Claude or Codex quotas reset. A return handoff requires '
        'a separate verified Gemini completion; never auto-approve prompts.'
    )


def gemini_session_name(root):
    suffix = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:10]
    return f'agent-gemini-{suffix}'


def record_path(root):
    return wd(root) / 'gemini-worker.json'


def status(root, *, has_session=tmux_exists):
    record = load_json(record_path(root), {}) or {}
    name = gemini_session_name(root)
    # Automatic fallback joins the existing shared Claude/Codex session so
    # its attached Windows Terminal follows the new Gemini window. Manual
    # launch-gemini retains the original dedicated session.
    worker=latest_worker(root)
    shared=session_name(root)
    if (worker.get('agent')=='gemini' and worker.get('session')==shared
            and worker.get('type')=='tmux' and worker.get('outcome')=='launched'):
        live=has_session(shared)
        return {'state':'tmux session present' if live else 'tmux session absent',
                'model':record.get('model') or worker.get('model'),
                'window':worker.get('window'),'session':shared,
                'quota':'monitored', 'running':live, 'automatic':True}
    if record.get('session') != name:
        return {'state': 'not started', 'quota': 'unknown'}
    live = has_session(name)
    return {'state': 'tmux session present' if live else 'tmux session absent',
            'model': record.get('model'), 'window': record.get('window'),
            'session': name, 'quota': 'unknown', 'running': live}


def launch(root, config, *, model=DEFAULT_MODEL, invoke=None, which=None,
           other_sessions=active_repository_agents):
    """Create exactly one interactive Argon TUI, never a headless run.

    Never replace or terminate an existing Claude/Codex or Gemini process.
    The local project must already have a real verified handoff document;
    launching Gemini is an explicit human action (not an inferred quota reset).
    """
    root = Path(root).resolve()
    if model not in MODEL_IDS:
        return 'NOT LAUNCHED: choose Gemini Argon high or medium'
    if paused(root):
        return 'NOT LAUNCHED: user has paused RelayWatch'
    if not handoff_is_real(root):
        return 'NOT LAUNCHED: docs/HANDOFF.md is missing or still a placeholder'
    run = invoke or subprocess.run
    check_binary = which or shutil.which
    extra_path = ':'.join([str(Path.home() / '.local/bin'),
                           str(Path.home() / '.npm-global/bin'),
                           str(Path.home() / '.bun/bin'), os.environ.get('PATH', '')])
    if check_binary('tmux') is None:
        return 'NOT LAUNCHED: tmux not found in WSL/native environment'
    if check_binary('agy', path=extra_path) is None:
        return ('NOT LAUNCHED: Antigravity CLI (agy) not found. Install it and '
                'sign in inside the same Debian WSL environment, then retry.')
    if tmux_exists(session_name(root), invoke=run):
        return ('NOT LAUNCHED: Claude/Codex managed tmux session still exists. '
                'Finish its verified handoff and close that CLI/window before switching.')
    if tmux_exists(gemini_session_name(root), invoke=run):
        return ('NOT LAUNCHED: Gemini managed session already exists. '
                'Use show-gemini to reconnect; no second CLI started.')
    active = other_sessions(root)
    if active:
        return 'NOT LAUNCHED: an existing coding process may still be working: ' + ', '.join(active[:4])

    with launch_lock(root):
        # Re-check under the same lock as the existing two-agent launcher.
        if tmux_exists(session_name(root), invoke=run) or tmux_exists(gemini_session_name(root), invoke=run):
            return 'NOT LAUNCHED: another managed session appeared; refusing a duplicate'
        prompt = ('AUTOMATIC HANDOFF DOCUMENT — manually selected Gemini 4 Argon session. '
                  'Read AGENTS.md, CLAUDE.md, docs/WORKFLOW.md, and docs/HANDOFF.md; '
                  'verify files and git status before edits. Work on the next safe local task, '
                  'run tests, update docs/HANDOFF.md, and request explicit approval before '
                  'commits, pushes, deployments, live changes or destructive operations. '
                  'RelayWatch monitors your Argon WEEKLY quota. There is NO 20% '
                  'preparation warning for Gemini. At 10% WEEKLY remaining or below, '
                  'write docs/HANDOFF.md and a dated docs/session-reports/ summary '
                  'at your next safe checkpoint. The watchdog will also deliver '
                  'a one-shot native hook reminder when possible. Do not auto-switch '
                  'agents, commit, deploy or interrupt unfinished work. '
                  'Leave a clear final handoff before exiting.')
        model_id = MODEL_IDS[model]
        # -i is documented as prompt-interactive. Do not use -p/--print.
        command = ('export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:$PATH"; ' +
                   'agy --model ' + shlex.quote(model_id) +
                   ' --remote-control -i ' + shlex.quote(prompt))
        shell = (command + '; rc=$?; printf "\\n[relaywatch] Gemini exited with status %s. '
                 'Terminal retained.\\n" "$rc"; exec bash -i')
        argv = ['tmux', 'new-session', '-d', '-P', '-F', '#{window_id}',
                '-s', gemini_session_name(root), '-n', 'gemini',
                '-c', str(root), 'bash', '-lic', shell]
        try:
            cp = run(argv, capture_output=True, text=True, timeout=12)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 'FAILED to open Gemini tmux: ' + str(exc)
        if cp.returncode:
            return 'FAILED to open Gemini tmux: ' + str(cp.stderr or cp.stdout)[:250]
        window = (cp.stdout or '').strip()
        if not re.fullmatch(r'@[0-9]+', window):
            return 'FAILED: tmux did not return a valid Gemini window ID; inspect the session'
        atomic_write_json(record_path(root), {
            'agent': 'gemini', 'session': gemini_session_name(root),
            'window': window, 'model': model_id, 'started_at': time.time(),
            'quota': 'unknown', 'outcome': 'launched'})
        return ('CREATED Gemini 4 Argon (' + model + ') tmux session ' +
                gemini_session_name(root) + ' window ' + window +
                '; use show-gemini to connect. Quota status is UNKNOWN; /usage is manual.')


def show(root, config, *, force=True, invoke=None):
    worker=latest_worker(root)
    if (worker.get('agent')=='gemini' and worker.get('session')==session_name(root)
            and worker.get('outcome')=='launched'):
        return show_agent_window(root, config, 'gemini', force=force,
                                 invoke=invoke)
    name = gemini_session_name(root)
    if not tmux_exists(name, invoke=invoke or subprocess.run):
        return 'NOT OPENED: no managed Gemini tmux session exists'
    return show_agent_window(root, config, 'gemini', force=force,
                             invoke=invoke, session_override=name)


def blocks_auto_handoff(root, *, invoke=None):
    """An active Gemini tmux session must block Claude/Codex automatic launches.

    Do not inspect tmux on projects that have never explicitly launched Gemini.
    This preserves the existing two-provider code path and older test mocks.
    """
    worker=latest_worker(root)
    shared=session_name(root)
    if (worker.get('agent')=='gemini' and worker.get('session')==shared
            and worker.get('type')=='tmux' and worker.get('outcome')=='launched'):
        return tmux_exists(shared,invoke=invoke)
    record = load_json(record_path(root), {}) or {}
    session = gemini_session_name(root)
    return bool(record.get('session') == session and
                tmux_exists(session, invoke=invoke))
