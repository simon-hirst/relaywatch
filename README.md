# RelayWatch

A cross-platform watchdog for Claude Code and Codex. Track usage limits, hand off work before a quota runs out, and get notified when an agent needs your decision.

**Windows (WSL2) · macOS · Linux**

[**Download the latest release**](https://github.com/simon-hirst/relaywatch/releases/latest) · [Report an issue](https://github.com/simon-hirst/relaywatch/issues)

## Overview

RelayWatch runs locally alongside your coding CLI sessions. It monitors verified five-hour and weekly quota readings and can coordinate an orderly handoff between providers when a session approaches its limit.

- **Quota-aware:** live usage and local reset times, without invented allowances or reset dates.
- **Handoffs:** prepare at 80% used; complete and verify the handoff at 90% used.
- **Codex delivery:** steer live threshold instructions through the verified local Codex daemon when supported, otherwise queue them and clearly report the delay.
- **Questions:** desktop and optional ntfy phone notifications when approval or input is required.
- **Safe pauses:** a user pause takes priority over automatic continuation.
- **Live presets:** change the model and effort for the *next* launch using **Shift+M**.
- **Terminal-based:** the current agent stays in an interactive `tmux` session; there is no hosted service.

**New installations monitor only by default.** Automatic agent launches and permission-bypass flags require explicit opt-in. RelayWatch does not answer approval requests, commit changes or deploy on your behalf.

## Architecture

The monitor and both CLIs run in the same Unix environment: native Linux/macOS, or WSL2 on Windows.

| Component | Source | Description |
| --- | --- | --- |
| Supervisor | [`watchdog.py`](watchdog.py), [`engine.py`](engine.py) | Polls quotas and coordinates verified handoffs. |
| Interactive sessions | [`runner.py`](runner.py), [`interactive-agent.sh`](interactive-agent.sh) | Starts and observes CLI sessions through `tmux`. |
| Hooks and decisions | [`hook.py`](hook.py), [`pause_control.py`](pause_control.py) | Detects questions, pauses, background work and handoff readiness. |
| Usage and reset times | [`usage.py`](usage.py), [`reset_times.py`](reset_times.py) | Validates local readings and formats their reported reset instants. |
| Notifications | [`alerts.py`](alerts.py) | Windows/macOS/Linux desktop alerts and optional ntfy push. |
| Tests and releases | [`.github/workflows/`](.github/workflows/) | CI checks and tagged source archives with SHA-256 checksums. |

RelayWatch reads existing **local** usage APIs on `127.0.0.1:3789/get` (Codex) and `127.0.0.1:3790/get` (Claude). Those backends are **not included**: supply compatible readers, such as the ones in [Provider Usage Tray](https://github.com/simon-hirst/agent-usage), and authenticate both CLIs yourself.

## Download and run

Download the [latest source release](https://github.com/simon-hirst/relaywatch/releases/latest) or clone this repository. These are **source distributions, not prebuilt executables**. You'll need Python 3.10+, `tmux`, and logged-in Claude Code/Codex CLIs.

| Platform | Install into your project | Start monitoring |
| --- | --- | --- |
| Windows 10/11 (WSL2) | `./install-windows.ps1 -ProjectPath "C:\path\to\project" -Distro Debian` | `./start-windows.ps1 -ProjectPath "C:\path\to\project" -UseSavedModels` |
| macOS | `./install.sh /path/to/project` | `./start-macos.sh /path/to/project` |
| Linux | `./install.sh /path/to/project` | `./start-linux.sh /path/to/project` |

Run the Windows commands in **PowerShell**. Install Claude, Codex, Python 3 and `tmux` inside the chosen WSL2 distro (Debian by default), not in Windows Python. On macOS/Linux the native CLI logins are used. Install `tmux` with Homebrew or your Linux package manager.

Install hooks before starting the monitor. The installer preserves unrelated project instructions, existing handoffs and session state; it keeps generated runtime files out of source control. It does **not** take control of an existing agent until the user opts in.

### Enable automatic handoffs

The default configuration tracks quotas without starting another agent. To opt in explicitly:

```bash
# macOS or Linux
./install.sh /path/to/project --enable-auto-launch
```

```powershell
# Windows PowerShell
./install-windows.ps1 -ProjectPath "C:\path\to\project" -EnableAutoLaunch
```

You can also opt into unrestricted CLI execution with `--unrestricted` or `-Unrestricted`. **Only use this for repositories you trust.** Permission-bypass flags change the CLI's security posture, and written agent instructions are not a security boundary.

### Usage and controls

| Command | What it does |
| --- | --- |
| `run` | Monitor continuously. |
| `once` | Read the current quota limits. |
| `status` | Inspect the active worker, handoffs, questions and alert delivery. |
| `models` | Choose model/effort presets for the next session. |
| `pause`, `unpause`, `resume` | Hold automatic work, clear the hold, or safely resume an idle worker. |
| `resume-check` | Inspect whether continuation is safe, without sending a message. |
| `notify-setup`, `notify-test` | Configure and test desktop/phone notifications. |

On macOS/Linux use `./relaywatch.sh --project /path/to/project COMMAND`. On Windows use `./watchdog-windows.ps1 -ProjectPath "C:\path\to\project" -Command COMMAND`.

The monitor never assumes that a provider has quota when telemetry is unavailable. It won't override a genuine approval request, start concurrent work during a user pause, or restart an already-running background measurement just to keep a conversation active. Codex Daybreak restrictions generate a reminder, not a blind model or permission toggle. Codex's normal `queue` command **does not deliver into an ongoing turn**. RelayWatch attempts immediate `turn/steer` only for a verified watchdog-owned thread, with an acknowledged running turn on the local daemon; it otherwise queues. A queue success is recorded as *queued*, not *acted on*, and RelayWatch never duplicates an unconfirmed steering request. This applies to RelayWatch's 80%/90% instructions—not arbitrary messages you send from Codex on your phone. Choose **Steer** in the Codex mobile interface when you want your own follow-up to affect the currently running task.

### Claude permission and subagent alerts

RelayWatch alerts on supported Claude Code native user-attention events, including
**PermissionRequest** (tool approval), **Notification: agent_needs_input**
(background agents and agent-team prompts), and **Elicitation** (MCP forms and
authentication). The delayed `permission_prompt` notification provides a
fallback, with duplicate suppression. These alerts do not change permission
modes or answer dialogs. Sensitive command arguments, forms, URLs and secrets
are never included in notification bodies. Ordinary subagent progress and
completion are not treated as approval requests.

For **answering from your phone**, use Claude Code's own
[Remote Control](https://code.claude.com/docs/en/remote-control) on the managed
Claude session. In the Claude terminal run `/remote-control` (or `/rc`),
then open the connected session under **Code** in the Claude mobile app.
Under Claude Code `/config`, enable **Push when actions required** to receive
Claude's own mobile notifications. RelayWatch can also send its separate
optional ntfy phone alerts; those alerts do not directly accept answers.
Remote Control must be available for your Claude account and connected
to this actual session. A remote-only or cloud session not managed by
RelayWatch is not monitored by its local hooks.

### Gemini 4 Argon (Antigravity CLI, opt-in)

RelayWatch supports **Gemini 4 Argon High and Medium** in the official
[Google Antigravity CLI](https://antigravity.google/docs/install?tab=cli)
(`agy`), **not** the older Gemini CLI. Install and sign in to `agy` inside
your **Debian WSL** distro (or your native Linux/macOS environment) first,
then check `agy models` includes `gemini-4-argon-high`.

A Gemini session is started **manually and exclusively**. You must finish
Claude/Codex's current task and verified handoff, then exit the previous
managed CLI/tmux session before starting Gemini. RelayWatch refuses to launch
Gemini while another managed coding session or process is still active.

```powershell
# From the RelayWatch repo in Windows PowerShell:
.\watchdog-windows.ps1 -ProjectPath "C:\path\to\project" -Command launch-gemini -GeminiModel high
.\watchdog-windows.ps1 -ProjectPath "C:\path\to\project" -Command show-gemini
.\watchdog-windows.ps1 -ProjectPath "C:\path\to\project" -Command gemini-status
```

Use `-GeminiModel medium` for the medium-thinking variant. The managed
`agy` session opens with `--remote-control` and a handoff-reading initial
prompt. It is **interactive**, stays in a separately named tmux session, and
preserves the terminal for diagnostics when `agy` exits. The agent is not
granted any permission bypass. Antigravity Remote Control can display the
session on a phone or laptop and send its own browser notifications.

**Current limitation:** Antigravity documents quota status in an interactive
`/usage` panel, but no stable machine-readable quota endpoint is documented.
RelayWatch therefore displays Gemini quota as **unknown**, and **does not**
automatically hand off into or out of Gemini at 80/90%. While the Gemini
session exists it suspends Claude/Codex automatic launches to prevent
concurrent edits. To resume the two-agent rotation, finish the Gemini handoff
and exit the Gemini terminal/session. No quota values are guessed.

### Automatically install new releases

Auto-update is **opt-in**. Once enabled, RelayWatch checks the latest published
GitHub release every 15 minutes. It downloads the matching source ZIP and
`SHA256SUMS.txt`, verifies the SHA-256 digest, and updates only the installed
watchdog files. It then restarts **the polling watcher**, leaving the current
Claude/Codex `tmux` session running and preserving your handoff documents,
model presets, notification settings and pause state. A failed download or
checksum check does not interrupt monitoring.

Run the installer for **v3.18.2 or later once** to install the updater, then
launch it using the same project folder as before:

```powershell
# Windows (PowerShell); runs inside your WSL2 distro
./start-windows.ps1 -ProjectPath "C:\path\to\project" -UseSavedModels -AutoUpdate
```

```bash
# macOS or Linux
./start-macos.sh /path/to/project --auto-update
./start-linux.sh /path/to/project --auto-update
```

Do not start a second normal watchdog alongside the updater. `Ctrl+C` stops the
supervisor and the polling watcher, **not the coding CLI**. Releases are
installed only if the managed watchdog code has not been modified locally;
for modified files, review and install updates manually. Updates require access
to GitHub and are applied only after a verified release is available.

**Security:** Enabling automatic updates authorises running future code from
this repository without another prompt. Enable it only if you trust its
maintainers and GitHub release infrastructure. This is not an operating-system
service: it works only while the supervisor terminal remains running, and an
update does not restart the computer or recover a dead terminal.

## Development

Run from source with Python 3.10+:

```bash
# Full supervisor suite, in Linux, macOS or WSL2
python3 -m unittest discover -s tests -v

# Native Windows portability tests (supervision runs in WSL2)
python -m unittest discover -s tests -p 'test_portable_windows.py' -v
python -m unittest discover -s tests -p 'test_release_packaging.py' -v
```

The [CI workflow](.github/workflows/ci.yml) runs the Unix supervisor suite on Linux/macOS, Windows-native smoke tests on Windows, and Python syntax checks on all platforms. Version tags (`v*`) publish [source archives](.github/workflows/release.yml) and SHA-256 checksums, not native application binaries.

## Contributing

Issues and pull requests are welcome, especially for CLI compatibility, supported terminal environments, and notification reliability. Include the operating system, CLI versions and relevant error output, and **remove tokens, session identifiers and other secrets** from logs before sharing them.

MIT licensed. RelayWatch is an independent project, unaffiliated with the providers it monitors. See [SECURITY.md](SECURITY.md) for trust boundaries, [CHANGELOG.md](CHANGELOG.md) for changes and [HISTORY.md](HISTORY.md) for the disclosed reconstructed history of earlier commits.
