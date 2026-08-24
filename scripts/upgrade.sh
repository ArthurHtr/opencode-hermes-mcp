#!/usr/bin/env bash
# upgrade.sh — update the opencode-hermes-mcp installation.
#
# Default: update the CONTROLLER (git pull + venv deps + service restart +
# smoke test). The OpenCode binary is NOT touched by default.
#
#   --binary             install the PINNED OpenCode version (read from
#                        opencode_hermes_mcp/pin.txt). Idempotent: if the
#                        installed binary is already at the pin, nothing is
#                        done; if it is at another version, it is aligned on
#                        the pin (upgrade or downgrade).
#   --binary latest      EXPLICIT opt-in to the LATEST OpenCode version
#                        (bleeding edge). The controller is validated for the
#                        pin only — re-validate with tests/run_tests.py before
#                        any production use.
#   --binary X.Y.Z       install the requested version; same warning when
#                        X.Y.Z != pin.
#
# Testing: set OPENCODE_MCP_HOME=/tmp/sandbox to redirect the targets
# (systemctl calls are skipped in sandbox mode).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# PINNED — the controller is validated against this OpenCode version only.
# Single source of truth: opencode_hermes_mcp/pin.txt (fallback below).
OPENCODE_VERSION="$(head -n1 "$REPO/opencode_hermes_mcp/pin.txt" 2>/dev/null | tr -d '[:space:]')"
OPENCODE_VERSION="${OPENCODE_VERSION:-1.18.18}"
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
  --binary             install the PINNED OpenCode version (read from
                       opencode_hermes_mcp/pin.txt). Idempotent: no-op if the
                       installed binary is already at the pin; otherwise it is
                       aligned on the pin (upgrade or downgrade).
  --binary latest      EXPLICIT opt-in to the LATEST OpenCode version
                       (bleeding edge).
  --binary X.Y.Z       install the requested version.
                       WARNING (latest, or X.Y.Z != pin): the controller is
                       validated for $OPENCODE_VERSION only — re-validate with
                       tests/run_tests.py before any production use.
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
  local healthy=0 last_err="" curl_err
  for _ in $(seq 1 30); do
    if curl_err="$(curl -sSf --max-time 3 -u "$CRED_USER:$CRED_PASS" "$CRED_URL/global/health" 2>&1)"; then
      healthy=1
      break
    else
      last_err="curl exit $? — ${curl_err:-no output}"
    fi
    sleep 1
  done
  if [ "$healthy" -ne 1 ]; then
    if [ "$SANDBOX" -eq 0 ]; then
      warn "diagnostic — systemctl --user status opencode-server:"
      systemctl --user status opencode-server --no-pager || true
    fi
    die "OpenCode server not healthy after 30s (last error: $last_err) — check: systemctl --user status opencode-server"
  fi
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
  # Binary upgrade.
  #   --binary          -> the validated PIN (idempotent, never bleeding edge)
  #   --binary latest   -> explicit opt-in to the latest version
  #   --binary X.Y.Z    -> the requested version
  # The controller is validated for the pin only.
  # --------------------------------------------------------------------- #
  command -v curl >/dev/null 2>&1 || die "curl is required to install the OpenCode binary"
  CURRENT_VER="$("$OC_BIN" --version 2>/dev/null || true)"
  if [ -z "$BINARY_VERSION" ]; then
    # Default: align the binary on the validated pin (never bleeding edge).
    if [ "$CURRENT_VER" = "$OPENCODE_VERSION" ]; then
      log "binary: already at the pinned version $OPENCODE_VERSION — nothing to do"
      exit 0
    fi
    if [ -n "$CURRENT_VER" ]; then
      warn "binary: installed version $CURRENT_VER != validated pin $OPENCODE_VERSION — aligning on the pin"
    else
      log "binary: no OpenCode binary found — installing the validated pin $OPENCODE_VERSION"
    fi
    log "binary: installing OpenCode $OPENCODE_VERSION (pinned) via official script"
    bash -c "curl -fsSL https://opencode.ai/install | bash -s -- --version $OPENCODE_VERSION"
  elif [ "$BINARY_VERSION" = "latest" ]; then
    warn "AVERTISSEMENT : le controller est validé pour $OPENCODE_VERSION uniquement —"
    warn "après cette opération, re-valider impérativement avec tests/run_tests.py"
    warn "avant toute utilisation en production."
    log "binary: installing the LATEST OpenCode via official script (explicit opt-in)"
    bash -c "curl -fsSL https://opencode.ai/install | bash"
  else
    if [ "$BINARY_VERSION" != "$OPENCODE_VERSION" ]; then
      warn "AVERTISSEMENT : le controller est validé pour $OPENCODE_VERSION uniquement —"
      warn "après cette opération, re-valider impérativement avec tests/run_tests.py"
      warn "avant toute utilisation en production."
    fi
    log "binary: installing OpenCode $BINARY_VERSION via official script"
    bash -c "curl -fsSL https://opencode.ai/install | bash -s -- --version $BINARY_VERSION"
  fi
  NEW_VER="$("$OC_BIN" --version 2>/dev/null || true)"
  log "binary: installed version = ${NEW_VER:-unknown}"
  restart_service
  wait_health
  run_smoke
  if [ "$NEW_VER" != "$OPENCODE_VERSION" ]; then
    warn "Rappel : le binaire ($NEW_VER) diffère du pin validé ($OPENCODE_VERSION) — re-validez impérativement avec tests/run_tests.py avant toute utilisation en production."
  fi
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
