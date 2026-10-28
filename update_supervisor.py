#!/usr/bin/env python3
"""Opt-in release updater and watchdog-only process supervisor.

Run *inside* the project environment (WSL2 on Windows). This program owns
only the polling watchdog subprocess, never a Claude/Codex CLI or tmux server.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

RELEASE_API = 'https://api.github.com/repos/simon-hirst/relaywatch/releases/latest'
ASSET_ROOT = 'https://github.com/simon-hirst/relaywatch/releases/download/'
MAX_JSON = 200_000
MAX_ZIP = 20 * 1024 * 1024
MAX_UNPACKED = 40 * 1024 * 1024
VERSION_RE = re.compile(r'^v?(\d+)\.(\d+)(?:\.(\d+))?$')


def version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value.strip())
    if not match:
        raise ValueError('invalid release version: ' + value[:80])
    return tuple(int(i or 0) for i in match.groups())


def _get(url: str, limit: int, *, timeout: int = 12) -> bytes:
    request = urllib.request.Request(url, headers={
        'User-Agent': 'RelayWatch-release-updater',
        'Accept': 'application/vnd.github+json' if url == RELEASE_API else 'application/octet-stream',
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('download exceeds size limit')
    return data


def latest_release(current: str, *, get=_get):
    release = json.loads(get(RELEASE_API, MAX_JSON))
    if release.get('draft') or release.get('prerelease'):
        return None
    tag = release.get('tag_name', '')
    if version(tag) <= version(current):
        return None
    asset_names = {a.get('name'): a.get('browser_download_url') for a in release.get('assets', [])}
    name = f'relaywatch-{tag}.zip'
    zip_url = asset_names.get(name)
    checksum_url = asset_names.get('SHA256SUMS.txt')
    expected_prefix = f'{ASSET_ROOT}{tag}/'
    if (zip_url != expected_prefix + name
            or checksum_url != expected_prefix + 'SHA256SUMS.txt'):
        raise ValueError('release is missing expected official ZIP/checksum assets')
    return {'tag': tag, 'zip_url': zip_url, 'checksum_url': checksum_url, 'name': name}


def stage_release(release, directory: Path, *, get=_get) -> Path:
    """Verify the official checksum and inspect ZIP paths before extracting."""
    checksum = get(release['checksum_url'], 16_384).decode('utf-8')
    matches = re.findall(r'^([0-9a-fA-F]{64})\s+\*?([^\s]+)$', checksum, re.M)
    hashes = [digest.lower() for digest, name in matches if name == release['name']]
    if len(hashes) != 1:
        raise ValueError('ZIP not uniquely present in checksum manifest')
    data = get(release['zip_url'], MAX_ZIP)
    if hashlib.sha256(data).hexdigest() != hashes[0]:
        raise ValueError('release checksum mismatch')
    prefix = f'relaywatch-{release["tag"]}/'
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 500 or sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise ValueError('release archive exceeds limits')
        if not infos:
            raise ValueError('release archive is empty')
        for entry in infos:
            name = entry.filename
            remainder = name[len(prefix):] if name.startswith(prefix) else ''
            parts = PurePosixPath(remainder).parts
            # No absolute, symlink, dot-dot, or alternate root extraction.
            mode = (entry.external_attr >> 16) & 0o170000
            if (not name.startswith(prefix) or (not remainder and not entry.is_dir()) or '..' in parts
                    or name.startswith('/') or '\\' in name
                    or (mode not in (0, 0o100000, 0o040000))):
                raise ValueError('unsafe source archive entry')
        archive.extractall(directory)
    source = directory / prefix.rstrip('/')
    if (source / 'VERSION').read_text(encoding='utf-8').strip() != release['tag'].removeprefix('v'):
        raise ValueError('source VERSION does not match release tag')
    for item in ('install.py', 'watchdog.py', 'update_supervisor.py'):
        if not (source / item).is_file():
            raise ValueError('release missing required file: ' + item)
    return source


def verify_managed_files(project: Path):
    """Fail closed if a user edited managed code since the previous install."""
    installed = project / '.agent-watchdog'
    manifest = json.loads((installed / 'managed-files.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or len(manifest) < 10:
        raise ValueError('invalid installed-file ledger; reinstall manually')
    for name, digest in manifest.items():
        if not re.fullmatch(r'[a-zA-Z0-9._-]{1,100}', name):
            raise ValueError('invalid managed filename in ledger')
        target = installed / name
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f'locally modified managed file: {name} (update skipped)')
    return tuple(manifest)


def stop_watcher(child, *, grace=12):
    """Stop only the poller process; tmux workers are separate processes."""
    if child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=4)


def apply_release(source: Path, project: Path, *, run=subprocess.run):
    """Install from verified staging, restoring managed code on install error."""
    names = verify_managed_files(project)
    installed = project / '.agent-watchdog'
    with tempfile.TemporaryDirectory(prefix='relaywatch-backup-') as tmp:
        backup = Path(tmp)
        for name in (*names, 'managed-files.json'):
            shutil.copy2(installed / name, backup / name)
        try:
            cp = run([sys.executable, str(source / 'install.py'), '--project', str(project)],
                     cwd=source, timeout=90)
            if cp.returncode != 0:
                raise RuntimeError(f'installer returned {cp.returncode}')
            if (installed / 'VERSION').read_text(encoding='utf-8').strip() != (source / 'VERSION').read_text(encoding='utf-8').strip():
                raise RuntimeError('installed version mismatch')
        except Exception:
            for name in (*names, 'managed-files.json'):
                shutil.copy2(backup / name, installed / name)
            raise


def _watcher(project: Path):
    return subprocess.Popen([sys.executable, str(project / '.agent-watchdog' / 'watchdog.py'), 'run'], cwd=project)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Opt-in RelayWatch release updater')
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--check-seconds', type=int, default=900)
    args = parser.parse_args(argv)
    if args.check_seconds < 60:
        parser.error('--check-seconds must be 60 or more')
    root = args.project.resolve()
    installed = root / '.agent-watchdog'
    if not (installed / 'watchdog.py').is_file():
        parser.error('Install RelayWatch in the project first')
    try:
        verify_managed_files(root)
    except Exception as exc:
        parser.error(f'auto-update cannot start safely: {exc}')
    # The watchdog itself is not stopped for network errors or invalid releases.
    import fcntl  # Only executed under WSL2, Linux or macOS.
    with (installed / 'auto-update.lock').open('a+b') as lockfile:
        try:
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('another auto-update supervisor is already running')
        child = _watcher(root)
        next_check = time.monotonic()  # Check on first start, then periodically.
        try:
            while True:
                if child.poll() is not None:
                    return child.returncode
                if time.monotonic() < next_check:
                    time.sleep(min(1, max(0, next_check - time.monotonic())))
                    continue
                next_check = time.monotonic() + args.check_seconds
                try:
                    current = (installed / 'VERSION').read_text(encoding='utf-8').strip()
                    release = latest_release(current)
                    if release is None:
                        continue
                    print(f'RELAYWATCH UPDATE: staging verified {release["tag"]}', flush=True)
                    with tempfile.TemporaryDirectory(prefix='relaywatch-release-') as tmp:
                        source = stage_release(release, Path(tmp))
                        verify_managed_files(root)
                        stop_watcher(child)
                        try:
                            apply_release(source, root)
                        except Exception:
                            child = _watcher(root)
                            raise
                    print(f'RELAYWATCH UPDATED: {release["tag"]}; restarting watchdog only (existing CLI/tmux untouched)', flush=True)
                    # Reload the freshly installed supervisor code as well.
                    try:
                        os.execv(sys.executable, [sys.executable, str(installed / 'update_supervisor.py'),
                                                 '--project', str(root), '--check-seconds', str(args.check_seconds)])
                    except OSError:
                        # Never leave monitoring stopped because exec itself failed.
                        child = _watcher(root)
                        raise
                except Exception as exc:
                    print(f'RELAYWATCH UPDATE SKIPPED: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            return 130
        finally:
            stop_watcher(child)


if __name__ == '__main__':
    sys.exit(main())
