# Changelog

## v1.0.1 — Public release consistency update (2026-10-27)

- Align installation and platform-specific launch instructions with the companion usage project.
- Add matching Windows, macOS and Linux start scripts without replacing existing entrypoints.
- Run the Python test suite across both supported CI Python versions on Windows, macOS and Linux.
- Add a version-tag source release workflow and SHA-256 manifest. No native binaries are implied.
- Tidy the public-release tests and use the RelayWatch HTTP user agent.
- Leave quota thresholds, pause/resume, approvals and provider handoff behaviour unchanged.

## v1.0.0 — Initial public source release (2026-10-27)

- Port the local quota monitor to native Linux/macOS with WSL2 support on Windows.
- Add portable setup, desktop notifications, live model presets and guarded handoffs.
- Keep automatic launching, unrestricted CLI modes and phone push disabled by default.
- Preserve the existing managed-session recovery and decision safety checks.

The original release's 43 earlier commits were assembled retrospectively. See [HISTORY.md](HISTORY.md).
