# Integrating opencode-hermes-mcp with Hermes

This is the complete manual for wiring the OpenCode MCP controller into
Hermes: what the MCP is for, what the installer writes, how to integrate it
by hand (if Hermes changes its config layout), how to use the tools, and how
to troubleshoot or remove it.

> The installer (`scripts/install.sh` → `opencode_hermes_mcp.installer`)
> already performs steps 1–3 automatically. Read this document to understand
> what it did, to integrate manually, or to re-integrate after a Hermes
> upgrade that changed the config format.

## What this MCP is for

Hermes is an LLM supervisor; OpenCode is a coding agent with its own LLM,
tools, and sessions. The **opencode-hermes-mcp** controller is a
deterministic (no-LLM) bridge between the two, exposed to Hermes as a
standard MCP stdio server:

```text
Hermes (LLM, MCP client)
  │  ~/.hermes/config.yaml → mcp_servers.opencode
  ▼
~/.local/bin/opencode-mcp-launch.sh
  │  reads ~/.config/hermes/opencode-server.json (basic-auth credentials)
  │  execs <clone>/.venv/bin/python -m opencode_hermes_mcp.server
  ▼
controller (FastMCP, 6 tools — SSE + REST state machine, no LLM)
  │  HTTP + SSE, directory-scoped
  ▼
OpenCode server  (systemd user service opencode-server, 127.0.0.1:4096)
```

Why a controller instead of calling the OpenCode HTTP API directly?

- **One blocking call per task.** Hermes calls `opencode_run` once and blocks
  (zero tokens spent watching) while the controller monitors SSE/REST,
  reconnects, and reconciles state.
- **Questions and permissions come back to Hermes as structured results**
  (`needs_agent_input`), and Hermes answers them *itself* — the user does not
  need to be present for a normal run.
- **The prompt is never resubmitted.** After an answer or a permission
  decision, the controller re-enters the wait on the *same* OpenCode turn.

## What the installer writes (and where)

| File | Purpose |
| --- | --- |
| `~/.opencode/bin/opencode` | OpenCode binary (pinned, 1.18.21) |
| `<clone>/.venv/` | venv with `mcp==1.12.4`, `rich`, `pyyaml`, the editable package |
| `~/.config/opencode/opencode.json` | OpenCode provider config (model, limits, agents, compaction) |
| `~/.config/opencode/secrets/api-key` | LLM API key (mode 600), referenced as `{file:secrets/api-key}` |
| `~/.config/hermes/opencode-server.json` | Server basic-auth credentials (mode 600) |
| `~/.local/bin/opencode-mcp-launch.sh` | MCP launcher (Hermes spawns this) |
| `~/.local/bin/opencode-server-launch.sh` | OpenCode server launcher (systemd) |
| `~/.local/bin/ocattach`, `~/.local/bin/oc-current` | TUI helpers (tmux attach) |
| `~/.config/systemd/user/opencode-server.service` | Permanent server (loopback :4096) |
| `~/.hermes/config.yaml` | `mcp_servers.opencode` + `timeouts.tools` (backup `.bak`) |

## 1. The Hermes config entry

The installer patches `~/.hermes/config.yaml` with exactly this (values from
a default install):

```yaml
mcp_servers:
  opencode:
    command: /home/<user>/.local/bin/opencode-mcp-launch.sh
    enabled: true
    timeout: 14400            # 4 h — a single opencode_run may block that long
    connect_timeout: 30
    supports_parallel_tool_calls: false   # runs are strictly sequential
timeouts:
  tools:
    sequential_call: 14400    # Hermes must not cut the blocking call short
    concurrent_batch: 14400
```

Notes:

- `timeout` (per-call) and `timeouts.tools.sequential_call` (Hermes-side)
  must both be large enough for your longest task. If a run dies at exactly
  the timeout, raise both.
- `supports_parallel_tool_calls: false` is deliberate: the controller runs
  one OpenCode turn at a time (single LLM slot, sequential subagents).
- The credentials live in `~/.config/hermes/opencode-server.json`, **not** in
  `config.yaml` — so `config.yaml` stays secret-free.

## 2. Manual integration (by hand)

Use this if the installer is not an option, or after a Hermes upgrade
changed the config layout. The installer itself only writes the YAML above —
if that block is valid for your Hermes version, you are done.

1. **Prerequisites in place** (what the installer builds):
   - OpenCode binary: `curl -fsSL https://opencode.ai/install | bash`
     (pin: `--version 1.18.21`).
   - A venv with the controller: `python3 -m venv <clone>/.venv &&
     <clone>/.venv/bin/pip install -e <clone>` (needs `mcp==1.12.4`).
   - OpenCode provider config + API key (`~/.config/opencode/...`).
   - Server credentials file:
     `~/.config/hermes/opencode-server.json` with
     `{"base_url": "http://127.0.0.1:4096", "username": "...",
     "password": "..."}` (mode 600).
   - The two launchers in `~/.local/bin/` (the installer generates them;
     they are short bash scripts — see the repo for the exact content).
   - The systemd user service `opencode-server` running
     (`systemctl --user status opencode-server`).

2. **Add the `mcp_servers.opencode` block** from section 1 to
   `~/.hermes/config.yaml` (adapt the path if your launcher lives elsewhere).

3. **Start a NEW Hermes session** — MCP servers are loaded at session start;
   an already-open session will not see the new tools.

4. **Verify**:

   ```sh
   hermes mcp test opencode      # must list exactly 6 tools
   ```

## 3. The six tools

| Tool | Purpose |
| --- | --- |
| `opencode_run` | Delegate a task (or resume a session). **Blocks** until the turn completes, errors, is aborted, times out, or needs Hermes input. |
| `opencode_answer` | Answer a pending OpenCode question; re-enters the blocking wait. |
| `opencode_permission` | Decide a pending permission (`once` / `always` / `reject`); re-enters the blocking wait. |
| `opencode_abort` | Abort the active run. |
| `opencode_inspect` | **Diagnostic only**: one-shot snapshot (status, pending q/p, last text). Never a substitute for the blocking wait. |
| `opencode_sessions` | List OpenCode sessions for a directory. |

Minimal delegation:

```text
opencode_run(directory="/abs/repo", agent="build", task="...", timeout=3600)
```

- `agent` is **required for a new session** and validated dynamically
  against the server (`GET /agent?directory=...`). Pick the primary agent
  that fits the task; never rely on the default (it may be read-only).
- Pass `session_id="ses_..."` to continue an existing session. If the turn
  is still in flight (e.g. after a controller restart), the prompt is NOT
  resubmitted — pass a placeholder like `task="resume"`.
- Possible results: `completed`, `needs_agent_input` (kind
  `question`/`permission`), `error`, `aborted`, `timeout`.

**The absolute rule:** never poll `opencode_inspect` while waiting for a
normal run. One `opencode_run` per task, zero tokens spent watching.

## 4. Troubleshooting

**Tools not visible in a Hermes session** (`tool_search` finds nothing).
The MCP server is spawned per session; if the controller venv is missing or
broken the connection fails and the server is parked (retried every 5 min).
Repair:

```sh
cd <clone> && python3 -m venv .venv && .venv/bin/pip install -e .
hermes mcp test opencode        # must list 6 tools
```

A session opened before the repair may still lack the tools — start a new
one.

**`Connection refused` on :4096.** The OpenCode server is down:
`systemctl --user status opencode-server`. The service is user-scoped — on a
headless box make sure lingering is enabled (`loginctl enable-linger <user>`).

**Install/upgrade health check hangs.** Any `curl` wait loop must stay
bounded (`--max-time 3`); a port that accepts TCP but never answers (another
process on 4096, blackholed SYNs) otherwise hangs silently. The installer
surfaces the last curl error and the systemd status on failure.

**Config changes not picked up.** OpenCode loads project config (agents,
commands, skills in `.opencode/`) at **server startup** and caches it. After
changing `~/.config/opencode/opencode.json` or project agents:
`systemctl --user restart opencode-server`. Sessions survive (SQLite DB);
in-memory `always` permission approvals are lost.

**Hermes upgrade changed the MCP config layout.** Re-read the new Hermes
docs for the `mcp_servers` schema, port section 1's block over, keep the
launcher path and the two timeouts, and re-verify with `hermes mcp test
opencode`. Nothing in this package depends on a specific Hermes version
beyond the `mcp_servers` mechanism.

## 5. Uninstall

```sh
scripts/uninstall.sh              # service, launchers, venv, Hermes entry, creds
scripts/uninstall.sh --purge      # + OpenCode provider config + API key
scripts/uninstall.sh --purge-binary   # + OpenCode binary
```

The git clone is never touched. A new Hermes session is required after
removing the config entry.

## 6. Operator skill (recommended)

A Hermes skill teaches the supervisor LLM the delegation protocol (agent
choice, answering questions/permissions itself, the no-poll rule, recovery).
The installer deliberately does **not** install one for you — Hermes's skill
layout may change, so it is your call. A complete, ready-to-copy template is
shipped in this package:

```sh
# adapt the paths/versions, then:
cp docs/skill.example.md ~/.hermes/skills/autonomous-ai-agents/opencode/SKILL.md
```

See `docs/skill.example.md` for the full protocol and the placeholders to
fill in.
