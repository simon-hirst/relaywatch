#!/usr/bin/env bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project="${1:-$PWD}"
command -v python3 >/dev/null || { echo 'Python 3 is required' >&2; exit 2; }
command -v tmux >/dev/null || { echo 'tmux is required. Install it using your OS package manager.' >&2; exit 2; }
python3 "$here/install.py" --project "$project" "${@:2}"
