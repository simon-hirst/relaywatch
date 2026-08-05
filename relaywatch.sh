#!/usr/bin/env bash
set -euo pipefail
project="$PWD"
if [[ ${1:-} == --project ]]; then
  [[ $# -ge 2 ]] || { echo 'Missing --project path' >&2; exit 2; }
  project="$2"; shift 2
fi
command="${1:-run}"; shift || true
case "$command" in
  models|model-status)
    action=choose; [[ $command == model-status ]] && action=show
    exec python3 "$project/.agent-watchdog/model_picker.py" "$action" "$@" ;;
  *) exec python3 "$project/.agent-watchdog/watchdog.py" "$command" "$@" ;;
esac
