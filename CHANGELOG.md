# Changelog

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
