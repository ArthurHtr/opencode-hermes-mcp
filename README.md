# opencode-hermes-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](#prerequisites)
[![OpenCode](https://img.shields.io/badge/OpenCode-1.18.21%20(pinned)-brightgreen.svg)](#version-pin-opencode-11821)

Deterministic MCP controller between **Hermes** (supervisor LLM) and the
permanent **OpenCode** server. The controller is a state machine — no LLM —
that blocks on OpenCode turns and surfaces questions/permissions to Hermes so
the supervisor LLM can decide and resume the *same* turn.

## Architecture

```
Hermes (LLM)  --MCP stdio-->  opencode_hermes_mcp.server (FastMCP, 6 tools)  --HTTP + SSE-->  OpenCode server :4096
```

- **Layer 1 — Hermes**: the supervisor LLM. It delegates a coding task with
  `opencode_run` and decides when the controller reports
  `needs_agent_input` (question / permission).
- **Layer 2 — this controller** (`opencode_hermes_mcp/`: `server.py` +
  `controller.py` + `client.py` + `models.py`): a NO-LLM process spawned by
  Hermes over MCP stdio. It submits the task, watches SSE + REST, blocks until
  the turn completes / errors / needs input, and posts the supervisor's
  decisions back into the same OpenCode turn (the prompt is never resubmitted).
- **Layer 3 — OpenCode server**: a permanent `opencode serve` process
  (systemd user service `opencode-server`, loopback :4096, HTTP basic auth).
  Its LLM is any supported provider (OpenAI-compatible endpoint, OpenAI, or
  Anthropic), configured in `~/.config/opencode/opencode.json`.

Tools exposed to Hermes: `opencode_run`, `opencode_answer`,
`opencode_permission`, `opencode_abort`, `opencode_inspect` (diagnostic
only), `opencode_sessions`.

## Prerequisites

- Hermes installed (`~/.hermes/config.yaml` present)
- `python3` >= 3.11 (with PyYAML for the Hermes config patch)
- Network access (OpenCode binary install, `mcp` package, LLM endpoint)
- `systemd` user sessions (for the `opencode-server` service)

## Installation (2 commands)

```sh
git clone <repo-url> opencode-hermes-mcp && cd opencode-hermes-mcp
scripts/install.sh
```

`scripts/install.sh` is a thin wrapper around the **setup wizard**
(`opencode_hermes_mcp/installer.py`, Python + `rich`): banner, numbered
steps, styled prompts, progress, and a summary panel. The wizard bootstraps
itself — if the repo venv is missing (or lacks `rich` / `pyyaml` /
`mcp==1.12.4` / the editable package), it creates it and re-execs, so a bare
`python3` >= 3.11 is the only prerequisite.

The install is idempotent — re-running it skips what is already in place.
It installs the pinned OpenCode binary, the venv (the `opencode_hermes_mcp`
package with `mcp==1.12.4` pinned), the LLM provider config + secret, the
server credentials, the two launchers, the systemd user service, and patches
`~/.hermes/config.yaml` (backup kept as `.bak`). It finishes with a health
check (bounded `curl --max-time 3`, last error surfaced) +
`python -m opencode_hermes_mcp.smoke_client` (must print
`tool surface OK`).

### LLM providers

The installer is provider-agnostic. Three providers are supported:

| Provider | Use | npm package |
| --- | --- | --- |
| `openai-compatible` | any OpenAI-compatible endpoint (Unsloth, Ollama, vLLM, llama-server, ...) — default | `@ai-sdk/openai-compatible` |
| `openai` | official OpenAI API | `@ai-sdk/openai` |
| `anthropic` | official Anthropic API | `@ai-sdk/anthropic` |

Interactive: pick the provider from the menu, then answer the prompts —
base URL + API key + model for `openai-compatible`, API key + model for
`openai` / `anthropic` — then the LLM speed (`slow` for a local LLM, which
adds `timeout:false` / `headerTimeout:false` / `chunkTimeout:120000` to the
provider options; `fast` is the default) and the model limits (context /
output, defaults 128000 / 32000).

Non-interactive (`--yes`), everything comes from env. Local
OpenAI-compatible endpoint (Ollama / vLLM / Unsloth / ...):

```sh
OPENCODE_PROVIDER=openai-compatible \
OPENCODE_LLM_BASE_URL=http://127.0.0.1:11434/v1 \
OPENCODE_API_KEY=... \
OPENCODE_LLM_MODEL=qwen3.8-27b \
OPENCODE_LLM_SPEED=slow \
scripts/install.sh --yes
```

OpenAI (cloud):

```sh
OPENCODE_PROVIDER=openai OPENCODE_API_KEY=sk-... OPENCODE_LLM_MODEL=gpt-4o \
scripts/install.sh --yes
```

Anthropic (cloud):

```sh
OPENCODE_PROVIDER=anthropic OPENCODE_API_KEY=sk-ant-... \
OPENCODE_LLM_MODEL=claude-sonnet-4-5 scripts/install.sh --yes
```

Flags: `--yes` (non-interactive, uses env `OPENCODE_PROVIDER` /
`OPENCODE_LLM_BASE_URL` / `OPENCODE_API_KEY` / `OPENCODE_LLM_MODEL` /
`OPENCODE_LLM_SPEED` / `OPENCODE_CONTEXT_LIMIT` / `OPENCODE_OUTPUT_LIMIT`),
`--port N` (default 4096), `--skip-binary`, `--force-config`, `--dry-run`,
`--skip-verify` (skip the final health + smoke verification — useful for
sandbox/CI).

`UNSLOTH_API_KEY` is still accepted as a deprecated fallback for
`OPENCODE_API_KEY` (existing scripts keep working).

**A new Hermes session is required** after installation to load the MCP
server.

## Hermes integration (manual)

The installer patches `~/.hermes/config.yaml` for you, but it does **not**
install a Hermes skill on purpose (Hermes's skill layout may change). The
package ships the full manual instead:

- [`docs/hermes-integration.md`](docs/hermes-integration.md) — what the MCP
  is for, the exact config entry written, manual integration (by hand), the
  six tools, troubleshooting, uninstall.
- [`docs/skill.example.md`](docs/skill.example.md) — a ready-to-copy Hermes
  skill (the delegation protocol) to drop into `~/.hermes/skills/` and adapt.

## Usage

Hermes delegates work through the MCP tools — no manual CLI needed:

- `opencode_run(directory, task, agent)` — submit a task; blocks until the
  turn completes, errors, or needs input. `agent` is required for a new
  session (a primary agent of the project, e.g. `build`, `plan`, or a
  project-specific agent).
- When a tool returns `state=needs_agent_input`, Hermes decides:
  `opencode_answer` (pick exact option labels) or `opencode_permission`
  (`once` / `always` / `reject`) — both resume the same turn.
- `opencode_abort` stops a stuck run; `opencode_sessions` lists sessions for
  a directory; `opencode_inspect` is for exceptional diagnostics only (never
  poll a running task).

The Hermes-side wiring (written by `scripts/install.sh` into
`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  opencode:
    command: ~/.local/bin/opencode-mcp-launch.sh
    enabled: true
    timeout: 14400
    connect_timeout: 30
    supports_parallel_tool_calls: false
timeouts:
  tools:
    sequential_call: 14400
    concurrent_batch: 14400
```

The launcher reads the OpenCode server credentials from
`~/.config/hermes/opencode-server.json` and execs
`python -m opencode_hermes_mcp.server` in the repo venv — `config.yaml` stays
secret-free.

## TUI attach helpers (watch OpenCode live)

`install.sh` also drops two helpers into `~/.local/bin/` (sources:
`scripts/helpers/`):

```sh
ocattach <repo-abs> [ses_...]   # open the OpenCode TUI on a repo / session
oc-current                      # attach to the session Hermes is supervising NOW
```

- `ocattach` opens the OpenCode TUI (`opencode attach`) against the permanent
  server `:4096` — no tmux needed. Without a session id it opens the latest
  session / lets you pick one.
- `oc-current` reads the newest `~/.local/state/opencode-hermes-mcp/turn_*.json`
  (the controller's in-flight turn state) and attaches to that session — use
  it while Hermes is driving OpenCode, to watch the reasoning live.

Both read the server credentials from `~/.config/hermes/opencode-server.json`
(same source as the controller launcher). **Do not press Esc/Ctrl+C in the TUI
while a turn is active** — that aborts the in-flight turn on the OpenCode side.

## Upgrade / uninstall

```sh
scripts/upgrade.sh            # controller only: git pull + venv deps + restart + smoke
scripts/upgrade.sh --binary   # install the PINNED OpenCode binary (idempotent) — see "Version pin" below
scripts/uninstall.sh          # service, launchers, venv, hermes entry, credentials
scripts/uninstall.sh --purge  # + OpenCode provider config + API key secret
scripts/uninstall.sh --purge-binary  # + the OpenCode binary
```

`uninstall.sh` never touches the git clone, the OpenCode provider config, the
API key secret, or the binary (unless the purge flags say so).

## Version pin: OpenCode 1.18.21

The controller is **validated against OpenCode `1.18.21` only** (its endpoint
contract was verified against that binary's live `/doc`, not the web docs).
The pin is a **single source of truth** in `opencode_hermes_mcp/pin.txt`
(one line, no `v` prefix): `installer.py` and `scripts/upgrade.sh` both read
it, falling back to the built-in constant when the file is missing or empty
(e.g. pip installs where the file is not shipped next to the code). `install.sh` pins
the binary to that version; `upgrade.sh` never upgrades the binary by
default.

`scripts/upgrade.sh --binary` (no version) installs the pinned version and is
idempotent (no-op if the binary is already at the pin). `--binary latest` is
the explicit opt-in to the bleeding edge; `--binary X.Y.Z` installs the
requested version. For anything other than the pin, the script warns you and
you MUST re-validate the controller before trusting it:

```sh
.venv/bin/python tests/run_tests.py
```

(all checks must pass; the suite drives the controller over MCP stdio against
the live server). If it fails, pin back: `scripts/upgrade.sh --binary`.

## Timeouts

Three independent timeouts bound the pipeline: the **controller** run timeout
(`DEFAULT_RUN_TIMEOUT` = 3600 s — a single `opencode_run`/`opencode_answer`/
`opencode_permission` call gives up after an hour), the **MCP** server
timeout in `~/.hermes/config.yaml` (`mcp_servers.opencode.timeout` = 14400 s,
`connect_timeout` = 30 s), and the **Hermes tools** timeouts
(`timeouts.tools.sequential_call` / `concurrent_batch` = 14400 s) — the outer
two are set 4x above the controller's so a long-but-healthy turn is never
killed by the supervisor layer.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup, how to run the
smoke test and the integration suite, and the contribution conventions.

## Files

| File | Role |
| --- | --- |
| `opencode_hermes_mcp/server.py` | FastMCP stdio server (the 6 tools) |
| `opencode_hermes_mcp/controller.py` | state machine: submit / wait / resume / classify |
| `opencode_hermes_mcp/client.py` | HTTP + SSE client for the OpenCode server |
| `opencode_hermes_mcp/models.py` | data helpers for turns / interactions |
| `opencode_hermes_mcp/smoke_client.py` | no-LLM smoke test (tool surface + basic calls) |
| `tests/run_tests.py` | full integration suite (live LLM turns) |
| `opencode_hermes_mcp/installer.py` | setup wizard (Python + rich; self-bootstrapping venv) |
| `opencode_hermes_mcp/pin.txt` | the OpenCode version pin (single source of truth, one line) |
| `scripts/install.sh` / `uninstall.sh` / `upgrade.sh` | lifecycle (`install.sh` is a thin wrapper around the wizard) |
| `scripts/helpers/ocattach` / `oc-current` | TUI attach helpers (installed to `~/.local/bin/`) |

## License

[MIT](LICENSE) — Copyright (c) 2026 Arthur Hottier.
