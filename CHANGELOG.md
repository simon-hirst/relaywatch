# Changelog

## v3.19.1 — Gemini dashboard and model selector (2026-10-30)

- Always show Gemini 4 Argon High/Medium on the watchdog dashboard, even before its optional Antigravity CLI is installed.
- Distinguish agy missing in Debian WSL, CLI present but sign-in unverified, and a currently active Gemini session; quotas remain explicitly unknown.
- Add Argon High/Medium to Shift+M and save the selection in .agent-watchdog/models.json without changing Claude/Codex selections.
- Default launch-gemini to the saved manual Argon preset; permit explicit -GeminiModel high/medium overrides.
- Keep the compact TTY footer from wrapping or losing its closing delimiter; test installation, model presets, dashboard statuses, and manual launcher selection.
- Do not start or stop existing coding sessions or change automatic Claude/Codex quota-handoff behavior.

## v3.19.0 — Optional Gemini 4 Argon / Antigravity CLI (2026-10-30)

- Add opt-in interactive Gemini 4 Argon High/Medium sessions using Google's Antigravity CLI (`agy`) with Remote Control and shared handoff documentation.
- Add Windows/WSL and native command entrypoints for `launch-gemini`, `show-gemini` and `gemini-status`, with saved tmux metadata and visible connection support.
- Prevent concurrent Gemini and Claude/Codex managed launches, preserving active work and refusing to replace or kill another CLI.
- Display Gemini as an optional managed session with unknown quota; automatic Claude/Codex switching pauses while Gemini owns the checkout.
- Add installer/auto-update manifest support and regression tests; retain existing Claude/Codex quota thresholds and handoff logic.
- Do not invent Gemini 5-hour/weekly allowance figures: the published Antigravity CLI `/usage` interface is interactive, so automatic quota-based Gemini switching remains disabled until a reliable supported API is available.

## v3.18.4 — Claude side-agent and permission prompts (2026-10-28)

- Alert for Claude Code's immediate tool permission requests, including those from managed background subagents, without automatically approving them.
- Forward delayed native notifications when an agent/team member needs input, when a permission dialog is waiting, or when an MCP form or authentication dialog requests attention.
- Detect MCP elicitation hooks immediately, with fallback notification when the prompt remains unanswered.
- Keep tool commands, form data, URLs, and other sensitive prompt contents out of desktop and phone notification bodies.
- Suppress duplicate alerts for the same prompt while allowing distinct subagent prompts through; keep these advisory alerts independent of quota-handoff blockers.
- Install and preserve the new managed Claude-only hooks through normal opt-in release auto-updates.
- Fix auto-update restart falsely marking a previously running Claude session as failed: reverify its exact tmux window, tolerate CLI wrapper re-exec, and distinguish legacy unverified sessions from genuinely failed new launches.
- Reattach an existing managed terminal when the poller restarts, without spawning a replacement coding CLI or opening a duplicate when already attached.
- Document that Claude Remote Control can be used to answer prompts from the Claude mobile app when connected; RelayWatch notifications never act on behalf of the user.

## v3.18.3 — Agent handoff and decision alerts hardening (2026-10-28)

- Fix Windows CRLF Bash launch failures by enforcing LF during installation, with launcher syntax checks before a tmux agent window is created.
- Prevent duplicate launches from outdated ready markers, and distinguish an actual interactive coding agent from a shell-only tmux window.
- Clear superseded source-agent approval prompts only after a receiving agent has truly started, without granting approval.
- Improve handoffs with prioritized actionable local tasks and measurable acceptance criteria.
- Detect Codex user-facing questions without its queued-follow-up indicator, and notify on Claude Code's native AskUserQuestion and ExitPlanMode interactions.
- Add cross-platform regression coverage for startup, handoff, native questions, and safe notifications.
- Publish checksummed version releases when VERSION changes on main; opt-in auto-updater verifies assets and restarts only the monitoring process.

## v3.18.2 — Opt-in automatic release updates (2026-10-28)

- Add a release-checking supervisor for Windows WSL2, macOS and Linux.
- Verify each tagged release ZIP against its published SHA-256 checksum before installing.
- Restart the polling watchdog after an update without restarting active Claude/Codex tmux sessions.
- Preserve model presets, alert configuration, handoff files and user pauses.
- Refuse updates when locally modified managed code would be overwritten; leave the existing watcher running on network or verification errors.
- Keep auto-update opt-in and document its security and operational limitations.


## v3.18.1 — Cross-platform CI and Codex delivery fixes (2026-10-27)

- Fix empty Bash array expansion in the interactive launchers on macOS.
- Correct macOS temporary-path test expectations for `/var` and `/private/var`.
- Run the Unix supervisor suite on supported Unix hosts, with portable native Windows smoke tests.
- Align project documentation with the companion Provider Usage Tray README.
- Steer managed Codex quota-threshold messages through its local daemon when acknowledged; report queued fallbacks explicitly and never resend an unconfirmed steer.
- Add focused steering, duplicate-prevention and native-Windows portability tests.

## v3.18 — First public source release (2026-10-27)

- Release the watchdog at the last internal version, **3.18**, rather than starting a separate public version sequence.
- Support Windows via WSL2 and native macOS/Linux installs with matching platform launchers.
- Add explicit opt-in for automatic agent launches, unrestricted CLI flags and phone notifications.
- Keep verified quota handoffs, pause/resume, live model presets and decision notifications.
- Handle background waits and security-restriction notices without repeated continuation loops.
- Organise setup, troubleshooting and tests consistently with the companion usage-monitor project.
- Add cross-platform Python CI, version-tag source archives and SHA-256 checksums.

The public packaging work does not change the 80% preparation or 90% handoff thresholds. This is a source release, not a compiled application. The original repository history contains reconstructed commits; see [HISTORY.md](HISTORY.md).
