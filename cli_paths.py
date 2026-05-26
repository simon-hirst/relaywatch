"""Resolve agent CLIs in WSL even when launched with no interactive NVM setup.

The watchdog runs via `wsl.exe ... python3`, so shell PATH initialization from
.bashrc, nvm.sh or a tmux login shell cannot be assumed. Return a concrete
executable path for subprocess.run rather than executing a user shell.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path


def _node_version(path: Path) -> tuple[int, ...]:
    """Sort known NVM folders numerically; not lexical (v9 vs v24)."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", path.name)
    return tuple(map(int, match.groups())) if match else (-1, -1, -1)


def find_cli(command: str, *, home: Path | None = None, path: str | None = None) -> str | None:
    """Locate a trusted CLI binary from the user's normal install locations.

    No globs outside the local user's home; no command evaluation, shell startup,
    project-local shims, or PATH modification to the daemon. PATH takes priority
    when the watchdog environment was already correctly configured.
    """
    if command not in ('claude', 'codex'):
        raise ValueError('Only known agent CLIs may be resolved')
    found = shutil.which(command, path=path)
    if found:
        return found
    home = Path(home).expanduser() if home is not None else Path.home()
    candidates = [
        home / '.local' / 'bin' / command,
        home / '.npm-global' / 'bin' / command,
        home / '.bun' / 'bin' / command,
        home / '.volta' / 'bin' / command,
    ]
    nvm = home / '.nvm' / 'versions' / 'node'
    if nvm.is_dir():
        versions = sorted((p for p in nvm.iterdir() if p.is_dir()), key=_node_version, reverse=True)
        candidates += [v / 'bin' / command for v in versions]
    # These are system-wide install locations, not relative project paths.
    candidates += [Path('/usr/local/bin') / command, Path('/usr/bin') / command]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def guidance(command: str) -> str:
    return (f'{command} executable unavailable to watchdog process; looked in PATH, '
            '~/.local/bin, ~/.npm-global/bin, ~/.bun/bin, ~/.volta/bin '
            'and ~/.nvm/versions/node/*/bin. Verify in Debian with '
            f'`bash -ic "command -v {command}"`.')


def cli_environment(executable: str, *, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Ensure an NVM-installed CLI's `#!/usr/bin/env node` resolves correctly.

    A concrete path to `codex` is not enough when the Codex wrapper uses Node
    via env(1); the non-interactive watchdog can be missing both bin entries.
    Never modify the global watchdog process environment.
    """
    env = dict(os.environ if environ is None else environ)
    prefixes = [str(Path(executable).parent)]
    if not shutil.which('node', path=os.pathsep.join(prefixes + [env.get('PATH','')])):
        home = Path.home()
        nvm = home / '.nvm' / 'versions' / 'node'
        if nvm.is_dir():
            versions = sorted((p for p in nvm.iterdir() if p.is_dir()),
                              key=_node_version, reverse=True)
            for version in versions:
                if (version / 'bin' / 'node').is_file():
                    prefixes.append(str(version / 'bin'))
                    break
    env['PATH'] = os.pathsep.join(prefixes + [env.get('PATH','')])
    return env
