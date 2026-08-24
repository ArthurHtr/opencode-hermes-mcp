---
name: opencode
description: Delegate coding to OpenCode via the MCP controller (opencode_run blocks; questions/permissions come back to Hermes, which answers them itself).
version: 1.0.0
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [Coding-Agent, OpenCode, Supervision, Development, MCP]
---

# OpenCode Supervisor Skill (MCP only)

<!-- TEMPLATE — copy this file to ~/.hermes/skills/<category>/opencode/SKILL.md
     and replace every <PLACEHOLDER> below. The installer does NOT install
     this for you on purpose (Hermes's skill layout may change). -->

Use OpenCode as Hermes's coding worker. Hermes is the **supervisor**: it
chooses the task, the repo, and the primary agent, delegates it, answers
OpenCode's questions and permission requests **itself**, and evaluates the
result. The user does not need to be present for a normal run.

**Delegation goes EXCLUSIVELY through the `opencode` MCP controller.** One
blocking `opencode_run` call returns only when the turn completes, errors, is
aborted, times out — or needs Hermes input. While OpenCode works, Hermes
spends **zero tokens** watching: the controller (deterministic Python, no
LLM) does all the SSE/REST monitoring. Questions and permissions come back
to Hermes as a structured `needs_agent_input` result, and Hermes answers them
with its own context. Do NOT drive OpenCode through shell scripts or raw
curl — the MCP tools are the only path.

## How to delegate

```text
mcp__opencode__opencode_run(
    directory="<abs/repo>",
    agent="<primary agent>",   # REQUIRED for a NEW session
    task="...",                # the full, self-contained task
    timeout=3600               # optional, seconds
)
```

Pass `session_id="ses_..."` to continue an existing session (same primitive;
`session_id` present = continuation). When resuming a session whose turn is
still in flight (e.g. after a controller restart), the prompt is NOT
resubmitted — the wait loop resumes on the SAME turn (`task`/`agent` are
ignored; pass a placeholder like `task="resume"`). The call blocks until the
next significant event. You will get back exactly one of:

- `{"state": "completed", ...}` — done. Read `last_assistant_text`, `diff`,
  `changed_files`, `timing`.
- `{"state": "needs_agent_input", "kind": "question", ...}` — see Questions.
- `{"state": "needs_agent_input", "kind": "permission", ...}` — see Permissions.
- `{"state": "error", "code": ..., ...}` — see Errors.
- `{"state": "aborted", ...}` / `{"state": "timeout", ...}`.

## Agents

`agent` is an explicit free string, validated **dynamically** against
`GET /agent?directory=...` (no hardcoded list, no `build` default).

1. If the user names an agent explicitly, use exactly that one (if valid).
2. Otherwise pick the **primary agent best suited to the mission**.
3. **Project-specific primary agents win** when they clearly fit the task —
   a repo that defines a dedicated agent for the job should be driven with
   that agent, never mechanically replaced by `build`.
4. `build` is the right choice for generic implementation when no better
   primary agent exists.
5. Never rely silently on `default_agent` (it may be `plan`, which is
   read-only). Always make an explicit choice.

Only `mode == "primary"` agents may be roots. A `subagent` is refused with
`{"state": "error", "code": "invalid_primary_agent", "mode": "subagent"}`;
an unknown agent with `{"state": "error", "code": "agent_not_found"}` (both
include the list of available primary agents).

The primary agent then uses its own subagents internally — that is OpenCode's
business, not Hermes'. The controller appends a guard requiring subagents to
run strictly sequentially.

## Questions (kind=question)

When OpenCode asks a question, the controller returns:

```json
{
  "state": "needs_agent_input",
  "kind": "question",
  "session_id": "ses_...",
  "directory": "<abs/repo>",
  "question_id": "q_...",
  "question": { "...": "raw OpenCode data" },
  "options": ["exact labels if present"]
}
```

**Answer it yourself** using: the user's original request, the conversation
history, constraints already given, project context, and technical judgment.
Do NOT ask the user to answer just because OpenCode asked. If options/labels
are present and the API requires one, pick **exactly one valid label**.

```text
mcp__opencode__opencode_answer(directory, session_id, question_id, answers=[...])
```

This does NOT return immediately: it posts the answer and **re-enters the
same blocking wait on the same turn**, returning at the next completed /
needs_agent_input / error / aborted / timeout. There may be several questions
in a row — loop until a terminal state.

## Permissions (kind=permission)

When OpenCode requests a permission, the controller returns:

```json
{
  "state": "needs_agent_input",
  "kind": "permission",
  "session_id": "ses_...",
  "directory": "<abs/repo>",
  "permission_id": "perm_...",
  "permission": { "full": "OpenCode data (type, patterns, metadata)" },
  "allowed_replies": ["once", "always", "reject"]
}
```

**Decide it yourself** as supervisor: weigh the task you delegated, the repo,
whether the action is necessary, its impact, the user's constraints, and
system safety.

- `once` — default for normal, task-relevant actions.
- `always` — only when the scope is clearly safe and reusable (same pattern
  will recur).
- `reject` — destructive, unrelated to the task, unexpectedly external, or
  intent-inconsistent.

Do not mechanically refuse normal development actions, and do not ask the
user for approval on each operation. But do not blindly approve operations
clearly outside the task scope.

```text
mcp__opencode__opencode_permission(directory, session_id, permission_id, reply="once")
```

**PITFALL: the `session_id` to pass is the permission's, not the result's.**
When a subagent (delegated by the primary) asks for a permission, the
`needs_agent_input` result carries the ROOT session's `session_id`, but the
permission belongs to the **subagent's** session (`permission.sessionID`).
Answering with the root session id fails: `permission ... belongs to session
ses_..., not ses_...`. Use `permission.sessionID`.

Same semantics as `opencode_answer`: posts the decision, re-enters the
blocking wait on the same turn.

## Monitoring — the absolute rule

```text
NEVER poll opencode_inspect while waiting for a normal run.
```

`opencode_run`, `opencode_answer`, and `opencode_permission` each wait for
you. The controller owns all SSE/REST monitoring, reconnection, and
reconciliation. `opencode_inspect` is **diagnostic only** (status, pending
q/p, last text, tree) — for after a timeout, for a session you did not start,
or for forensics. It is never a substitute for the blocking wait.

## Recovery — `output_limit_reached` (two stages)

Stage 1 (prevention): an anti-loop guard in the agent prompt.
Stage 2 (recovery): the controller detects `finish=length` **without a
deliverable** (empty diff AND text < 200 chars) and returns:

```json
{"state": "error", "code": "output_limit_reached", "session_id": "ses_...",
 "error": "agent exhausted its output budget (finish='length') before producing a deliverable",
 "hint": "send a short corrective turn on the SAME session ..."}
```

**Supervisor reaction (automatic, no user):** send a short corrective turn on
the **SAME session** (`opencode_run` with the same `session_id` + agent):
"You reached the output limit after already identifying the conclusion. Do
NOT re-analyse the problem. Take exclusively the conclusions already
established in the previous turn and execute now [the pending action]." The
session keeps all context — the agent only has to act. **New session +
summary = last resort** (session truly unrecoverable) — an exceptional
operation, not the normal behaviour.

## Pitfalls

PITFALL: **`default_agent` may be read-only (`plan`).** Always pass an
explicit write-capable primary agent for implementation work, or the turn
"completes" and writes nothing.

PITFALL: **Project config (agents/commands/skills in `.opencode/`) loads at
server STARTUP and is cached.** After adding/modifying project agents:
`systemctl --user restart opencode-server` (sessions survive in the SQLite
DB; in-memory `always` permission approvals are lost — re-approve). Verify
with `GET /agent?directory=R`.

PITFALL: **`finish: length` = effective output-token cap — and that cap can
be LOWER than `limit.output`.** A model that re-deliberates without emitting
the writing tool exhausts the budget and is cut **with nothing written**.
Fixes: (1) anti-loop semantic guard in the agent prompt (act as soon as
verdict + target + content are determined); (2) controller recovery on
`finish=length` without deliverable (see Recovery). Do NOT "fix" it by
lowering `limit.output`.

PITFALL: **context can blow up reading large files.** An agent that reads a
multi-MB CSV/log in full overflows the window and loses the whole session
with no deliverable. Prevention: explicit prompt + agent rule (never read
> 100 Ko in full; `head`/`wc -l`/`grep -c`; never open `*.pt`/`*.mp4`/
`*.sqlite`/`*.xlsx`) + **write deliverables incrementally** (after each
explored module, never at session end).

PITFALL: **permission `always` approvals are in-memory, lost on restart.**
`bash` allow patterns in agent frontmatter must match the EXACT invocation
form (`python3*` does NOT match `.venv/bin/python ...`). For autonomous runs
pre-allow the realistic set and keep `"*": ask`.

PITFALL: **Directory scoping is mandatory on the API.** SSE (`/event`),
`/session/status`, `/session/{id}/diff`, and q/p replies all need
`?directory=<abs-repo>`; unscoped they return heartbeats/`[]`/404. The
controller handles this.

PITFALL: **`session.idle` is not terminal**, and a permission **reject** can
end a turn with the last assistant message stuck at `finish: tool-calls`, no
error, no new message. The controller's completion heuristic plus a
post-interaction stall detector covers this. Do not "simplify" it.

PITFALL: **the MCP `opencode` server is spawned per Hermes session; no
hot-reload.** If the controller venv at `<CLONE>` is missing or broken, the
connection fails (`Connection closed`) and the server is parked (retried
every 5 min) — the MCP tools are then NOT injected: `tool_search` finds
nothing. Repair: `cd <CLONE> && python3 -m venv .venv && .venv/bin/pip
install -e .`, then `hermes mcp test opencode` (must list 6 tools). A session
already open may keep lacking the tools after the revival — start a new one.
OpenCode binary is PINNED `<OPENCODE_VERSION>`: after any upgrade, re-validate
with `tests/run_tests.py`.

## Verification

```text
# Smoke (no LLM): 6 tools listed, validation errors correct
<CLONE>/.venv/bin/python -m opencode_hermes_mcp.smoke_client

# Connection from Hermes
hermes mcp test opencode        # "Tools discovered: 6"

# Integration suite (real LLM turns; long). Run one test:
<CLONE>/.venv/bin/python <CLONE>/tests/run_tests.py agent_validation
```

**The acceptance criterion that matters:** a deliberately long OpenCode task
produces exactly 1 `opencode_run` call from Hermes, N minutes of OpenCode
activity, **0** `opencode_inspect` calls, and 1 final result — zero Hermes
tokens spent watching.

## Placeholders to fill in this template

- `<CLONE>` — absolute path of the opencode-hermes-mcp clone
  (e.g. `~/gitlab/opencode-hermes-mcp`).
- `<OPENCODE_VERSION>` — the pinned OpenCode binary version
  (e.g. `1.18.18`).
- Agent names / permission pre-allow lists — adapt to your
  `~/.config/opencode/opencode.json`.
