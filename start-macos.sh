#!/usr/bin/env bash
# Run the installed monitor on macOS. First argument is the project directory.
set -euo pipefail
here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project="${1:-$PWD}"
if [[ ${2:-} == --auto-update ]]; then
  exec "$here/relaywatch.sh" --project "$project" auto-update
fi
exec "$here/relaywatch.sh" --project "$project" run
