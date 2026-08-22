#!/usr/bin/env bash
# upgrade.sh — update the opencode-hermes-mcp installation.
#
# Default: update the CONTROLLER (git pull + venv deps + service restart +
# smoke test). The OpenCode binary is NOT touched by default.
#
#   --binary [VERSION]   upgrade the OpenCode BINARY instead (default: latest
#                        via the official script). Prints an explicit warning:
#                        the controller is validated for 1.18.18 only — after
#                        a binary upgrade, re-validate with tests/run_tests.py.
#
# Testing: set OPENCODE_MCP_HOME=/tmp/sandbox to redirect the targets
# (systemctl calls are skipped in sandbox mode).
set -euo pipefail

# PINNED — the controller is validated against this OpenCode version only.
OPENCODE_VERSION="1.18.18"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${OPENCODE_MCP_HOME:-$HOME}"
SANDBOX=0
[ "$HOME_DIR" != "$HOME" ] && SANDBOX=1

VENV_PY="$REPO/.venv/bin/python"
OC_BIN="$HOME/.opencode/bin/opencode"
CRED_FILE="$HOME_DIR/.config/hermes/opencode-server.json"

BINARY_MODE=0
BINARY_VERSION=""

log()  { printf '\033[1;32m[upgrade]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[upgrade]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[upgrade]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<USAGE
Usage: upgrade.sh [--binary [VERSION]]

Default: update the controller (git pull + venv deps + restart + smoke test).

Options:
  --binary [VERSION]   upgrade the OpenCode binary (default: latest).
                       WARNING: the controller is validated for $OPENCODE_VERSION
                       only — re-validate with tests/run_tests.py afterwards.
  -h, --help           this help

Env:
  OPENCODE_MCP_HOME    redirect all targets to a tmpdir (testing)
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --binary)
      BINARY_MODE=1
      if [ $# -ge 2 ] && [ "${2#--}" = "$2" ]; then
        BINARY_VERSION="$2"
        shift
      fi
      ;;
    --binary=*) BINARY_MODE=1; BINARY_VERSION="${1#--binary=}" ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option: $1" ;;
  esac
  shift
done

wait_health() {
  [ -f "$CRED_FILE" ] || die "server credentials not found: $CRED_FILE (run scripts/install.sh first)"
  eval "$(python3 - "$CRED_FILE" <<'PY'
import json, shlex, sys
cfg = json.load(open(sys.argv[1]))
print(f"CRED_URL={shlex.quote(cfg['base_url'])}")
print(f"CRED_USER={shlex.quote(cfg['username'])}")
print(f"CRED_PASS={shlex.quote(cfg['password'])}")
PY
)"
  log "waiting for server health ($CRED_URL/global/health, ~30s)..."
  local healthy=0
  for _ in $(seq 1 30); do
    if curl -sf -u "$CRED_USER:$CRED_PASS" "$CRED_URL/global/health" >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 1
  done
  [ "$healthy" -eq 1 ] || die "OpenCode server not healthy after 30s — check: systemctl --user status opencode-server"
}

run_smoke() {
  [ -x "$VENV_PY" ] || die "venv missing: $VENV_PY (run scripts/install.sh first)"
  log "smoke: running smoke_client.py"
  local smoke_out
  if ! smoke_out="$("$VENV_PY" -m opencode_hermes_mcp.smoke_client 2>&1)"; then
    printf '%s\n' "$smoke_out"
    die "smoke test failed (non-zero exit)"
  fi
  printf '%s\n' "$smoke_out"
  case "$smoke_out" in
    *"tool surface OK"*) log "smoke: OK" ;;
    *) die "smoke test failed (expected 'tool surface OK' in output)" ;;
  esac
}

restart_service() {
  if [ "$SANDBOX" -eq 1 ]; then
    log "systemd: skipping restart (sandbox mode)"
  else
    log "systemd: restarting opencode-server"
    systemctl --user restart opencode-server
  fi
}

if [ "$BINARY_MODE" -eq 1 ]; then
  # --------------------------------------------------------------------- #
  # Binary upgrade (explicit opt-in — the controller is pinned-validated)
  # --------------------------------------------------------------------- #
  warn "AVERTISSEMENT : le controller est validé pour $OPENCODE_VERSION ;"
  warn "après un upgrade du binaire, re-valider le controller (tests/run_tests.py)."
  command -v curl >/dev/null 2>&1 || die "curl is required to install the OpenCode binary"
  if [ -n "$BINARY_VERSION" ]; then
    log "binary: installing OpenCode $BINARY_VERSION via official script"
    bash -c "curl -fsSL https://opencode.ai/install | bash -s -- --version $BINARY_VERSION"
  else
    log "binary: installing the LATEST OpenCode via official script"
    bash -c "curl -fsSL https://opencode.ai/install | bash"
  fi
  NEW_VER="$("$OC_BIN" --version 2>/dev/null || true)"
  log "binary: installed version = ${NEW_VER:-unknown}"
  restart_service
  wait_health
  run_smoke
  warn "Rappel : re-validez le controller avec tests/run_tests.py (validé pour $OPENCODE_VERSION)."
else
  # --------------------------------------------------------------------- #
  # Controller upgrade (default)
  # --------------------------------------------------------------------- #
  log "controller: git pull in $REPO"
  git -C "$REPO" pull
  [ -x "$VENV_PY" ] || die "venv missing: $VENV_PY (run scripts/install.sh first)"
  if command -v uv >/dev/null 2>&1; then
    log "deps: refreshing venv (uv pip install -e .)"
    uv pip install --python "$VENV_PY" -e "$REPO"
  else
    log "deps: refreshing venv (pip install -e .)"
    "$VENV_PY" -m pip install -e "$REPO"
  fi
  restart_service
  wait_health
  run_smoke
  log "upgrade: done (controller updated, binary untouched — OpenCode $("$OC_BIN" --version 2>/dev/null || echo unknown))"
fi
