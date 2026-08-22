#!/usr/bin/env python3
"""
opencode-hermes-mcp — deterministic controller between Hermes (MCP client) and
the permanent OpenCode server.

    Hermes  --MCP stdio-->  this server  --HTTP + SSE-->  OpenCode :4096

This process is NO LLM: a state machine + HTTP/SSE client (see controller.py,
client.py, models.py). The Hermes LLM is the supervisor: it delegates a task
with opencode_run, and when OpenCode needs input (question / permission) the
controller returns `needs_agent_input` to the Hermes LLM, which decides and
calls opencode_answer / opencode_permission. Those tools post the decision and
re-enter the SAME blocking wait — the same OpenCode turn, never a resubmitted
prompt.

Tools (final surface):
  opencode_run        submit a task, block until completed/error/needs input
  opencode_answer     answer a pending OpenCode question, keep waiting
  opencode_permission decide a pending OpenCode permission, keep waiting
  opencode_abort      abort the active (or given) session
  opencode_inspect    DIAGNOSTIC ONLY — never poll a running task
  opencode_sessions   list sessions for a directory

Credentials come from the environment (injected by the launcher):
  OPENCODE_SERVER_URL / OPENCODE_SERVER_USERNAME / OPENCODE_SERVER_PASSWORD

Target: OpenCode 1.18.18. Endpoint contract = the live server's /doc,
verified against the installed binary — not the web docs.
"""

# NOTE: no `from __future__ import annotations` here — FastMCP inspects
# parameter annotations at runtime to detect the Context injection point, and
# stringified annotations break that detection.

import os
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import OpenCode
from .controller import DEFAULT_RUN_TIMEOUT, Controller

OC = OpenCode()
CTRL = Controller(OC)

# PID file (used by tests to kill the controller process mid-turn to simulate
# an MCP restart). Best-effort; ignored if the env var is not set.
_PID_FILE = os.environ.get("OPENCODE_MCP_PID_FILE")


def _write_pid() -> None:
    if not _PID_FILE:
        return
    try:
        with open(_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _remove_pid() -> None:
    if not _PID_FILE:
        return
    try:
        os.remove(_PID_FILE)
    except OSError:
        pass


@asynccontextmanager
async def _lifespan(_: FastMCP):
    # SSE subscriptions are per-wait (scoped by directory); nothing global to
    # start here. The httpx client is created lazily and closed on shutdown.
    _write_pid()
    try:
        yield
    finally:
        _remove_pid()
        await OC.aclose()


server = FastMCP(
    name="opencode",
    debug=False,
    instructions=(
        "Supervise the permanent OpenCode server. Delegate coding tasks with "
        "opencode_run — it blocks until the turn completes, errors, or needs "
        "input. NEVER poll with opencode_inspect while waiting: opencode_run, "
        "opencode_answer and opencode_permission wait for you. When a tool "
        "returns state=needs_agent_input, YOU decide (question: pick the exact "
        "option labels; permission: once/always/reject) and call "
        "opencode_answer or opencode_permission to resume the same turn. "
        "opencode_inspect is for exceptional diagnostics only."
    ),
    lifespan=_lifespan,
)


def _busy_error() -> dict[str, Any]:
    active_sid = (CTRL._active or {}).get("session_id")
    return {
        "ok": False,
        "error": (
            "another OpenCode operation is already in progress (strictly "
            "sequential). Use opencode_abort to stop it, or wait."
        ),
        "active_session": active_sid,
    }


@server.tool()
async def opencode_run(
    directory: str,
    task: str,
    agent: str | None = None,
    model: str | None = None,
    session_id: str | None = None,
    timeout: float = DEFAULT_RUN_TIMEOUT,
) -> dict[str, Any]:
    """Delegate a coding task to OpenCode and block until it completes, errors,
    or needs input (a question or a permission).

    - New task: pass `directory` + `task` + `agent` (a new session is created).
      `agent` is REQUIRED for a new session.
    - Continuation / resume: pass `session_id` (+ `task` for a NEW turn on that
      session, or `task` is ignored when the turn is still in flight). `agent`
      is not needed to resume an in-flight turn (it is taken from the turn's
      durable state); pass it only when starting a fresh turn on an existing
      session.
    - `agent`: the OpenCode agent to run as root. Free string, validated
      dynamically against the project's live agent list (GET /agent). It MUST
      be a primary agent of that directory (project-specific primary agents
      are preferred when they fit the task; `build` is the generic
      implementation agent; `plan` is read-only). Subagents are rejected as
      root. Do not rely on the server's default_agent: always choose
      explicitly for a new session.
    - `model`: optional 'provider/model' override.

    RESUME: if `session_id` is given and that session still has a turn in
    flight (busy/retry) — e.g. the controller restarted mid-turn — the prompt
    is NOT resubmitted: the wait loop simply resumes on the SAME turn
    (`task` and `agent` are ignored in that case).

    The call blocks until the turn ends. While OpenCode works, NOTHING is
    polled — the controller watches SSE + REST internally. If OpenCode asks a
    question or requests a permission, the call returns
    state='needs_agent_input' (kind='question' or 'permission') with everything
    needed to decide; answer with opencode_answer / opencode_permission, which
    resume the SAME turn. Returns the final assistant text + diff on completion.
    """
    if CTRL._lock.locked():
        return _busy_error()
    async with CTRL._lock:
        return await CTRL.run(
            directory=directory,
            task=task,
            agent=agent,
            model=model,
            session_id=session_id,
            timeout=timeout,
        )


@server.tool()
async def opencode_answer(
    directory: str,
    session_id: str,
    question_id: str,
    answers: list[Any],
    timeout: float = DEFAULT_RUN_TIMEOUT,
) -> dict[str, Any]:
    """Answer a pending OpenCode question (state=needs_agent_input,
    kind=question) and keep blocking until the turn completes, errors, or
    needs input again.

    - `answers`: ONE entry per sub-question, in order. Each entry is a string
      or a list of strings. When a sub-question offers options and does not
      allow custom answers, each value MUST be an exact option label (the
      server rejects anything else — this tool validates before posting).
    - The answer is posted to OpenCode and the SAME turn resumes (the prompt
      is never resubmitted).
    - If the question is no longer pending (already answered/consumed), an
      error is returned; the turn may have moved on.
    """
    if CTRL._lock.locked():
        return _busy_error()
    async with CTRL._lock:
        return await CTRL.answer(
            directory=directory,
            session_id=session_id,
            question_id=question_id,
            answers=answers,
            timeout=timeout,
        )


@server.tool()
async def opencode_permission(
    directory: str,
    session_id: str,
    permission_id: str,
    reply: str,
    timeout: float = DEFAULT_RUN_TIMEOUT,
) -> dict[str, Any]:
    """Decide a pending OpenCode permission (state=needs_agent_input,
    kind=permission) and keep blocking until the turn completes, errors, or
    needs input again.

    - `reply`: exactly one of 'once' (allow this call), 'always' (allow this
      pattern for the session), 'reject'. Decide as supervisor: allow normal
      actions necessary for the delegated task; reject destructive or
      out-of-scope requests.
    - The decision is posted to OpenCode and the SAME turn resumes (the prompt
      is never resubmitted).
    - If the permission is no longer pending, an error is returned.
    """
    if CTRL._lock.locked():
        return _busy_error()
    async with CTRL._lock:
        return await CTRL.permission(
            directory=directory,
            session_id=session_id,
            permission_id=permission_id,
            reply=reply,
            timeout=timeout,
        )


@server.tool()
async def opencode_abort(session_id: str | None = None) -> dict[str, Any]:
    """Abort the active OpenCode session (or a specific one). Does not require
    the run lock, so it can stop a stuck run."""
    return await CTRL.abort(session_id)


@server.tool()
async def opencode_inspect(session_id: str | None = None) -> dict[str, Any]:
    """DIAGNOSTIC ONLY: one-shot snapshot of a session (status, tree, pending
    permissions/questions, last assistant text). NEVER use this to poll or
    monitor a running task — opencode_run / opencode_answer /
    opencode_permission block until the turn ends; polling wastes tokens and
    is forbidden. Use only for exceptional diagnostics (after a timeout, or to
    inspect a session you did not start)."""
    return await CTRL.inspect(session_id)


@server.tool()
async def opencode_sessions(directory: str) -> dict[str, Any]:
    """List OpenCode sessions for a directory (to pick a session_id to reuse)."""
    return await CTRL.sessions(directory)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
