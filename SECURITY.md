# Security Policy

## Reporting a vulnerability

If you believe you have found a security vulnerability in `opencode-hermes-mcp`,
please report it **privately** so it can be fixed before public disclosure:

- Open a **private vulnerability report** on this repository's GitHub
  security advisories (preferred), or
- Email **arthur.ho2tier@gmail.com** with a description of the issue and, if
  possible, reproduction steps.

Please do **not** open a public issue for security vulnerabilities.

## Scope

The controller is a NO-LLM process. Its security-relevant surface is limited:

- **OpenCode server credentials** (`OPENCODE_SERVER_URL` /
  `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD`) are injected into
  the controller's environment by the launcher
  (`~/.local/bin/opencode-mcp-launch.sh`) and are **never written to disk by
  the controller** and never logged.
- The **LLM API secret** (e.g. the Unsloth API key) is stored by the installer
  in a file with mode `600`
  (`~/.config/opencode/secrets/unsloth-api-key`) and referenced from the
  OpenCode config via `{file:...}` — it is not handled by this package at all.
- The **server credentials file** (`~/.config/hermes/opencode-server.json`) is
  created by `scripts/install.sh` with mode `600`.
- The controller persists small **durable turn-state files** under
  `~/.local/state/opencode-hermes-mcp/` (session ids, timestamps, request ids).
  They contain no credentials and no task content.

Out of scope: the OpenCode server itself, the LLM provider, and Hermes.
