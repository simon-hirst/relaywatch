#!/usr/bin/env bash
# tmux-owned, real interactive TUI. Always launch from the project root.
set -euo pipefail
agent="${1:?expected claude|codex}"
root="${2:?expected project root}"
prompt_file="${3:?expected prompt file}"
cd "$root"
# tmux and WSL may inherit a stale PATH even when an interactive login shell
# can find Claude. Include the officially documented Claude native install path
# before attempting to launch either CLI. Do not source arbitrary shell files.
export PATH="$HOME/.local/bin:$HOME/.claude/local:$HOME/.npm-global/bin:$HOME/.bun/bin:$PATH"
# NVM-installed CLIs may not be on PATH in a detached tmux or an
# external WSL watchdog. Prefer the current shell's version; otherwise try
# installed versions in descending numeric order.
if ! type -P "$agent" >/dev/null 2>&1; then
  while IFS= read -r node_bin; do
    if [[ -x "$node_bin/$agent" ]]; then
      export PATH="$node_bin:$PATH"
      break
    fi
  done < <(find "$HOME/.nvm/versions/node" -mindepth 2 -maxdepth 2 -type d -name bin -print 2>/dev/null | sort -r)
fi
if [[ ! -f "$prompt_file" ]]; then
  printf 'Handoff prompt missing: %s\n' "$prompt_file" >&2
  exit 2
fi
prompt="$(cat "$prompt_file")"
# Read models selected when the watchdog was started. Never eval JSON as shell.
selection="$(python3 "$root/.agent-watchdog/model_picker.py" print --agent "$agent")" || exit 4
IFS=$'\t' read -r model effort <<< "$selection"
if [[ ! "$model" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{1,100}$ ]] || [[ ! "$effort" =~ ^(default|low|medium|high|xhigh|max|ultra|none|minimal)$ ]]; then
  printf 'Invalid runtime model selection: %q / %q\n' "$model" "$effort" >&2
  exit 4
fi
printf '\n=== Managed session: %s ===\nProject: %s\n' "$agent" "$PWD"
# Public default is to preserve the CLI's permission prompts. Repository owners
# may opt in to unrestricted mode in their local, gitignored configuration.
unrestricted="$(python3 - "$root/.agent-watchdog/config.json" <<'PYCONF'
import json,sys
try:
    data=json.load(open(sys.argv[1],encoding='utf-8'))
    print('yes' if data.get('unrestricted_mode') is True else 'no')
except (OSError, ValueError):
    print('no')
PYCONF
)"
case "$agent" in
  codex)
    # A shared managed app-server is needed for phone remote control.
    # This command is idempotent and leaves the daemon running.
    if ! codex remote-control start; then
      printf 'Codex remote-control daemon failed; not falling back to a non-remote session.\n' >&2
      exit 3
    fi
    printf 'Codex: %s / %s; interactive TUI, --remote unix:// (permissions from local config)\n' "$model" "$effort"
    # Older macOS Bash (3.2) rejects expansion of an empty array under set -u.
    # Build the two permission modes explicitly instead of using empty arrays.
    if [[ "$unrestricted" == yes ]]; then
      if [[ "$effort" == default ]]; then
        exec codex --remote unix:// --cd "$PWD" --model "$model" --yolo "$prompt"
      fi
      exec codex --remote unix:// --cd "$PWD" --model "$model" -c "model_reasoning_effort=\"$effort\"" --yolo "$prompt"
    fi
    if [[ "$effort" == default ]]; then
      exec codex --remote unix:// --cd "$PWD" --model "$model" "$prompt"
    fi
    exec codex --remote unix:// --cd "$PWD" --model "$model" -c "model_reasoning_effort=\"$effort\"" "$prompt"
    ;;
  claude)
    claude_binary="$(type -P claude || true)"
    if [[ -z "$claude_binary" || ! -x "$claude_binary" ]]; then
      printf '\n[watchdog] Claude Code was not found in Debian WSL.\n' >&2
      printf 'Checked the inherited PATH and common native/npm installation directories.\n' >&2
      printf 'Verify:  ls -l ~/.local/bin/claude; command -v claude\n' >&2
      printf 'If absent, install Claude Code INSIDE Debian WSL (not Windows):\n' >&2
      printf '  curl -fsSL https://claude.ai/install.sh | bash\n' >&2
      printf 'After installing, retry in this retained terminal:\n' >&2
      printf '  bash .agent-watchdog/interactive-agent.sh claude "$PWD" .agent-watchdog/next-agent-prompt.txt\n' >&2
      exit 127
    fi
    printf 'Resolved Claude CLI: %s\n' "$claude_binary"
    printf 'Claude: %s / %s; interactive TUI, --rc (permissions from local config)\n' "$model" "$effort"
    # --rc accepts an optional SESSION NAME, not an initial prompt. Supplying
    # the entire handoff as a positional argument causes an empty Claude turn
    # and can lead to remote session creation HTTP 400. Start Remote Control
    # without a positional argument; safely submit the actual user instruction
    # once tmux shows an idle Claude composer.
    if [[ -n "${TMUX_PANE:-}" ]]; then
      python3 "$root/.agent-watchdog/claude_startup.py" "$root" "$TMUX_PANE" \
        >/dev/null 2>&1 </dev/null &
      disown || true
      printf 'Initial handoff prompt: queued for verified interactive input (pane %s).\n' "$TMUX_PANE"
    else
      printf '[watchdog] No tmux pane detected; Claude will open WITHOUT an automatic first message.\n' >&2
      printf 'Paste: Read docs/HANDOFF.md and continue with the next task.\n' >&2
    fi
    if [[ "$unrestricted" == yes ]]; then
      if [[ "$effort" == default ]]; then
        exec claude --model "$model" --dangerously-skip-permissions --rc
      fi
      exec claude --model "$model" --effort "$effort" --dangerously-skip-permissions --rc
    fi
    if [[ "$effort" == default ]]; then
      exec claude --model "$model" --rc
    fi
    exec claude --model "$model" --effort "$effort" --rc
    ;;
  *)
    printf 'Invalid agent: %s\n' "$agent" >&2
    exit 2
    ;;
esac
