#!/usr/bin/env bash
set -euo pipefail
session="${1:?expected tmux session}"; agent="${2:-codex}"
[[ "$session" =~ ^agent-(handoff|gemini)-[a-f0-9]{10}$ ]] || { echo 'Invalid managed session' >&2; exit 2; }
command -v tmux >/dev/null || { echo 'tmux not installed' >&2; exit 2; }
case "$(uname -s)" in
 Darwin)
   # macOS Terminal executes a shell command; the session name is strictly validated.
   osascript -e 'on run argv' -e 'tell application "Terminal" to do script "tmux attach-session -t " & (item 1 of argv)' -e 'end run' "$session" >/dev/null
   ;;
 Linux)
   if command -v x-terminal-emulator >/dev/null; then
      x-terminal-emulator -e tmux attach-session -t "$session" >/dev/null 2>&1 &
   elif command -v gnome-terminal >/dev/null; then
      gnome-terminal -- tmux attach-session -t "$session" >/dev/null 2>&1 &
   elif command -v konsole >/dev/null; then
      konsole -e tmux attach-session -t "$session" >/dev/null 2>&1 &
   else
      echo "No supported terminal emulator. Run: tmux attach -t $session" >&2; exit 3
   fi
   ;;
 *) echo 'Unsupported OS: attach to tmux manually' >&2; exit 3 ;;
esac
