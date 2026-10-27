# RelayWatch

A local **Windows, macOS and Linux** watchdog for Claude Code and Codex sessions. It reads the two local usage APIs, prepares a handoff as quotas run low and can switch to the other provider after a verified handoff. Desktop and optional phone alerts tell you when an agent needs an answer.

**The default installation only monitors.** Automatic launching and unrestricted CLI flags are opt-in. Review the workspace and the existing project instructions before enabling them.

## Download and run

Download the **v3.18** source archive from **GitHub → Releases** or clone the repository. Python 3.10+ and `tmux` are required. Both CLIs must already be installed and signed in wherever RelayWatch runs.

| Operating system | Where it runs | How to start after installation |
| --- | --- | --- |
| Windows 10/11 | WSL2 (`Debian` by default) | `./start-windows.ps1 -ProjectPath "C:\path\to\project" -UseSavedModels` |
| macOS | Native terminal | `./start-macos.sh /path/to/project` |
| Linux | Native terminal | `./start-linux.sh /path/to/project` |

On Windows, start commands above are run in **PowerShell**, and you need a configured WSL2 distribution. The Windows launcher opens a separate PowerShell window running the monitor; the managed agent uses a WSL `tmux` session. On macOS, install `tmux` with Homebrew if needed; on Linux, use your distribution's package manager.

The launchers do **not** install hooks automatically. Install RelayWatch into the project first, as shown below. macOS/Linux launch and alert paths have automated tests, but haven't been verified on every native terminal.

## Install into a project

Run from the extracted repository directory. The installer adds managed hooks and workflow sections without replacing unrelated project instructions.

| OS | Command |
| --- | --- |
| Windows (PowerShell) | `./install-windows.ps1 -ProjectPath "C:\path\to\project" -Distro Debian` |
| macOS / Linux | `./install.sh /path/to/project` |

Set up the two usage sources before enabling automatic switching. The default local endpoints are `http://127.0.0.1:3789/get` for Codex and `http://127.0.0.1:3790/get` for Claude. RelayWatch **does not bundle** the servers; provide compatible endpoints from your own usage readers (not included in this repository).

Check the current quotas and select the model presets:

```bash
./relaywatch.sh --project /path/to/project once
./relaywatch.sh --project /path/to/project models
```

For Windows PowerShell:

```powershell
./watchdog-windows.ps1 -ProjectPath "C:\path\to\project" -Command once
./watchdog-windows.ps1 -ProjectPath "C:\path\to\project" -Command models
```

### Automatic handoffs (optional)

To allow RelayWatch to launch the next agent after a verified handoff, reinstall with explicit opt-in:

```bash
./install.sh /path/to/project --enable-auto-launch
```

For Windows, pass `-EnableAutoLaunch` to `install-windows.ps1`. An additional `--unrestricted` / `-Unrestricted` option enables CLI permission-bypass flags. **Only enable unrestricted mode in repositories you trust.** Agent instructions to avoid pushes, commits and deployments are not a security boundary.

New installs store configuration in the target project's `.agent-watchdog/config.json`. You can change the local usage endpoints, notification settings and launch policy there. The installer excludes generated configuration, session state, handoffs and notification topics from the project's Git tracking.

## Usage and controls

- **Quota monitoring:** the watchdog polls both five-hour and weekly allowances every 20 seconds, using only verified values. Missing reset timestamps are displayed as unknown.
- **80% used:** tell the active agent to prepare its handoff and session report while it finishes bounded, safe local work.
- **90% used:** stop feature work and finish the handoff. The next provider starts only once the handoff is complete and its quota permits it.
- **Questions:** a formal `USER_DECISION_REQUEST:` or `NEEDS_USER_INPUT:` triggers desktop notification and, if configured, an ntfy push. Answers and approvals are never assumed.
- **Background jobs:** an agent can wait for a running external test without being mistaken for a user question.
- **Pause/resume:** a persistent marker blocks automatic work until you explicitly resume; normal task completion is not a reason to override a pause.
- **Live models:** press **Shift+M** in the monitoring terminal to change the next launch's model/effort without restarting the watchdog.
- **Daybreak:** Codex restriction notices trigger a guarded recovery reminder. RelayWatch does not blindly toggle Daybreak or change an existing interactive model.

| Command | What it does |
| --- | --- |
| `run` | Monitor continuously |
| `once` | Fetch and print the current quotas |
| `status` | Show worker status, handoffs, questions and notification results |
| `models` | Choose the next model and effort presets |
| `pause` | Block automatic continuation and new launches |
| `resume-check` | Check whether the existing session can safely resume |
| `resume` | Clear the pause and attempt guarded continuation |
| `notify-setup` | Configure a private ntfy topic |
| `notify-test` | Check desktop and phone notification delivery |

Run these with `./relaywatch.sh --project PATH COMMAND` on macOS/Linux, or `./watchdog-windows.ps1 -ProjectPath PATH -Command COMMAND` in PowerShell. Use `./start-linux.sh`, `./start-macos.sh` or `./start-windows.ps1` to launch the continuous monitor.

## Privacy and limitations

- Runtime config, notification topics, CLI authentication and project handoff documents stay **out of this source repository**. Do not commit files from a project's `.agent-watchdog/` folder.
- ntfy is opt-in. Subscribe to the generated private topic on your phone before enabling delivery.
- Windows uses WSL2 rather than a native Windows Python daemon. Codex Remote Control requires a working local Unix socket; Claude Remote Control uses its own CLI.
- Hook-based enforcement only runs when the relevant hook runs. RelayWatch cannot guarantee it will interrupt an already-running process or override a provider's own limits.
- Quota endpoints and interactive CLI capabilities can change between provider releases. See [SECURITY.md](SECURITY.md) for trust boundaries.

## Running from source (developers)

No third-party Python packages are needed for the monitor or test suite.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
```

The [GitHub Actions test workflow](.github/workflows/ci.yml) runs the suite on Windows, macOS and Linux. Version tags also publish source archives and SHA-256 checksums; these are **not** standalone executables. See [CHANGELOG.md](CHANGELOG.md) for release notes and [HISTORY.md](HISTORY.md) for the explicitly reconstructed historical commit metadata.

MIT licensed. Unaffiliated with the listed CLI providers.
