# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
