# Contributing

Thanks for helping improve `opencode-hermes-mcp`. This document covers the
development setup, how to run the tests, and the contribution conventions.

## Version pin

The pin is a **single source of truth** in `opencode_hermes_mcp/pin.txt`
(one line, no `v` prefix). The controller is **validated against the pinned
version only** (its endpoint contract was verified against that binary's live
`/doc`). `scripts/install.sh` pins the binary to that version. If you test
against a different OpenCode version, the controller's behaviour is NOT
guaranteed — re-run the full integration suite and report any divergence.

## Development setup

Python >= 3.11 is required.

With [uv](https://docs.astral.sh/uv/) (recommended):

```sh
uv venv .venv
uv pip install --python .venv/bin/python -e '.[dev]'
```

Or with the standard tooling:

```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

The `dev` extra installs `pytest` and `ruff`. Lint with:

```sh
.venv/bin/ruff check .
```

## Running the smoke test (no LLM)

The smoke test drives the controller over MCP stdio against a **live OpenCode
server** but makes **no LLM calls** (tool surface + validation + session
listing only):

```sh
.venv/bin/python -m opencode_hermes_mcp.smoke_client
```

Credentials are read from the environment when set:

- `OPENCODE_SERVER_URL` (e.g. `http://127.0.0.1:4096`)
- `OPENCODE_SERVER_USERNAME`
- `OPENCODE_SERVER_PASSWORD`

Otherwise they are read from `~/.config/hermes/opencode-server.json` (written
by `scripts/install.sh`). The test directory defaults to
`/tmp/oc-mcp-test/gitrepo` and is overridable with `OPENCODE_MCP_TEST_DIR`.

## Running the integration suite (live LLM)

`tests/run_tests.py` is the reference integration suite (15 tests). It drives
the controller over MCP stdio exactly like Hermes does, against a **live
OpenCode server with a working LLM provider** — several tests run real LLM
turns.

Prerequisites:

- a live OpenCode server (see `scripts/install.sh`), healthy on
  `OPENCODE_SERVER_URL` (default `http://127.0.0.1:4096`);
- credentials available via the `OPENCODE_SERVER_URL` /
  `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` env vars **or**
  `~/.config/hermes/opencode-server.json`;
- the test project directories (defaults, overridable):
  - `OPENCODE_MCP_TEST_DIR` — default `/tmp/oc-mcp-test/gitrepo` (must contain
    the project agents used by the suite, e.g. `tester`);
  - `OPENCODE_MCP_RESEARCH_DIR` — default `/tmp/oc-mcp-test/research-repo`
    (optional; the `custom_agent_e2e` test is skipped when absent).

Run the full suite (from the repo root):

```sh
.venv/bin/python tests/run_tests.py
```

Run a single test by name filter:

```sh
.venv/bin/python tests/run_tests.py agent_validation
```

The suite is sequential by design (one OpenCode turn at a time — the
controller enforces it).

## Conventions

- **Branches**: work on a `feat/<topic>` or `fix/<topic>` branch from `main`;
  never push directly to `main`.
- **Commits**: conventional-commit style messages
  (`feat: ...`, `fix: ...`, `refactor: ...`, `chore: ...`), imperative mood,
  one logical change per commit.
- **Merge requests**: open an MR against `main` with a short description of
  the change and how it was validated (smoke / integration suite output).
  CI (ruff + build + smoke) must pass.
- **Controller logic**: changes to the controller's behaviour (state machine,
  completion heuristics, endpoint contract) MUST be validated against the
  pinned OpenCode version (`opencode_hermes_mcp/pin.txt`) with the full
  integration suite before merging.
