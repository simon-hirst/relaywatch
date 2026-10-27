#!/usr/bin/env python3
"""Package the tracked source tree for a version-tag release.

The source archives are not standalone executables. Runtime configuration,
notification topics and CLI credentials must never be included in a release.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build(version: str, output_dir: Path, *, root: Path = ROOT) -> list[Path]:
    if not re.fullmatch(r"v\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?", version):
        raise ValueError('Version must look like v3.18 or v3.18.0')
    output_dir.mkdir(parents=True, exist_ok=True)
    label = f'relaywatch-{version}'
    assets = []
    for kind, extension in (('zip', '.zip'), ('tar.gz', '.tar.gz')):
        path = output_dir / f'{label}{extension}'
        subprocess.run(
            ['git', 'archive', f'--format={kind}', f'--prefix={label}/',
             f'--output={path}', 'HEAD'],
            cwd=root, check=True,
        )
        assets.append(path)
    manifest = output_dir / 'SHA256SUMS.txt'
    manifest.write_text(''.join(
        f'{hashlib.sha256(asset.read_bytes()).hexdigest()}  {asset.name}\n'
        for asset in assets
    ), encoding='utf-8')
    return [*assets, manifest]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True, help='Release tag, e.g. v3.18')
    parser.add_argument('--output', type=Path, default=ROOT / 'release-assets')
    args = parser.parse_args()
    for asset in build(args.version, args.output):
        print(asset)


if __name__ == '__main__':
    main()
