# opencode-hermes-mcp

Deterministic MCP controller between **Hermes** (supervisor LLM) and the
permanent **OpenCode** server. The controller is a state machine — no LLM —
that blocks on OpenCode turns and surfaces questions/permissions to Hermes so
the supervisor LLM can decide and resume the *same* turn.

## Architecture

```
Hermes (LLM)  --MCP stdio-->  server.py (FastMCP, 6 tools)  --HTTP + SSE-->  OpenCode server :4096
```

- **Layer 1 — Hermes**: the supervisor LLM. It delegates a coding task with
  `opencode_run` and decides when the controller reports
  `needs_agent_input` (question / permission).
- **Layer 2 — this controller** (`server.py` + `controller.py` + `client.py`
  + `models.py`): a NO-LLM process spawned by Hermes over MCP stdio. It
  submits the task, watches SSE + REST, blocks until the turn completes /
  errors / needs input, and posts the supervisor's decisions back into the
  same OpenCode turn (the prompt is never resubmitted).
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

`install.sh` is idempotent — re-running it skips what is already in place.
It installs the pinned OpenCode binary, the venv (`mcp==1.12.4`), the LLM
provider config + secret, the server credentials, the two launchers, the
systemd user service, and patches `~/.hermes/config.yaml` (backup kept as
`.bak`). It finishes with a health check + `smoke_client.py` (must print
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
`--port N` (default 4096), `--skip-binary`, `--force-config`, `--dry-run`.

`UNSLOTH_API_KEY` is still accepted as a deprecated fallback for
`OPENCODE_API_KEY` (existing scripts keep working).

**A new Hermes session is required** after installation to load the MCP
server.

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

## Upgrade / uninstall

```sh
scripts/upgrade.sh            # controller only: git pull + venv deps + restart + smoke
scripts/upgrade.sh --binary   # upgrade the OpenCode BINARY (latest) — see warning below
scripts/uninstall.sh          # service, launchers, venv, hermes entry, credentials
scripts/uninstall.sh --purge  # + OpenCode provider config + API key secret
scripts/uninstall.sh --purge-binary  # + the OpenCode binary
```

`uninstall.sh` never touches the git clone, the OpenCode provider config, the
API key secret, or the binary (unless the purge flags say so).

## Version pin: OpenCode 1.18.18

The controller is **validated against OpenCode `1.18.18` only** (its endpoint
contract was verified against that binary's live `/doc`, not the web docs).
`install.sh` pins the binary to `1.18.18`; `upgrade.sh` never upgrades the
binary by default.

If you upgrade the binary (`scripts/upgrade.sh --binary [VERSION]`), the
script warns you and you MUST re-validate the controller before trusting it:

```sh
.venv/bin/python tests/run_tests.py
```

(all checks must pass; the suite drives the controller over MCP stdio against
the live server). If it fails, pin back: `scripts/upgrade.sh --binary 1.18.18`.

## Timeouts

Three independent timeouts bound the pipeline: the **controller** run timeout
(`DEFAULT_RUN_TIMEOUT` = 3600 s — a single `opencode_run`/`opencode_answer`/
`opencode_permission` call gives up after an hour), the **MCP** server
timeout in `~/.hermes/config.yaml` (`mcp_servers.opencode.timeout` = 14400 s,
`connect_timeout` = 30 s), and the **Hermes tools** timeouts
(`timeouts.tools.sequential_call` / `concurrent_batch` = 14400 s) — the outer
two are set 4x above the controller's so a long-but-healthy turn is never
killed by the supervisor layer.

## Files

| File | Role |
| --- | --- |
| `server.py` | FastMCP stdio server (the 6 tools) |
| `controller.py` | state machine: submit / wait / resume / classify |
| `client.py` | HTTP + SSE client for the OpenCode server |
| `models.py` | dataclasses for turns / interactions |
| `smoke_client.py` | no-LLM smoke test (tool surface + basic calls) |
| `tests/run_tests.py` | full integration suite (live LLM turns) |
| `scripts/install.sh` / `uninstall.sh` / `upgrade.sh` | lifecycle |
