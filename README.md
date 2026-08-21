# RelayWatch

A local quota-aware session relay for **Claude Code** and **OpenAI Codex**. It watches local usage endpoints, prepares handoff notes at 80% utilisation, and can switch the active terminal session at 90%. Optional desktop and phone alerts cover questions and blocked work.

> **Public-release safety:** Install is intentionally conservative: automatic launches, unrestricted CLI flags and phone push are **off by default**. Enable only after reviewing configuration and trusting the workspace. The original personal deployment used more permissive defaults; this package does not copy those credentials or settings.

## Platforms

| Host | Execution | Requirements |
|---|---|---|
| Windows 10/11 | WSL2 Linux distro (tested workflow: Debian) | WSL2, Python 3.10+, `tmux`, PowerShell, Windows Terminal recommended |
| Linux | Native | Python 3.10+, `tmux`, Bash, optionally `notify-send` and desktop terminal emulator |
| macOS | Native | Python 3.10+, `tmux` (`brew install tmux`), Bash, Terminal.app |

Windows runs the relay **inside WSL2**, not as a native Windows Python daemon. Both CLIs must be installed and authenticated inside the environment where the relay runs. Linux/macOS launch and alert paths are implemented but still need validation on those actual hosts; CI runs their unit tests.

## Install into an existing project

On Linux/macOS:

```bash
./install.sh /path/to/project
./relaywatch.sh --project /path/to/project once
./relaywatch.sh --project /path/to/project models
./relaywatch.sh --project /path/to/project run
```

On Windows PowerShell, from the extracted repo:

```powershell
.\install-windows.ps1 -ProjectPath "C:\path\to\your-project" -Distro Debian
.\watchdog-windows.ps1 -ProjectPath "C:\path\to\your-project" -Command models
.\start-watchdog-window.ps1 -ProjectPath "C:\path\to\your-project" -UseSavedModels
```

The installer adds `.agent-watchdog/` scripts and managed instruction sections to `CLAUDE.md`, `AGENTS.md`, and `docs/WORKFLOW.md` without overwriting other project guidance. It never imports your global CLI credentials and never includes your local configuration in this source repository.

### Optional autonomous mode

After reviewing the trust model, enable automatic managed launches with:

```bash
./install.sh /path/to/project --enable-auto-launch
```

The optional **unrestricted** flag also enables CLI permission-bypass modes (`--yolo` in Codex, `--dangerously-skip-permissions` in Claude):

```bash
./install.sh /path/to/project --enable-auto-launch --unrestricted
```

On Windows, pass `-EnableAutoLaunch` and optionally `-Unrestricted` to `install-windows.ps1`. **Do not use unrestricted mode on untrusted repositories.** External actions, deploys, pushes, production changes and commits still require explicit user authorisation in the managed instructions; model adherence is not a security boundary.

For existing installs, edit `.agent-watchdog/config.json` (gitignored): set `auto_launch_next_agent`, `unrestricted_mode`, your local usage service URLs and preferred notification settings. Set both local usage APIs to valid endpoints **before** enabling automation. The defaults expect Claude usage on `127.0.0.1:3790/get` and Codex on `127.0.0.1:3789/get`; these companion usage readers are **not bundled**.

## Workflows

- Every 20 seconds the relay reads verified five-hour/weekly utilisation from both configured JSON endpoints. Percentages and reset times are never guessed.
- At **80% used**, it asks the active agent to checkpoint progress and prepare handoff documentation. Below 90%, bounded safe work can continue.
- At **90% used**, it requires a completed handoff and session report before a safe provider switch. If both are quota-limited it waits for verified recovery.
- Questions use `USER_DECISION_REQUEST:` or `NEEDS_USER_INPUT:` markers. A live terminal monitor can also detect queued questions, then raise a local notification. Approval never occurs automatically.
- A `PAUSE_AUTO_CONTINUE` marker overrides automated work. Background-job waits are distinct from permission questions, reducing retry loops.
- Live presets can be changed with **Shift+M** in an interactive console. New presets apply on next launch; active agents are not forcibly restarted.
- Codex Daybreak restrictions are detected with a guarded recovery state, but the relay does **not** toggle `/daybreak` or change an interactive model without your action.

| Command | Purpose |
|---|---|
| `run` | Monitor continuously |
| `once` | Poll once and display usage |
| `status` | Inspect managed worker, questions, alerts, quotas |
| `models` | Choose saved model and effort |
| `pause` | Stop automatic continuation and new launches |
| `resume-check` | Inspect whether the managed worker can safely resume |
| `resume` | Clear pause, then attempt guarded continuation |
| `notify-setup` | Configure a private ntfy topic on your device |
| `notify-test` | Test desktop and optional phone delivery |

On Windows the command wrapper uses `-Command`, e.g. `-Command status`. On POSIX use `./relaywatch.sh --project PATH status`.

## Configuration and privacy

- `.agent-watchdog/config.json`, `models.json`, `usage-*.json`, notification topic and all receipts are **gitignored** in the target project by the installer. Never commit them.
- The ntfy topic is randomly generated per install and disabled by default; configure and subscribe to it explicitly before enabling push.
- Remote Control is a feature of the provider CLIs, not a RelayWatch server. Codex needs `codex remote-control start` and a working Unix socket for managed remote sessions; Claude uses its `--rc` flag.
- Never copy `.agent-watchdog/` runtime JSON, API credentials, CLI authentication directories or real project handoffs into a public repository.
- A passive quota monitor can only enforce prompts when hook events fire; it cannot guarantee zero token use or override an agent's direct action.

## Security

The tool runs subprocesses, modifies project instruction/hook files, sends local HTTP requests and can post to a configured ntfy server. Treat the installed hooks as code execution with your local user permissions. Review `SECURITY.md`, choose a private notification topic, and avoid unrestricted mode unless the checkout is trusted. Network push is opt-in.

## Development

```bash
python3 -m unittest discover -s tests -q
python3 -m compileall -q .
```

The release history in this repository is **synthetically reconstructed** for packaging and review. Author and committer timestamps were assigned retrospectively across April–October 2026; they are not evidence of historical development dates. See `HISTORY.md` for details.

MIT licensed; see [LICENSE](LICENSE). Contributions welcome via issues and pull requests.
