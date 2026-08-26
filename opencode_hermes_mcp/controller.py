"""Deterministic controller between Hermes (MCP) and the OpenCode server.

No LLM, no inference. Responsibilities:
  * keep one SSE subscription per active wait (scoped to the turn's directory);
  * reconcile against REST (/session/status, /question, /permission, messages)
    on every SSE poke AND on a bounded safety timeout (SSE can drop);
  * block until the turn ends: completed / error / needs_agent_input / timeout;
  * persist a small durable turn state so that answering a question or
    permission works even after the controller process restarts.

Completion semantics (verified against live server 1.18.21 — see skill
reference mcp-controller.md):
  * /session/status (scoped) lists only ACTIVE sessions; absence = idle;
  * `session.idle` fires BETWEEN assistant messages too — NOT terminal;
  * complete = root+descendants not busy AND last assistant message has a
    terminal `finish` (anything but 'tool-calls') AND postdates submission.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from . import journal
from .client import OpenCode, OpenCodeError, event_stream
from .models import (
    assistant_text,
    clear_turn_state,
    diff_summary,
    last_assistant,
    last_user_message,
    load_turn_state,
    msg_created_ms,
    new_turn,
    parse_model,
    save_turn_state,
    validate_agent,
    validate_permission_reply,
    validate_question_answers,
)

# Safety: max seconds to wait on the SSE poke before forcing a REST reconcile.
RECONCILE_POKE_TIMEOUT = 30.0
# Max seconds a single blocking tool call may wait (Hermes mcp timeout >= this).
DEFAULT_RUN_TIMEOUT = 3600.0
# After we answer a question / decide a permission, if the session goes idle
# and makes NO further progress (no busy period, no new assistant message) for
# this long, the turn has been terminated by the interaction resolution — most
# notably a permission REJECT, where OpenCode 1.18.21 ends the turn with the
# last assistant message still at finish='tool-calls' and no error. Without
# this, the completion heuristic (finish != tool-calls) would wait forever.
INTERACTION_STALL_GRACE_MS = 45_000

SUPERVISOR_GUARD = """\
[Hermes supervision constraint]
You are the coding worker supervised by Hermes.
You may use your configured OpenCode subagents when useful, but run delegated
subagents strictly sequentially: one at a time, wait for each to finish before
starting the next, and never launch multiple subagents concurrently.
[/Hermes supervision constraint]"""

# Threshold for "anecdotal" assistant text on finish='length': a message
# shorter than this (after stripping) is NOT a deliverable — the model was
# cut off mid-preamble (e.g. a truncated "Let me analyze..."). 200 chars is
# far below any real answer yet comfortably above a fragment.
ANECDOTAL_TEXT_MAX_CHARS = 200


def classify_length_finish(diff_summary: dict[str, Any], text: str) -> str:
    """Classify a finish='length' completion: "completed" or "error".

    "completed" when a deliverable exists (non-empty diff OR substantive
    assistant text); "error" (output_limit_reached) when the model exhausted
    its output budget with neither — the supervisor must then send a short
    corrective turn on the SAME session.
    """
    has_diff = int(diff_summary.get("files") or 0) > 0 or bool(diff_summary.get("changed_files"))
    has_text = len((text or "").strip()) >= ANECDOTAL_TEXT_MAX_CHARS
    return "completed" if (has_diff or has_text) else "error"


class Controller:
    def __init__(self, oc: OpenCode) -> None:
        self.oc = oc
        self._lock = asyncio.Lock()
        self._active: dict[str, Any] | None = None  # turn currently being waited on
        self._pokes: dict[str, asyncio.Event] = {}
        self._aborted: set[str] = set()  # session ids we asked to abort
        self._sse_connected = False
        self._sse_reconnects = 0

    # ------------------------------------------------------------------ #
    # SSE lifecycle
    # ------------------------------------------------------------------ #

    def _poke(self, sid: str) -> asyncio.Event:
        ev = self._pokes.get(sid)
        if ev is None:
            ev = asyncio.Event()
            self._pokes[sid] = ev
        return ev

    def _poke_set(self, sid: str) -> None:
        self._poke(sid).set()

    async def _subscribe(self, turn: dict[str, Any]) -> None:
        """SSE subscription scoped to the turn's directory (fast path).

        CRITICAL: the stream must be scoped with ?directory — unscoped it
        only carries server.heartbeat, never session events. If the
        connection drops, the turn keeps working: the safety poke in the
        wait loop forces REST reconciliation every RECONCILE_POKE_TIMEOUT,
        and the first reconcile after reconnect re-checks pending
        questions/permissions (they are REST-persistent, so one that
        appeared during the drop is caught).
        """
        backoff = 1.0
        while True:
            try:
                async for event in event_stream(await self.oc.client(), turn["directory"]):
                    self._sse_connected = True
                    backoff = 1.0
                    self._handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — reconnect on any SSE error
                self._sse_connected = False
                self._sse_reconnects += 1
                # Nudge the turn so it reconciles via REST meanwhile.
                if self._active is turn:
                    self._poke_set(turn["session_id"])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        props = event.get("properties", {}) or {}
        sid = props.get("sessionID")
        if not sid:
            return
        active = self._active
        if not active:
            return
        root = active["session_id"]
        tree: set[str] = active["tree"]
        # Relevant only if the event is for the root or a known descendant.
        if sid != root and sid not in tree:
            return
        # Grow the tree when a child session is created under the root.
        if etype == "session.created":
            tree.add(sid)
        # Any of these means "something changed; reconcile".
        # NOTE: session.idle is NOT terminal by itself (it fires between
        # assistant messages); reconciliation decides via the finish reason.
        if etype in {
            "session.status",
            "session.idle",
            "session.error",
            "question.asked",
            "permission.asked",
            "session.created",
            "message.updated",
            "session.diff",
        }:
            self._poke_set(root)

    # ------------------------------------------------------------------ #
    # Session tree
    # ------------------------------------------------------------------ #

    async def _refresh_tree(self, root: str, tree: set[str]) -> set[str]:
        """Expand the session tree (root + descendants) via REST."""
        queue = [root]
        seen = {root}
        while queue:
            cur = queue.pop(0)
            for child in await self.oc.children(cur):
                cid = child.get("id")
                if cid and cid not in seen:
                    seen.add(cid)
                    queue.append(cid)
        tree.update(seen)
        return tree

    # ------------------------------------------------------------------ #
    # Reconciliation (the real work, via REST)
    # ------------------------------------------------------------------ #

    async def _reconcile(self, turn: dict[str, Any]) -> dict[str, Any]:
        root = turn["session_id"]
        directory = turn["directory"]
        tree = await self._refresh_tree(root, turn["tree"])
        statuses = await self.oc.status_map(directory)

        perms = [p for p in await self.oc.permissions(directory) if p.get("sessionID") in tree]
        quests = [q for q in await self.oc.questions(directory) if q.get("sessionID") in tree]

        # 1) needs-input takes priority
        # (skip requests we just answered — the server may list them briefly
        #  until the agent's turn consumes them)
        ignore: set[str] = turn.get("ignore_ids") or set()
        quests = [q for q in quests if q.get("id") not in ignore]
        perms = [p for p in perms if p.get("id") not in ignore]
        if quests:
            return {
                "state": "needs_agent_input",
                "kind": "question",
                "question": quests[0],
                "session_id": root,
                "tree": sorted(tree),
                "submitted_at_ms": turn["submitted_at_ms"],
            }
        if perms:
            return {
                "state": "needs_agent_input",
                "kind": "permission",
                "permission": perms[0],
                "session_id": root,
                "tree": sorted(tree),
                "submitted_at_ms": turn["submitted_at_ms"],
            }

        # 2) still working? (root or any descendant busy/retry)
        busy = any(
            (statuses.get(c) or {}).get("type") in {"busy", "retry"} for c in tree
        )
        now_ms = int(time.time() * 1000)

        # 2a) we asked to abort this session → report aborted promptly
        # (don't wait for the server to settle or the run timeout).
        if root in self._aborted:
            self._aborted.discard(root)
            return {
                "state": "aborted",
                "session_id": root,
                "agent": turn.get("agent"),
                "tree": sorted(tree),
                "note": "abort requested via opencode_abort",
            }
        if busy:
            # Progress is happening: reset the idle clock (the post-interaction
            # stall check below requires continuous idle for the grace window).
            turn["idle_since_ms"] = None
            return {"state": "working", "tree": sorted(tree)}
        # Not busy: start/keep the idle clock (used for post-interaction
        # stall detection below).
        if turn.get("idle_since_ms") is None:
            turn["idle_since_ms"] = now_ms

        # 3) not busy → is the turn actually finished, or just between steps?
        messages = await self.oc.messages(root, directory)
        last = last_assistant(messages)
        n_assistant = sum(
            1 for m in messages if (m.get("info") or {}).get("role") == "assistant"
        )

        # 3a) Post-interaction stall: we answered a question / decided a
        # permission, the session has been continuously idle for the grace
        # window, no new assistant message was created (count unchanged), the
        # last assistant message is a non-terminal tool-calls tail, and there
        # is no error. That is the signature of a turn TERMINATED by the
        # interaction resolution — most notably a permission REJECT, where
        # OpenCode 1.18.21 ends the turn leaving the last assistant message at
        # finish='tool-calls' with no error and no new message. Without this,
        # the completion heuristic (finish != tool-calls) would wait forever.
        #
        # Why count-based (not a "resumed" boolean): a transient 'busy' read
        # right after posting the reply would wrongly clear a boolean flag.
        # The count is authoritative: a resumed model creates a new assistant
        # message (count grows → this check is skipped) or updates the finish
        # to a terminal reason (caught by the normal heuristic below).
        count_at_answer = turn.get("assistant_count_at_answer")
        if (
            turn.get("answered_interaction")
            and count_at_answer is not None
            and n_assistant <= count_at_answer
            and last is not None
            and (last.get("info") or {}).get("finish") in ("tool-calls", None)
            and not (last.get("info") or {}).get("error")
            and turn.get("idle_since_ms") is not None
            and now_ms - turn["idle_since_ms"] >= INTERACTION_STALL_GRACE_MS
        ):
            user_msg = last_user_message(messages)
            diffs = await self.oc.diff(
                root, directory, message_id=(user_msg or {}).get("info", {}).get("id")
            )
            return {
                "state": "completed",
                "session_id": root,
                "agent": turn.get("agent"),
                "last_assistant_text": assistant_text(last),
                "diff": diff_summary(diffs),
                "tree": sorted(tree),
                "timing": {
                    "submitted_at_ms": turn["submitted_at_ms"],
                    "completed_at_ms": now_ms,
                    "elapsed_ms": now_ms - turn["submitted_at_ms"],
                },
                "note": (
                    "turn ended after the interaction was resolved (the model "
                    "did not resume); the last assistant message was a "
                    "tool-calls tail"
                ),
            }

        if last is None:
            # startup race: prompt accepted but no assistant message yet
            return {"state": "working", "tree": sorted(tree)}

        info = last.get("info") or {}
        finish = info.get("finish")
        msg_created = msg_created_ms(last)

        # An assistant message that errored is a terminal failure — EXCEPT an
        # abort: OpenCode 1.18.21 marks the aborted message with
        # error.name == "MessageAbortedError". That is a clean abort, not a
        # failure. (Handled here too, not just via the _aborted set, because
        # the SSE event from the abort can poke the wait loop before abort()
        # adds the sid to _aborted.)
        if info.get("error"):
            err = info["error"]
            err_name = err.get("name") if isinstance(err, dict) else getattr(err, "name", None)
            if err_name == "MessageAbortedError" or root in self._aborted:
                self._aborted.discard(root)
                return {
                    "state": "aborted",
                    "session_id": root,
                    "agent": turn.get("agent"),
                    "tree": sorted(tree),
                    "note": "turn aborted",
                }
            return {
                "state": "error",
                "session_id": root,
                "error": str(info["error"])[:2000],
                "last_assistant_text": assistant_text(last)[:4000],
                "tree": sorted(tree),
            }

        # Baseline guard: when we know how many assistant messages existed
        # before the turn (fresh submission), the finishing message must be a
        # NEW one. On resume without durable state the baseline is None and
        # the user-message ordering check below is the authority instead.
        baseline = turn.get("assistant_count_before")
        if baseline is not None:
            new_count = sum(
                1 for m in messages if (m.get("info") or {}).get("role") == "assistant"
            )
            if new_count <= baseline:
                return {"state": "working", "tree": sorted(tree)}

        # Terminal only when: the last assistant message finished with a
        # terminal reason (not 'tool-calls' = not mid-tool-loop) AND it
        # belongs to the CURRENT turn. Belonging = created after the user
        # prompt that started the turn (robust across controller restarts:
        # the in-flight turn's assistant message was created after its user
        # prompt, while any older terminal message predates it).
        if finish and finish != "tool-calls":
            user_msg = last_user_message(messages)
            user_created = msg_created_ms(user_msg)
            belongs_to_turn = (
                (user_msg is not None and msg_created >= user_created - 5000)
                or msg_created >= turn["submitted_at_ms"] - 5000
            )
            if belongs_to_turn:
                diffs = await self.oc.diff(
                    root, directory, message_id=(user_msg or {}).get("info", {}).get("id")
                )
                now_ms = int(time.time() * 1000)
                dsummary = diff_summary(diffs)
                text = assistant_text(last)
                if finish == "length" and classify_length_finish(dsummary, text) == "error":
                    # Output budget exhausted with NO deliverable (empty diff,
                    # anecdotal text): the agent re-deliberated until the cap
                    # and produced nothing. Return a structured error so the
                    # supervisor can send a short corrective turn on the SAME
                    # session instead of receiving a silent "completed".
                    return {
                        "state": "error",
                        "code": "output_limit_reached",
                        "session_id": root,
                        "agent": turn.get("agent"),
                        "directory": directory,
                        "last_assistant_text": text[:4000],
                        "tree": sorted(tree),
                        "error": (
                            "agent exhausted its output budget (finish='length') "
                            "before producing a deliverable"
                        ),
                        "hint": (
                            "send a short corrective turn on the SAME session via opencode_run "
                            "(same session_id): instruct the agent to stop re-analyzing and "
                            "execute the pending action using the conclusions already "
                            "established in the previous turn. Do NOT start a new session "
                            "unless this one is unrecoverable."
                        ),
                    }
                result: dict[str, Any] = {
                    "state": "completed",
                    "session_id": root,
                    "agent": turn.get("agent"),
                    "last_assistant_text": text,
                    "diff": dsummary,
                    "tree": sorted(tree),
                    "timing": {
                        "submitted_at_ms": turn["submitted_at_ms"],
                        "completed_at_ms": now_ms,
                        "elapsed_ms": now_ms - turn["submitted_at_ms"],
                    },
                }
                if finish == "length":
                    result["warning"] = (
                        "assistant message ended with finish='length' (output token cap "
                        "reached) — the answer may be truncated"
                    )
                return result

        # busy-then-idle with a tool-calls tail: the model may still be
        # composing the next message; keep waiting (the safety poke bounds it).
        return {"state": "working", "tree": tree}

    # ------------------------------------------------------------------ #
    # The blocking wait (shared by run / answer / permission)
    # ------------------------------------------------------------------ #

    async def _wait(
        self,
        turn: dict[str, Any],
        timeout: float,
        deadline_override: float | None = None,
    ) -> dict[str, Any]:
        """Block until the turn ends. Returns a normalized result dict:
        state in {completed, error, needs_agent_input, timeout}.
        """
        sid = turn["session_id"]
        self._active = turn
        turn.setdefault("tree", {sid})
        save_turn_state(turn)
        sse_task = asyncio.create_task(self._subscribe(turn))
        if deadline_override is not None:
            deadline = deadline_override
        else:
            deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {
                        "ok": False,
                        "state": "timeout",
                        "session_id": sid,
                        "error": f"wait exceeded {timeout:.0f}s; session left running",
                        "hint": (
                            "resume the same turn with opencode_answer/"
                            "opencode_permission if it needs input, or continue "
                            "with opencode_run (same session_id); use "
                            "opencode_inspect (diagnostic) or opencode_abort"
                        ),
                    }
                ev = self._poke(sid)
                ev.clear()
                try:
                    poke_timeout = min(RECONCILE_POKE_TIMEOUT, remaining)
                    await asyncio.wait_for(ev.wait(), timeout=poke_timeout)
                except asyncio.TimeoutError:
                    pass  # safety poke → reconcile anyway
                # FAST-PATH abort: if we were asked to abort this session,
                # return immediately WITHOUT any REST reconciliation. This is
                # checked here (not only in _reconcile) because _reconcile's
                # first calls (_refresh_tree / status_map / messages) can be
                # slow or blocked, and we must not let a pending abort wait for
                # them. The abort POST has already been fired (see abort()).
                if sid in self._aborted:
                    self._aborted.discard(sid)
                    # The turn is terminating (aborted) — clear durable state so
                    # a later opencode_run on this session does not wrongly
                    # treat it as an in-flight turn and try to resume it.
                    clear_turn_state(sid)
                    return self._finalize({
                        "state": "aborted",
                        "session_id": sid,
                        "agent": turn.get("agent"),
                        "tree": sorted(turn.get("tree") or {sid}),
                        "note": "abort requested via opencode_abort",
                    }, turn)
                result = await self._reconcile(turn)
                state = result["state"]
                if state in ("completed", "error", "aborted", "needs_agent_input"):
                    if state in ("completed", "error", "aborted"):
                        clear_turn_state(sid)
                    return self._finalize(result, turn)
                # working → loop
        finally:
            self._active = None
            sse_task.cancel()
            try:
                await sse_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _finalize(self, result: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
        """Normalize terminal / needs-input results for the MCP layer."""
        state = result["state"]
        if state == "completed":
            result["ok"] = True
            result["directory"] = turn["directory"]
            return result
        if state == "error":
            result["ok"] = False
            return result
        if state == "aborted":
            result["ok"] = True
            result["directory"] = turn["directory"]
            return result
        # needs_agent_input: the MCP tool returns this to the Hermes LLM,
        # which answers via opencode_answer / opencode_permission (same turn).
        result["ok"] = True
        result["directory"] = turn["directory"]
        if result.get("kind") == "question":
            q = result["question"]
            result["question_id"] = q.get("id")
            subqs = q.get("questions") or []
            result["questions"] = [
                {
                    "header": sq.get("header"),
                    "question": sq.get("question"),
                    "options": [
                        {
                            "label": o.get("label"),
                            "description": o.get("description"),
                        }
                        for o in (sq.get("options") or [])
                    ],
                    "multiple": bool(sq.get("multiple")),
                    "custom": bool(sq.get("custom")),
                }
                for sq in subqs
            ]
            # exact labels the server will accept (validation happens here)
            result["valid_labels"] = [
                [o.get("label") for o in (sq.get("options") or []) if o.get("label")]
                for sq in subqs
            ]
        else:
            p = result["permission"]
            result["permission_id"] = p.get("id")
            result["allowed_replies"] = ["once", "always", "reject"]
            result["permission_summary"] = {
                "permission": p.get("permission"),
                "patterns": p.get("patterns"),
                "metadata": p.get("metadata"),
            }
        return result

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #

    async def run(
        self,
        directory: str,
        task: str,
        agent: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        timeout: float = DEFAULT_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        if not os.path.isdir(directory):
            return {"ok": False, "error": f"directory does not exist: {directory}"}

        try:
            model_obj = parse_model(model) if model else None
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # -- agent validation (dynamic, against the live /agent list) ------ #
        if agent:
            try:
                agents = await self.oc.agents(directory)
            except OpenCodeError as exc:
                return {"ok": False, "error": f"cannot list agents: {exc}"}
            _, err = validate_agent(agents, agent)
            if err:
                return err

        # create or reuse session
        if session_id:
            sid = session_id
            try:
                await self.oc.session(sid)
            except OpenCodeError as exc:
                return {
                    "ok": False,
                    "error": f"session {sid} not found: {exc}",
                }
            # RESUME semantics. Authoritative signal that a turn is in flight
            # is the durable turn state (written at submission, cleared on
            # terminal). status_map is NOT reliable here: while another
            # controller process holds the SSE stream it returns {} (1.18.21
            # quirk), so we must not use "not busy" alone to decide to
            # resubmit — that would wrongly send a NEW prompt onto a turn that
            # is still running (forbidden).
            state = load_turn_state(sid)
            in_flight = state is not None
            if not in_flight:
                # No durable state: check status_map (works when no other
                # controller is holding the stream) as a secondary signal.
                statuses = await self.oc.status_map(directory)
                in_flight = (statuses.get(sid) or {}).get("type") in ("busy", "retry")
            if in_flight:
                # A turn is in flight (or was, and may have just finished
                # while we were disconnected). Re-enter the wait loop on the
                # SAME turn — NEVER resubmit the prompt. `task` is ignored.
                # If the turn already finished, _wait's reconcile returns the
                # terminal state promptly (or, if it finished with no terminal
                # message, the post-interaction stall logic / baseline guard
                # resolves it).
                turn = state or new_turn(sid, directory, agent=agent)
                if turn.get("assistant_count_before") is None:
                    pre = await self.oc.messages(sid, directory)
                    turn["assistant_count_before"] = sum(
                        1 for m in pre if (m.get("info") or {}).get("role") == "assistant"
                    )
                journal.append_start(sid, directory, turn.get("agent"), task)
                result = await self._wait(turn, timeout)
                if result.get("state") in ("completed", "error", "aborted", "timeout"):
                    diff = result.get("diff") or {}
                    journal.append_end(
                        sid, directory, result["state"],
                        (result.get("timing") or {}).get("elapsed_ms"),
                        diff.get("files"), diff.get("additions"), diff.get("deletions"),
                        diff.get("changed_files"),
                    )
                return result
        else:
            try:
                sess = await self.oc.create_session(
                    directory, title=task[:80], agent=agent, model=model_obj
                )
            except OpenCodeError as exc:
                return {"ok": False, "error": f"failed to create session: {exc}"}
            sid = (sess or {}).get("id")
            if not sid:
                return {"ok": False, "error": "failed to create session", "raw": sess}

        # NEW prompt path (new session, or a fresh turn on an existing session).
        # `agent` is REQUIRED here — we must not silently fall back to
        # default_agent. (On the resume path above we already returned, so an
        # in-flight turn never reaches this point.)
        if not agent:
            return {
                "ok": False,
                "error": (
                    "agent is required for a new turn. Pass the primary agent "
                    "explicitly (e.g. 'build', 'plan', or a project agent). "
                    "This is not a resume of an in-flight turn."
                ),
                "session_id": sid,
            }

        turn = new_turn(sid, directory, agent=agent)
        # Baseline for the completion heuristic: the finishing assistant
        # message must be NEWER than the messages that already existed.
        pre_messages = await self.oc.messages(sid, directory)
        turn["assistant_count_before"] = sum(
            1 for m in pre_messages if (m.get("info") or {}).get("role") == "assistant"
        )
        text = task.rstrip() + ("\n\n" + SUPERVISOR_GUARD if task.strip() else "")
        try:
            await self.oc.prompt_async(sid, text, agent=agent, model=model_obj, directory=directory)
        except OpenCodeError as exc:
            clear_turn_state(sid)
            return {
                "ok": False,
                "error": f"failed to submit prompt: {exc}",
                "session_id": sid,
            }
        journal.append_start(sid, directory, turn.get("agent"), task)
        result = await self._wait(turn, timeout)
        if result.get("state") in ("completed", "error", "aborted", "timeout"):
            diff = result.get("diff") or {}
            journal.append_end(
                sid, directory, result["state"],
                (result.get("timing") or {}).get("elapsed_ms"),
                diff.get("files"), diff.get("additions"), diff.get("deletions"),
                diff.get("changed_files"),
            )
        return result

    # ------------------------------------------------------------------ #
    # Resume primitives (answer / permission) — same turn, no resubmit
    # ------------------------------------------------------------------ #

    async def _mark_interaction_answered(
        self, session_id: str, request_id: str, directory: str
    ) -> None:
        """Record that we just answered a question / decided a permission.

        Captures the assistant message count at answer time (the model's
        in-flight message is already present). This count is the robust
        discriminator for post-interaction stall detection: if the model
        RESUMES it creates a new assistant message (count grows) or updates
        the finish to a terminal reason (normal heuristic catches it); if the
        turn is TERMINATED by the interaction (e.g. a permission reject) the
        count stays put and the last message stays at a non-terminal
        finish. Adds the request id to `ignore_ids` and resets the idle
        clock. Persisted so it survives a controller restart.
        """
        state = load_turn_state(session_id)
        if state is None:
            return
        state["ignore_ids"].add(request_id)
        state["answered_interaction"] = True
        state["idle_since_ms"] = None
        try:
            msgs = await self.oc.messages(session_id, directory)
            state["assistant_count_at_answer"] = sum(
                1 for m in msgs if (m.get("info") or {}).get("role") == "assistant"
            )
        except OpenCodeError:
            state["assistant_count_at_answer"] = None
        save_turn_state(state)

    async def _resume_wait(
        self,
        session_id: str,
        directory: str,
        timeout: float,
        deadline_override: float | None = None,
    ) -> dict[str, Any]:
        """Re-enter the wait loop on an already-submitted turn.

        The turn state (submission time, ignored request ids) is loaded from
        the durable state file if present (controller-restart case), else
        reconstructed. The prompt is NEVER resubmitted.
        """
        state = load_turn_state(session_id)
        if state is not None:
            turn = state
            turn["tree"] = {session_id}
        else:
            # No durable state (e.g. pre-migration or cleared): reconstruct.
            # submitted_at_ms = now is safe for completion detection only if
            # the finishing message postdates it; if the turn already finished
            # before we resumed, reconcile will report it via the last
            # assistant message (created >= now-5s is the only risk window,
            # and that window means the turn is brand new anyway).
            turn = new_turn(session_id, directory)
        return await self._wait(turn, timeout, deadline_override=deadline_override)

    async def answer(
        self,
        directory: str,
        session_id: str,
        question_id: str,
        answers: list[Any],
        timeout: float = DEFAULT_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        # 1) validate the question is still pending (REST is the source of truth)
        try:
            quests = await self.oc.questions(directory)
        except OpenCodeError as exc:
            return {"ok": False, "error": f"cannot list pending questions: {exc}"}
        quest = next((q for q in quests if q.get("id") == question_id), None)
        if quest is None:
            return {
                "ok": False,
                "error": (
                    f"question {question_id} is not pending anymore (already answered "
                    "or consumed). The turn may have moved on — use opencode_inspect "
                    "(diagnostic) or continue with opencode_run."
                ),
                "session_id": session_id,
            }
        if quest.get("sessionID") != session_id:
            return {
                "ok": False,
                "error": (
                    f"question {question_id} belongs to session "
                    f"{quest.get('sessionID')}, not {session_id}"
                ),
            }
        # 2) validate answers against the question's options
        ok, err = validate_question_answers(quest, answers)
        if not ok:
            return {"ok": False, "error": err, "session_id": session_id, "question_id": question_id}
        # 3) post the answer
        normalized = [list(a) if isinstance(a, list) else [a] for a in answers]
        try:
            await self.oc.reply_question(question_id, normalized, directory)
        except OpenCodeError as exc:
            return {
                "ok": False,
                "error": f"failed to post answer: {exc}",
                "session_id": session_id,
                "question_id": question_id,
            }
        # 4) mark answered + re-enter the wait loop on the SAME turn
        await self._mark_interaction_answered(session_id, question_id, directory)
        return await self._resume_wait(session_id, directory, timeout)

    async def permission(
        self,
        directory: str,
        session_id: str,
        permission_id: str,
        reply: str,
        timeout: float = DEFAULT_RUN_TIMEOUT,
    ) -> dict[str, Any]:
        ok, err = validate_permission_reply(reply)
        if not ok:
            return {
                "ok": False,
                "error": err,
                "session_id": session_id,
                "permission_id": permission_id,
            }
        # 1) validate the permission is still pending
        try:
            perms = await self.oc.permissions(directory)
        except OpenCodeError as exc:
            return {"ok": False, "error": f"cannot list pending permissions: {exc}"}
        perm = next((p for p in perms if p.get("id") == permission_id), None)
        if perm is None:
            return {
                "ok": False,
                "error": (
                    f"permission {permission_id} is not pending anymore (already answered "
                    "or consumed). The turn may have moved on — use opencode_inspect "
                    "(diagnostic) or continue with opencode_run."
                ),
                "session_id": session_id,
            }
        if perm.get("sessionID") != session_id:
            return {
                "ok": False,
                "error": (
                    f"permission {permission_id} belongs to session "
                    f"{perm.get('sessionID')}, not {session_id}"
                ),
            }
        # 2) post the decision
        try:
            await self.oc.reply_permission(permission_id, reply, directory)
        except OpenCodeError as exc:
            return {
                "ok": False,
                "error": f"failed to post permission reply: {exc}",
                "session_id": session_id,
                "permission_id": permission_id,
            }
        # 3) mark answered + re-enter the wait loop on the SAME turn
        await self._mark_interaction_answered(session_id, permission_id, directory)
        return await self._resume_wait(session_id, directory, timeout)

    # ------------------------------------------------------------------ #
    # abort / inspect / sessions
    # ------------------------------------------------------------------ #

    async def abort(self, session_id: str | None) -> dict[str, Any]:
        sid = session_id or (self._active or {}).get("session_id")
        if not sid:
            return {"ok": False, "error": "no session_id given and no active run"}
        # Signal the active wait IMMEDIATELY (before the slow POST). The
        # abort POST makes the server drain the in-flight generation and can
        # take tens of seconds; if we awaited it first, the run's wait loop
        # would time out before we ever set the flag. Registering + poking
        # first lets the running opencode_run return state=aborted at once,
        # while the POST proceeds in the background to actually stop the turn.
        self._aborted.add(sid)
        self._poke_set(sid)
        ok = await self.oc.abort(sid)
        return {"ok": ok, "session_id": sid, "aborted": ok}

    async def inspect(self, session_id: str | None) -> dict[str, Any]:
        sid = session_id or (self._active or {}).get("session_id")
        if not sid:
            return {"ok": False, "error": "no session_id given and no active run"}
        try:
            sess = await self.oc.session(sid)
        except OpenCodeError as exc:
            return {"ok": False, "error": f"session not found: {exc}"}
        directory = sess.get("directory") or (self._active or {}).get("directory")
        tree = {sid}
        await self._refresh_tree(sid, tree)
        statuses = await self.oc.status_map(directory)
        perms = [p for p in await self.oc.permissions(directory) if p.get("sessionID") in tree]
        quests = [q for q in await self.oc.questions(directory) if q.get("sessionID") in tree]
        messages = await self.oc.messages(sid, directory)
        last = last_assistant(messages)
        return {
            "ok": True,
            "session_id": sid,
            "sse_connected": self._sse_connected,
            "sse_reconnects": self._sse_reconnects,
            "root_status": statuses.get(sid, {"type": "idle"}),
            "tree": sorted(tree),
            "pending_permissions": perms,
            "pending_questions": quests,
            "message_count": len(messages),
            "last_assistant_finish": (last.get("info") or {}).get("finish") if last else None,
            "last_assistant_text": assistant_text(last)[:4000],
        }

    async def sessions(self, directory: str) -> dict[str, Any]:
        items = await self.oc.list_sessions(directory)
        out = [
            {
                "id": s.get("id"),
                "title": s.get("title"),
                "directory": s.get("directory"),
                "time": s.get("time") or {},
            }
            for s in items
        ]
        return {"ok": True, "directory": directory, "count": len(out), "sessions": out[:200]}
