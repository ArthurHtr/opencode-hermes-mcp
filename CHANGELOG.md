# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `docs/hermes-integration.md` — full manual for integrating the controller
  with Hermes: what the MCP is for, the exact `~/.hermes/config.yaml` entry
  the installer writes, manual (by-hand) integration, the six tools,
  troubleshooting, uninstall.
- `docs/skill.example.md` — ready-to-copy Hermes skill template (the
  delegation protocol: agent choice, answering questions/permissions itself,
  the no-poll rule, recovery). The installer deliberately does NOT install a
  skill for you (Hermes's skill layout may change); adapt this template into
  `~/.hermes/skills/` instead. The wizard's final summary now points to both
  documents.
- `--skip-verify` flag for the installer: skips the final health + smoke
  verification (step 9) — useful for sandbox/CI runs where no OpenCode
  server is expected to come up.

### Changed

- **Installer rewritten as a Python + `rich` setup wizard**
  (`opencode_hermes_mcp/installer.py`): banner, numbered steps, styled
  prompts (API key never echoed), progress during long phases, and a final
  summary panel (Installed / Already in place / Skipped + duration).
  `scripts/install.sh` is now a thin wrapper that execs the wizard.
  The wizard self-bootstraps: with a bare `python3` >= 3.11 it creates the
  repo venv, installs `mcp==1.12.4` + `rich>=13` + `pyyaml>=6` + the
  editable package, and re-execs itself. All flags, env vars, generated
  files (opencode.json, credentials, launchers, systemd unit, Hermes config
  patch) and the idempotent skip-if-present behavior are unchanged from the
   former bash installer. `rich>=13` and `pyyaml>=6` are now package
   dependencies.
- The OpenCode pin is now a **single source of truth** in
  `opencode_hermes_mcp/pin.txt` (one line, no `v` prefix): `installer.py`
  (via `pinned_version()`) and `scripts/upgrade.sh` both read it, falling
  back to `1.18.18` when the file is missing or empty (e.g. pip installs
  where the file is not shipped next to the code).
- `scripts/upgrade.sh --binary` without a version now installs the validated
  PINNED version instead of the bleeding edge: it is idempotent (no-op when
  the binary is already at the pin) and aligns the binary on the pin
  (upgrade or downgrade) when it is at another version. `--binary latest`
  remains the explicit opt-in to the latest version, and `--binary X.Y.Z`
  installs the requested version — both warn that the controller is
  validated for the pin only and must be re-validated with
  `tests/run_tests.py` before any production use.

### Fixed

- `scripts/install.sh` (step 9) and `scripts/upgrade.sh` (`wait_health`): the
  "server health" wait loop now bounds each `curl` with `--max-time 3`
  (previously unbounded), captures the last curl error (exit code + message)
  and includes it in the failure message, and on failure prints
  `systemctl --user status opencode-server --no-pager` (outside sandbox mode)
  for immediate diagnosis. Previously, a port that accepts TCP but never
  answers (e.g. another process already bound to it) made each iteration hang
  for tens of seconds, blocking the install ~15 minutes in silence before the
  failure message.

## [0.3.0] - 2026-08-23

### Added

- TUI attach helpers `ocattach` and `oc-current` (sources in
  `scripts/helpers/`, installed to `~/.local/bin/` by `install.sh`, removed
  by `uninstall.sh`): `ocattach <repo> [ses_...]` opens the OpenCode TUI on a
  repo/session; `oc-current` attaches to the session the controller is
  supervising right now (reads the newest
  `~/.local/state/opencode-hermes-mcp/turn_*.json`). Both read the server
  credentials from `~/.config/hermes/opencode-server.json`.

## [0.2.0] - 2026-08-22

### Added

- Proper Python package layout: `opencode_hermes_mcp/` (relative imports),
  `__init__.py` with `__version__`.
- Console entry point `opencode-mcp-controller`
  (`opencode_hermes_mcp.server:main`, MCP stdio).
- `smoke_client` invocable as `python -m opencode_hermes_mcp.smoke_client`;
  credentials now read from `OPENCODE_SERVER_*` env vars (falling back to
  `~/.config/hermes/opencode-server.json`).
- Professional metadata: authors, README, MIT license, classifiers, keywords,
  project URLs, `dev` extra (`pytest`, `ruff`).
- `LICENSE` (MIT), `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`.
- GitHub Actions CI: ruff lint + package build (sdist/wheel) on a
  Python 3.11/3.12 matrix, plus a smoke test against a live OpenCode 1.18.18
  server (no LLM calls).
- GitHub Actions publish workflow (PyPI + GitHub release) on `v*` tags.

### Changed

- **Breaking (layout only, not usage):** the flat root modules
  (`server.py`, `controller.py`, `client.py`, `models.py`, `smoke_client.py`)
  moved into the `opencode_hermes_mcp` package. Launchers and scripts now use
  `python -m opencode_hermes_mcp.server` / `python -m opencode_hermes_mcp.smoke_client`.
  Existing live installations must be re-wired with `scripts/upgrade.sh`
  (or `scripts/install.sh`) after upgrading.
- `scripts/install.sh` installs the package into the repo venv
  (`pip install -e .`) so the generated launcher can exec the module.
- `tests/run_tests.py` imports the package and spawns the controller via
  `-m opencode_hermes_mcp.server`.

## [0.1.0] - 2026-08-22

### Added

- Initial release: deterministic MCP controller (6 tools) between Hermes and
  the permanent OpenCode server, validated against OpenCode **1.18.18** (pinned).
- `pyproject.toml` (flat layout) + `scripts/install.sh`, `scripts/uninstall.sh`,
  `scripts/upgrade.sh` (pinned OpenCode 1.18.18 binary, venv, credentials,
  systemd user service, Hermes config patch, smoke verification).
- `tests/run_tests.py` integration suite (14 tests, live LLM) and
  `smoke_client.py` (no-LLM smoke test).
