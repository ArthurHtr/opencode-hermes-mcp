#!/usr/bin/env bash
# uninstall.sh — remove the opencode-hermes-mcp installation.
#
# Always removed: systemd user service, the 2 launchers, the repo venv,
# mcp_servers.opencode from the Hermes config, the server credentials file.
#
# NEVER touched (by design): the git clone, the OpenCode provider config
# (~/.config/opencode/opencode.json), the Unsloth secret, the OpenCode binary.
#   --purge          also remove the OpenCode provider config + Unsloth secret
#   --purge-binary   also remove the OpenCode binary (~/.opencode/bin/opencode)
#
# Testing: set OPENCODE_MCP_HOME=/tmp/sandbox to redirect the targets.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${OPENCODE_MCP_HOME:-$HOME}"
SANDBOX=0
[ "$HOME_DIR" != "$HOME" ] && SANDBOX=1

PURGE=0
PURGE_BINARY=0

REMOVED=()
KEPT=()

log()  { printf '\033[1;32m[uninstall]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[uninstall]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[uninstall]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<USAGE
Usage: uninstall.sh [options]

Options:
  --purge          also remove ~/.config/opencode/opencode.json + the
                   Unsloth secret (~/.config/opencode/secrets/unsloth-api-key)
  --purge-binary   also remove the OpenCode binary (~/.opencode/bin/opencode)
  -h, --help       this help

Env:
  OPENCODE_MCP_HOME   redirect all targets to a tmpdir (testing)
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --purge-binary) PURGE_BINARY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option: $1" ;;
  esac
  shift
done

# --------------------------------------------------------------------------- #
# 1. systemd user service
# --------------------------------------------------------------------------- #
UNIT="$HOME_DIR/.config/systemd/user/opencode-server.service"
if [ "$SANDBOX" -eq 1 ]; then
  log "systemd: skipping stop/disable (sandbox mode)"
else
  if systemctl --user list-unit-files 2>/dev/null | grep -q '^opencode-server\.service'; then
    log "systemd: stopping + disabling opencode-server"
    systemctl --user disable --now opencode-server || warn "systemctl disable --now reported an error (continuing)"
  else
    log "systemd: service opencode-server not installed — skipping"
  fi
fi
if [ -f "$UNIT" ]; then
  rm -f "$UNIT"
  [ "$SANDBOX" -eq 0 ] && systemctl --user daemon-reload
  REMOVED+=("systemd unit $UNIT")
else
  KEPT+=("systemd unit (not present)")
fi

# --------------------------------------------------------------------------- #
# 2. Launchers
# --------------------------------------------------------------------------- #
for launcher in "$HOME_DIR/.local/bin/opencode-mcp-launch.sh" "$HOME_DIR/.local/bin/opencode-server-launch.sh"; do
  if [ -f "$launcher" ]; then
    rm -f "$launcher"
    REMOVED+=("launcher $launcher")
  else
    KEPT+=("launcher (not present) $launcher")
  fi
done

# --------------------------------------------------------------------------- #
# 3. Venv
# --------------------------------------------------------------------------- #
if [ -d "$REPO/.venv" ]; then
  rm -rf "$REPO/.venv"
  REMOVED+=("venv $REPO/.venv")
else
  KEPT+=("venv (not present) $REPO/.venv")
fi

# --------------------------------------------------------------------------- #
# 4. Hermes config: remove mcp_servers.opencode (backup .bak)
# --------------------------------------------------------------------------- #
HERMES_CFG="$HOME_DIR/.hermes/config.yaml"
if [ -f "$HERMES_CFG" ]; then
  python3 -c 'import yaml' 2>/dev/null || die "PyYAML is required to patch the Hermes config (python3 -m pip install pyyaml)"
  OUT="$(python3 - "$HERMES_CFG" <<'PY'
import os, shutil, sys
import yaml

path = sys.argv[1]
shutil.copy2(path, path + ".bak")
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
mcp = cfg.get("mcp_servers") or {}
if "opencode" in mcp:
    del mcp["opencode"]
    if mcp:
        cfg["mcp_servers"] = mcp
    else:
        cfg.pop("mcp_servers", None)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
    print(f"hermes config: removed mcp_servers.opencode from {path} (backup: {path}.bak)")
else:
    os.remove(path + ".bak")
    print(f"hermes config: mcp_servers.opencode not present in {path} — nothing to do")
PY
)"
  printf '%s\n' "$OUT"
  case "$OUT" in
    *"removed mcp_servers.opencode"*) REMOVED+=("hermes config entry mcp_servers.opencode") ;;
    *) KEPT+=("hermes config entry (not present)") ;;
  esac
else
  KEPT+=("hermes config (not present) $HERMES_CFG")
fi

# --------------------------------------------------------------------------- #
# 5. Server credentials
# --------------------------------------------------------------------------- #
CRED_FILE="$HOME_DIR/.config/hermes/opencode-server.json"
if [ -f "$CRED_FILE" ]; then
  rm -f "$CRED_FILE"
  REMOVED+=("server credentials $CRED_FILE")
else
  KEPT+=("server credentials (not present) $CRED_FILE")
fi

# --------------------------------------------------------------------------- #
# 6. Optional purge: OpenCode provider config + Unsloth secret
# --------------------------------------------------------------------------- #
OC_CFG="$HOME_DIR/.config/opencode/opencode.json"
OC_SECRET="$HOME_DIR/.config/opencode/secrets/unsloth-api-key"
if [ "$PURGE" -eq 1 ]; then
  for f in "$OC_CFG" "$OC_SECRET"; do
    if [ -f "$f" ]; then
      rm -f "$f"
      REMOVED+=("purge: $f")
    else
      KEPT+=("purge target (not present) $f")
    fi
  done
else
  KEPT+=("opencode provider config $OC_CFG")
  KEPT+=("unsloth secret $OC_SECRET")
fi

# --------------------------------------------------------------------------- #
# 7. Optional purge: OpenCode binary
# --------------------------------------------------------------------------- #
OC_BIN="$HOME/.opencode/bin/opencode"
if [ "$PURGE_BINARY" -eq 1 ]; then
  if [ -f "$OC_BIN" ]; then
    rm -f "$OC_BIN"
    REMOVED+=("purge-binary: $OC_BIN")
  else
    KEPT+=("purge-binary target (not present) $OC_BIN")
  fi
else
  KEPT+=("opencode binary $OC_BIN")
fi

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
echo
echo "================ UNINSTALL SUMMARY ================"
echo "Removed:"
for x in "${REMOVED[@]}"; do echo "  - $x"; done
echo "Kept:"
for x in "${KEPT[@]}"; do echo "  = $x"; done
echo
echo "NOTE: the git clone at $REPO was NOT touched."
echo "NOTE: a NEW Hermes session is required for the config change to take effect."
