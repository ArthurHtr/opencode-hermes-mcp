#!/usr/bin/env bash
# install.sh — thin wrapper around the Python setup wizard.
# All logic (bootstrap, idempotent steps, rich UI) lives in
# opencode_hermes_mcp/installer.py. This script only locates the repo
# root and execs the wizard, passing through all flags.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export OPENCODE_MCP_REPO="$REPO"

PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m opencode_hermes_mcp.installer "$@"
