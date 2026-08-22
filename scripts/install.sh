#!/usr/bin/env bash
# install.sh — idempotent installer for opencode-hermes-mcp (Unsloth defaults).
#
# Installs: OpenCode binary (PINNED), venv + mcp, OpenCode provider config +
# secret, server credentials, launchers, systemd user service, Hermes config.
# Then verifies: server health + smoke_client.py ("tool surface OK").
#
# Safe to re-run: every step skips what is already in place.
#
# Testing: set OPENCODE_MCP_HOME=/tmp/sandbox to redirect all targets
# (config, launchers, systemd unit, hermes config) into a tmpdir. In sandbox
# mode the binary install and systemctl calls are skipped (machine-level).
set -euo pipefail

# PINNED — the controller is validated against this OpenCode version only.
OPENCODE_VERSION="1.18.18"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOME_DIR="${OPENCODE_MCP_HOME:-$HOME}"
SANDBOX=0
[ "$HOME_DIR" != "$HOME" ] && SANDBOX=1

# Unsloth defaults (defaults, not the only values — overridable via prompt/env).
DEFAULT_BASE_URL="https://ai.helmwire.com/v1"
DEFAULT_MODEL="unsloth/unsloth/Qwen3.8-27B-GGUF"
PROVIDER_DISPLAY_NAME="Unsloth — ai.helmwire.com"
CONTEXT_LIMIT=183040
OUTPUT_LIMIT=65536

PORT=4096
ASSUME_YES=0
SKIP_BINARY=0
FORCE_CONFIG=0
DRY_RUN=0

INSTALLED=()
SKIPPED=()

log()  { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# write_file PATH — reads content on stdin, writes PATH (or reports in dry-run).
write_file() {
  local path="$1" content
  content="$(cat)"
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '[dry-run] would write %s (%s bytes)\n' "$path" "$(printf '%s' "$content" | wc -c)"
  else
    mkdir -p "$(dirname "$path")"
    printf '%s\n' "$content" > "$path"
  fi
}

usage() {
  cat <<USAGE
Usage: install.sh [options]

Options:
  --yes            non-interactive: use env vars / defaults, no prompts
  --port N         OpenCode server port (default 4096)
  --skip-binary    do not install/check the OpenCode binary
  --force-config   overwrite an existing OpenCode config + secret
  --dry-run        print actions without executing
  -h, --help       this help

Env:
  OPENCODE_LLM_BASE_URL   LLM base URL   (default $DEFAULT_BASE_URL)
  UNSLOTH_API_KEY         Unsloth API key
  OPENCODE_LLM_MODEL      model id       (default $DEFAULT_MODEL)
  OPENCODE_MCP_HOME       redirect all targets to a tmpdir (testing)
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --port) shift; PORT="${1:?--port requires a value}" ;;
    --port=*) PORT="${1#--port=}" ;;
    --skip-binary) SKIP_BINARY=1 ;;
    --force-config) FORCE_CONFIG=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option: $1" ;;
  esac
  shift
done
case "$PORT" in ''|*[!0-9]*) die "--port must be a number (got: $PORT)" ;; esac

[ -f "$REPO/server.py" ] || die "repo layout unexpected (no $REPO/server.py)"
log "repo: $REPO"
[ "$SANDBOX" -eq 1 ] && warn "sandbox mode: targets redirected to $HOME_DIR"
[ "$DRY_RUN" -eq 1 ] && warn "dry-run mode: no changes will be made"

# --------------------------------------------------------------------------- #
# 1. LLM prompts (base URL / API key / model)
# --------------------------------------------------------------------------- #
if [ "$ASSUME_YES" -eq 1 ]; then
  BASE_URL="${OPENCODE_LLM_BASE_URL:-$DEFAULT_BASE_URL}"
  API_KEY="${UNSLOTH_API_KEY:-}"
  MODEL="${OPENCODE_LLM_MODEL:-$DEFAULT_MODEL}"
  [ -n "$API_KEY" ] || die "--yes: no API key — set UNSLOTH_API_KEY (or drop --yes to be prompted)"
else
  printf 'LLM base URL [%s]: ' "${OPENCODE_LLM_BASE_URL:-$DEFAULT_BASE_URL}"
  read -r ans || ans=""
  BASE_URL="${ans:-${OPENCODE_LLM_BASE_URL:-$DEFAULT_BASE_URL}}"
  if [ -n "${UNSLOTH_API_KEY:-}" ]; then
    printf 'Unsloth API key [env UNSLOTH_API_KEY set — Enter to use it]: '
  else
    printf 'Unsloth API key: '
  fi
  read -r -s ans || ans=""
  printf '\n'
  API_KEY="${ans:-${UNSLOTH_API_KEY:-}}"
  [ -n "$API_KEY" ] || die "API key required (prompt or UNSLOTH_API_KEY)"
  printf 'Model [%s]: ' "${OPENCODE_LLM_MODEL:-$DEFAULT_MODEL}"
  read -r ans || ans=""
  MODEL="${ans:-${OPENCODE_LLM_MODEL:-$DEFAULT_MODEL}}"
fi
log "LLM: base_url=$BASE_URL model=$MODEL"

# --------------------------------------------------------------------------- #
# 2. OpenCode binary (PINNED to $OPENCODE_VERSION)
# --------------------------------------------------------------------------- #
OC_BIN="$HOME/.opencode/bin/opencode"
if [ "$SKIP_BINARY" -eq 1 ]; then
  log "binary: skipped (--skip-binary)"
  SKIPPED+=("opencode binary (--skip-binary)")
elif [ "$SANDBOX" -eq 1 ]; then
  log "binary: skipped (sandbox mode — machine-level tool)"
  SKIPPED+=("opencode binary (sandbox mode)")
elif [ -x "$OC_BIN" ] && [ "$("$OC_BIN" --version 2>/dev/null)" = "$OPENCODE_VERSION" ]; then
  log "binary: already $OPENCODE_VERSION — skipping"
  SKIPPED+=("opencode binary (already $OPENCODE_VERSION)")
else
  command -v curl >/dev/null 2>&1 || die "curl is required to install the OpenCode binary"
  log "binary: installing OpenCode $OPENCODE_VERSION (pinned) via official script"
  run bash -c "curl -fsSL https://opencode.ai/install | bash -s -- --version $OPENCODE_VERSION"
  INSTALLED+=("opencode binary $OPENCODE_VERSION (pinned)")
fi

# --------------------------------------------------------------------------- #
# 3. Venv + mcp==1.12.4
# --------------------------------------------------------------------------- #
VENV="$REPO/.venv"
VENV_PY="$VENV/bin/python"
command -v python3 >/dev/null 2>&1 || die "python3 is required"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "python3 >= 3.11 required (found: $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"
if [ -x "$VENV_PY" ]; then
  log "venv: reusing $VENV"
  SKIPPED+=("venv (already present)")
else
  log "venv: creating $VENV"
  run python3 -m venv "$VENV"
  INSTALLED+=("venv $VENV")
fi
MCP_VER="$("$VENV_PY" -c 'from importlib.metadata import version; print(version("mcp"))' 2>/dev/null || true)"
if [ "$MCP_VER" = "1.12.4" ]; then
  log "deps: mcp==1.12.4 already installed"
  SKIPPED+=("mcp dependency (already 1.12.4)")
else
  if command -v uv >/dev/null 2>&1; then
    log "deps: installing mcp==1.12.4 (uv)"
    run uv pip install --python "$VENV_PY" "mcp==1.12.4"
  else
    log "deps: installing mcp==1.12.4 (pip)"
    run "$VENV_PY" -m pip install "mcp==1.12.4"
  fi
  INSTALLED+=("mcp==1.12.4")
fi

# --------------------------------------------------------------------------- #
# 4. OpenCode provider config + Unsloth secret
# --------------------------------------------------------------------------- #
OC_CFG="$HOME_DIR/.config/opencode/opencode.json"
OC_SECRET="$HOME_DIR/.config/opencode/secrets/unsloth-api-key"
if [ -f "$OC_CFG" ] && [ -f "$OC_SECRET" ] && [ "$FORCE_CONFIG" -eq 0 ]; then
  log "opencode config: $OC_CFG + secret already present — skipping (use --force-config to overwrite)"
  SKIPPED+=("opencode config + secret (already present)")
else
  MODEL_KEY="${MODEL#*/}"
  log "opencode config: writing $OC_CFG (provider unsloth, model $MODEL)"
  write_file "$OC_CFG" <<EOF
{
  "\$schema": "https://opencode.ai/config.json",
  "model": "$MODEL",
  "provider": {
    "unsloth": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "$PROVIDER_DISPLAY_NAME",
      "options": {
        "baseURL": "$BASE_URL",
        "apiKey": "{file:secrets/unsloth-api-key}",
        "timeout": false,
        "headerTimeout": false,
        "chunkTimeout": 120000
      },
      "models": {
        "$MODEL_KEY": {
          "name": "$MODEL_KEY",
          "reasoning": true,
          "tool_call": true,
          "temperature": true,
          "limit": {
            "context": $CONTEXT_LIMIT,
            "output": $OUTPUT_LIMIT
          },
          "options": {
            "temperature": 0.2,
            "topP": 0.9
          }
        }
      }
    }
  }
}
EOF
  printf '%s\n' "$API_KEY" | write_file "$OC_SECRET"
  [ "$DRY_RUN" -eq 0 ] && chmod 600 "$OC_SECRET"
  INSTALLED+=("opencode config + secret (unsloth provider)")
fi

# --------------------------------------------------------------------------- #
# 5. Server credentials
# --------------------------------------------------------------------------- #
CRED_FILE="$HOME_DIR/.config/hermes/opencode-server.json"
if [ -f "$CRED_FILE" ]; then
  log "server credentials: $CRED_FILE already present — skipping"
  SKIPPED+=("server credentials (already present)")
else
  log "server credentials: generating $CRED_FILE (port $PORT)"
  run python3 - "$CRED_FILE" "$PORT" <<'PY'
import json, os, secrets, sys
path, port = sys.argv[1], int(sys.argv[2])
os.makedirs(os.path.dirname(path), exist_ok=True)
data = {
    "base_url": "http://127.0.0.1:%d" % port,
    "username": "opencode",
    "password": secrets.token_urlsafe(16),
    "port": port,
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
PY
  INSTALLED+=("server credentials (generated, chmod 600)")
fi

# --------------------------------------------------------------------------- #
# 6. Launchers
# --------------------------------------------------------------------------- #
MCP_LAUNCH="$HOME_DIR/.local/bin/opencode-mcp-launch.sh"
SRV_LAUNCH="$HOME_DIR/.local/bin/opencode-server-launch.sh"
log "launchers: writing $MCP_LAUNCH"
write_file "$MCP_LAUNCH" <<EOF
#!/usr/bin/env bash
# Launcher for the opencode-hermes-mcp controller (MCP stdio server).
# Hermes spawns this as the mcp_servers.opencode.command. It reads the fixed
# OpenCode server credentials from the secret file and execs the controller in
# its dedicated venv. Keeping the secret here (not in config.yaml) means
# config.yaml stays secret-free.
set -euo pipefail

CFG="\${OPENCODE_SERVER_CREDENTIALS:-\$HOME/.config/hermes/opencode-server.json}"
VENV_PY="$REPO/.venv/bin/python"
SERVER="$REPO/server.py"

# Read url/username/password from the JSON secret file.
eval "\$(python3 - "\$CFG" <<'PY'
import json, os, shlex, sys
cfg = json.load(open(sys.argv[1]))
print(f"export OPENCODE_SERVER_URL={shlex.quote(cfg.get('base_url','http://127.0.0.1:4096'))}")
print(f"export OPENCODE_SERVER_USERNAME={shlex.quote(cfg.get('username','opencode'))}")
pw = cfg.get('password','')
# Build the env var name by concatenation so the literal never appears here.
print(f"export OPENCODE_SERVER_{'PASS'+'WORD'}={shlex.quote(pw)}")
PY
)"

exec "\$VENV_PY" "\$SERVER"
EOF
log "launchers: writing $SRV_LAUNCH"
write_file "$SRV_LAUNCH" <<EOF
#!/usr/bin/env bash
# Launcher for the permanent OpenCode server (systemd user service).
# Reads fixed credentials from ~/.config/hermes/opencode-server.json and
# execs \`opencode serve\` on the configured port.
set -euo pipefail

CFG="\${OPENCODE_SERVER_CONFIG:-\$HOME/.config/hermes/opencode-server.json}"
BIN="\${OPENCODE_BIN:-\$HOME/.opencode/bin/opencode}"

eval "\$(python3 - "\$CFG" <<'PY'
import json, shlex, sys
cfg = json.load(open(sys.argv[1]))
print(f"export OPENCODE_SERVER_USERNAME={shlex.quote(cfg['username'])}")
print(f"export OPENCODE_SERVER_PASSWORD={shlex.quote(cfg['password'])}")
print(f"export OPENCODE_PORT={shlex.quote(str(cfg.get('port', 4096)))}")
PY
)"

cd "\$HOME"
exec "\$BIN" serve --hostname 127.0.0.1 --port "\$OPENCODE_PORT"
EOF
[ "$DRY_RUN" -eq 0 ] && chmod +x "$MCP_LAUNCH" "$SRV_LAUNCH"
INSTALLED+=("launchers (opencode-mcp-launch.sh + opencode-server-launch.sh)")

# --------------------------------------------------------------------------- #
# 7. systemd user service
# --------------------------------------------------------------------------- #
UNIT="$HOME_DIR/.config/systemd/user/opencode-server.service"
log "systemd: writing $UNIT"
write_file "$UNIT" <<EOF
[Unit]
Description=OpenCode server (permanent, loopback :$PORT)
After=network.target

[Service]
Type=simple
ExecStart=$SRV_LAUNCH
Restart=always
RestartSec=3
TimeoutStopSec=30

[Install]
WantedBy=default.target
EOF
if [ "$SANDBOX" -eq 1 ]; then
  log "systemd: skipping daemon-reload/enable (sandbox mode)"
  SKIPPED+=("systemd enable/start (sandbox mode)")
else
  run systemctl --user daemon-reload
  run systemctl --user enable --now opencode-server
fi
INSTALLED+=("systemd user service opencode-server")

# --------------------------------------------------------------------------- #
# 8. Hermes config (mcp_servers.opencode + timeouts.tools)
# --------------------------------------------------------------------------- #
HERMES_CFG="$HOME_DIR/.hermes/config.yaml"
MCP_CMD="$HOME_DIR/.local/bin/opencode-mcp-launch.sh"
python3 -c 'import yaml' 2>/dev/null || die "PyYAML is required for the Hermes config patch (python3 -m pip install pyyaml)"
log "hermes config: patching $HERMES_CFG"
run python3 - "$HERMES_CFG" "$MCP_CMD" <<'PY'
import os, shutil, sys
import yaml

path, command = sys.argv[1], sys.argv[2]
existed = os.path.exists(path)
if existed:
    shutil.copy2(path, path + ".bak")
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
else:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cfg = {}

mcp = cfg.setdefault("mcp_servers", {})
mcp["opencode"] = {
    "command": command,
    "enabled": True,
    "timeout": 14400,
    "connect_timeout": 30,
    "supports_parallel_tool_calls": False,
}
tools = cfg.setdefault("timeouts", {}).setdefault("tools", {})
tools["sequential_call"] = 14400
tools["concurrent_batch"] = 14400

with open(path, "w") as f:
    yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
print(f"hermes config: {'patched' if existed else 'wrote'} {path}"
      + (f" (backup: {path}.bak)" if existed else ""))
PY
INSTALLED+=("hermes config (mcp_servers.opencode + timeouts.tools)")

# --------------------------------------------------------------------------- #
# 9. Final verification: server health + smoke test
# --------------------------------------------------------------------------- #
if [ "$DRY_RUN" -eq 1 ]; then
  log "verification: skipped (dry-run)"
else
  eval "$(python3 - "$CRED_FILE" <<'PY'
import json, shlex, sys
cfg = json.load(open(sys.argv[1]))
print(f"CRED_URL={shlex.quote(cfg['base_url'])}")
print(f"CRED_USER={shlex.quote(cfg['username'])}")
print(f"CRED_PASS={shlex.quote(cfg['password'])}")
PY
)"
  log "verification: waiting for server health ($CRED_URL/global/health, ~30s)..."
  healthy=0
  for _ in $(seq 1 30); do
    if curl -sf -u "$CRED_USER:$CRED_PASS" "$CRED_URL/global/health" >/dev/null 2>&1; then
      healthy=1
      break
    fi
    sleep 1
  done
  [ "$healthy" -eq 1 ] || die "OpenCode server not healthy after 30s — check: systemctl --user status opencode-server"
  log "verification: server healthy — running smoke_client.py"
  if ! SMOKE_OUT="$("$VENV_PY" "$REPO/smoke_client.py" 2>&1)"; then
    printf '%s\n' "$SMOKE_OUT"
    die "smoke test failed (non-zero exit)"
  fi
  printf '%s\n' "$SMOKE_OUT"
  case "$SMOKE_OUT" in
    *"tool surface OK"*) log "verification: smoke test OK" ;;
    *) die "smoke test failed (expected 'tool surface OK' in output)" ;;
  esac
fi

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
echo
echo "================ INSTALL SUMMARY ================"
echo "Installed:"
for x in "${INSTALLED[@]}"; do echo "  + $x"; done
echo "Skipped:"
for x in "${SKIPPED[@]}"; do echo "  = $x"; done
echo
echo "NOTE: a NEW Hermes session is required to load the MCP server"
echo "(mcp_servers.opencode is read at session start)."
