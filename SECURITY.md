# Security policy

Report suspected vulnerabilities privately through the repository host's security advisory feature once the repository is published.

## Trust boundaries

- The relay is a local subprocess supervisor, **not** an authentication or permissions system.
- Automatic and unrestricted launch modes are disabled by default in a new install. The unrestricted opt-in bypasses interactive CLI permission prompts, including filesystem and shell access. Only enable it for repositories and users you trust.
- Local JSON endpoints can contain usage metadata: bind them to loopback, protect access and do not expose them publicly.
- Never publish CLI auth data, environment files, ntfy topic names, secrets, session transcripts or `.agent-watchdog/` runtime JSON.
- The relay never approves user decisions, production changes or credential use automatically.
- Review external project hooks before installing: they execute with your local account permissions.

## Supported deployment shape

Windows uses WSL2, while macOS/Linux run native POSIX processes. Network push goes to a configured HTTPS ntfy server only when enabled.
