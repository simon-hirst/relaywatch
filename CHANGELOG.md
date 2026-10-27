# Changelog

## v3.18 — First public source release (2026-10-27)

- Release the watchdog at the last internal version, **3.18**, rather than starting a separate public version sequence.
- Support Windows via WSL2 and native macOS/Linux installs with matching platform launchers.
- Add explicit opt-in for automatic agent launches, unrestricted CLI flags and phone notifications.
- Keep verified quota handoffs, pause/resume, live model presets and decision notifications.
- Handle background waits and security-restriction notices without repeated continuation loops.
- Organise setup, troubleshooting and tests consistently with the companion usage-monitor project.
- Add cross-platform Python CI, version-tag source archives and SHA-256 checksums.

The public packaging work does not change the 80% preparation or 90% handoff thresholds. This is a source release, not a compiled application. The original repository history contains reconstructed commits; see [HISTORY.md](HISTORY.md).
