"""Conservative restoration of lost watchdog worker metadata.

Only a uniquely identified *live* interactive CLI in this project's own tmux
session can be adopted. This module does not type, kill, attach or spawn anything.
"""
from __future__ import annotations
import os
import subprocess
import time
from pathlib import Path
from engine import atomic_write_json, load_json
from runner import session_name, tmux_exists, wd


def _descendants(pid: int, *, limit: int = 100):
    """Read-only Linux /proc traversal, bounded and cycle-safe."""
    seen = set()
    queue = [pid]
    while queue and len(seen) < limit:
        p = queue.pop(0)
        if p in seen or p <= 0:
            continue
        seen.add(p)
        try:
            children = (Path(f'/proc/{p}/task/{p}/children').read_text().split())
            queue.extend(int(c) for c in children)
        except (OSError, ValueError):
            pass
        yield p


def _interactive_identity(pid: int, root: Path):
    """Return provider only for a real interactive process in this project."""
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
        argv = [s.decode('utf-8', 'replace') for s in raw.split(b'\0') if s]
        cwd = Path(os.readlink(f'/proc/{pid}/cwd')).resolve()
    except (OSError, ValueError):
        return None
    if not argv or cwd != root:
        return None
    # The managed tmux pane and its process descendants are authoritative
    # for interactive identity. Remote-control flags may disappear from the
    # process argv after CLI upgrades, resume or native wrapper re-exec.
    # Preserve exact cwd and exclude helper/daemon commands in _active_cli_kind.
    return _active_cli_kind(argv)


def detect_managed_agent(root, *, run=None, walk=None, identify=None, expected_window=None):
    """Examine this project's known tmux session and return unambiguous evidence.

    This never adopts a window based on its display name alone. A running CLI
    process must match provider flags and the exact repository cwd.
    """
    root = Path(root).resolve()
    session = session_name(root)
    fn = run or subprocess.run
    if not tmux_exists(session, invoke=fn):
        return None, f'no tmux session named {session}'
    argv = ['tmux', 'list-panes', '-s', '-t', f'={session}', '-F',
            '#{window_id}\t#{window_name}\t#{pane_pid}\t#{pane_dead}']
    try:
        cp = fn(argv, capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f'cannot inspect tmux panes: {type(exc).__name__}: {exc}'
    if cp.returncode:
        return None, f'tmux pane inspection failed: {(cp.stderr or cp.stdout)[:180]}'
    matches=[]
    for line in (cp.stdout or '').splitlines():
        parts=line.split('\t')
        if len(parts)!=4: continue
        window,title,pid_text,dead=parts
        if dead in ('1','true') or not window.startswith('@'):continue
        # Startup verification checks only the recorded pane. A separate
        # window from an earlier agent or unrelated user session must not
        # make the recorded worker appear missing or ambiguous.
        if expected_window is not None and window != expected_window:
            continue
        try:pid=int(pid_text)
        except ValueError:continue
        identities=set()
        for child in (walk or _descendants)(pid):
            agent=(identify or _interactive_identity)(child,root)
            if agent:identities.add(agent)
        if len(identities)==1:
            matches.append({'agent':identities.pop(),'window':window,'pane_pid':pid})
        elif len(identities)>1:
            return None, f'ambiguous provider processes in {window}; no adoption'
    if len(matches)!=1:
        return None, f'found {len(matches)} verified interactive CLI processes; need exactly one'
    return {'session':session,**matches[0]}, None


def recover_worker(root, *, run=None, walk=None, identify=None):
    """Safely rebuild missing worker.json; never overwrite a conflicting record."""
    root = Path(root).resolve()
    worker_file = wd(root)/'worker.json'
    old = load_json(worker_file,{}) or {}
    if old.get('agent') in ('codex','claude','gemini'):
        return None  # Existing state is not ours to rewrite automatically.
    evidence,reason=detect_managed_agent(root,run=run,walk=walk,identify=identify)
    if not evidence:return f'RECOVERY NOT APPLIED: {reason}'
    # Double-check under a lock: the real launcher may have started meanwhile.
    from runner import launch_lock
    with launch_lock(root):
        current=load_json(worker_file,{}) or {}
        if current.get('agent') in ('claude','codex','gemini'):
            return None
        worker={'type':'tmux','session':evidence['session'],
                'agent':evidence['agent'],'window':evidence['window'],
                'event_id':'recovered-'+str(int(time.time())),
                'started_at':time.time(),'outcome':'launched','pid':None,
                'recovered':True}
        atomic_write_json(worker_file,worker)
    return (f'RECOVERED {evidence["agent"]} managed tmux worker in '
            f'{evidence["session"]} window {evidence["window"]}; '
            'no processes were interrupted')


def _active_cli_kind(argv):
    """Identify real coding CLIs, excluding background helper binaries.

    Checking `startswith("codex")` is unsafe: `codex-code-mode-host` is
    a helper process, not an interactive agent. Likewise `codex app-server`
    is the remote daemon, not a coding session. Be conservative about actual
    CLI entrypoints; return None for unknown processes.
    """
    if not argv:
        return None
    executable = Path(argv[0]).name.lower()
    arguments = [arg.lower() for arg in argv[1:]]
    if executable in ('codex', 'codex.exe'):
        if arguments and arguments[0] in ('app-server', 'remote-control', 'mcp-server', 'completion', '--version', '-v', '--help', '-h'):
            return None
        return 'codex'
    if executable in ('agy','agy.exe'):
        # Poller commands (agy -p /usage, --version) are *not* interactive.
        if arguments and arguments[0] in ('-p','--print','--version','-v','--help','-h','remote-control','models','update','doctor'):
            return None
        return 'gemini'
    if executable in ('claude', 'claude.exe'):
        # Claude's persistent background PTY host shares the repository cwd
        # but is not an interactive editing agent (e.g. `claude bg-pty-host`).
        if arguments and arguments[0] in ('bg-pty-host', '--bg-pty-host', 'mcp', 'doctor', 'install', 'update', '--version', '-v', '--help', '-h'):
            return None
        return 'claude'
    if executable in ('node', 'node.exe') and len(argv) > 1:
        # Real npm-installed CLI scripts, not a project JS file whose name
        # merely contains the string "codex" or "claude".
        script = argv[1].replace('\\', '/').lower()
        script_name = Path(script).name
        if script_name in ('codex', 'codex.js') or ('/@openai/codex/' in script and script_name in ('cli.js', 'codex.mjs')):
            if len(arguments) > 1 and arguments[1] in ('app-server', 'remote-control', 'mcp-server', 'completion', '--version', '-v', '--help', '-h'):
                return None
            return 'codex'
        if script_name in ('claude', 'claude.js') or ('/@anthropic-ai/claude-code/' in script and script_name in ('cli.js', 'claude.mjs')):
            if len(arguments) > 1 and arguments[1] in ('bg-pty-host', '--bg-pty-host', 'mcp', 'doctor', 'install', 'update', '--version', '-v', '--help', '-h'):
                return None
            return 'claude'
    return None


def active_repository_agents(root, *, proc_root=Path('/proc')):
    """Find real coding agents in the checkout, not unrelated helper services.

    The function is read-only. An actual Claude or Codex process still blocks
    orphan-handoff acknowledgement until the user verifies it is no longer
    working. Never terminate anything here.
    """
    root=Path(root).resolve()
    found=[]
    try:
        entries=proc_root.iterdir()
    except OSError:
        return ['process inspection unavailable']  # fail safely
    for entry in entries:
        if not entry.name.isdigit(): continue
        try:
            cwd=Path(os.readlink(entry/'cwd')).resolve()
            if cwd!=root: continue
            argv=[x.decode('utf-8','replace') for x in (entry/'cmdline').read_bytes().split(b'\0') if x]
        except (OSError,ValueError): continue
        kind=_active_cli_kind(argv)
        if kind:
            found.append(f'{kind} PID {entry.name}')
    return found
