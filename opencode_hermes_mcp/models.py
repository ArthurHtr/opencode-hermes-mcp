"""Data helpers: message parsing, agent validation, durable turn state.

The durable state file (one per session, under OPENCODE_MCP_STATE_DIR) is what
makes `opencode_answer` / `opencode_permission` survive a controller restart:
everything the wait loop needs to resume the SAME OpenCode turn is either
server-side (status/messages/pending q/p) or persisted here (submission time,
answered request ids).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

STATE_DIR = os.environ.get(
    "OPENCODE_MCP_STATE_DIR",
    os.path.join(os.path.expanduser("~"), ".local", "state", "opencode-hermes-mcp"),
)

# A root agent must have mode "primary" (observed on 1.18.18). "all" is
# treated as usable-as-root too, in case a future version exposes it.
ROOT_AGENT_MODES = {"primary", "all"}


# --------------------------------------------------------------------------- #
# Message helpers
# --------------------------------------------------------------------------- #


def assistant_text(message: dict[str, Any] | None) -> str:
    if not message:
        return ""
    parts = message.get("parts") or []
    texts = [
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
    ]
    return "\n".join(x for x in texts if x)


def last_assistant(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for msg in reversed(messages):
        if (msg.get("info") or {}).get("role") == "assistant":
            return msg
    return None


def last_user_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for msg in reversed(messages):
        if (msg.get("info") or {}).get("role") == "user":
            return msg
    return None


def msg_created_ms(message: dict[str, Any] | None) -> int:
    info = (message or {}).get("info") or {}
    return int((info.get("time") or {}).get("created", 0) or 0)


def parse_model(value: str) -> dict[str, str]:
    if "/" not in value:
        raise ValueError(f"model must be provider/model, got {value!r}")
    provider, model = value.split("/", 1)
    if not provider or not model:
        raise ValueError(f"model must be provider/model, got {value!r}")
    return {"providerID": provider, "modelID": model}


def diff_summary(diffs: list[dict[str, Any]]) -> dict[str, Any]:
    files = [d.get("file") for d in diffs if d.get("file")]
    return {
        "files": len(files),
        "additions": sum(int(d.get("additions", 0) or 0) for d in diffs),
        "deletions": sum(int(d.get("deletions", 0) or 0) for d in diffs),
        "changed_files": files[:50],
    }


# --------------------------------------------------------------------------- #
# Agent validation (dynamic — no hardcoded whitelist)
# --------------------------------------------------------------------------- #


def validate_agent(
    agents: list[dict[str, Any]], agent: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Check `agent` against the live /agent list.

    Returns (agent_info, None) when usable as a root agent, or
    (None, error_result) otherwise. Error results carry the code
    agent_not_found / invalid_primary_agent plus the available primaries.
    """
    primaries = [a for a in agents if a.get("mode") in ROOT_AGENT_MODES]
    primaries = [a for a in primaries if not a.get("hidden")]
    primary_names = [a.get("name") for a in primaries if a.get("name")]
    for a in agents:
        if a.get("name") == agent:
            if a.get("mode") in ROOT_AGENT_MODES:
                return a, None
            return None, {
                "ok": False,
                "state": "error",
                "code": "invalid_primary_agent",
                "agent": agent,
                "mode": a.get("mode"),
                "error": f"agent '{agent}' is a {a.get('mode')} and cannot be a root agent",
                "primary_agents": primary_names,
            }
    return None, {
        "ok": False,
        "state": "error",
        "code": "agent_not_found",
        "agent": agent,
        "error": f"agent '{agent}' does not exist in this directory",
        "primary_agents": primary_names,
    }


# --------------------------------------------------------------------------- #
# Answer / permission validation (against the ORIGINAL request data)
# --------------------------------------------------------------------------- #


def validate_question_answers(
    question: dict[str, Any], answers: list[Any]
) -> tuple[bool, str]:
    """Validate `answers` (one entry per sub-question, each a str or list[str])
    against the question's options.

    OpenCode 1.18.18 rejects (400) any answer that is not an exact option
    label when the sub-question has options and custom=false.
    """
    subqs = question.get("questions") or []
    if len(answers) != len(subqs):
        return False, f"expected {len(subqs)} answer(s) (one per sub-question), got {len(answers)}"
    for i, (q, ans) in enumerate(zip(subqs, answers)):
        if isinstance(ans, str):
            ans = [ans]
        valid = isinstance(ans, list) and ans and all(
            isinstance(x, str) and x.strip() for x in ans
        )
        if not valid:
            return False, f"answer {i + 1} must be a non-empty string or list of strings"
        labels = [o.get("label") for o in (q.get("options") or []) if o.get("label")]
        custom = bool(q.get("custom"))
        if labels and not custom:
            for x in ans:
                if x not in labels:
                    return False, (
                        f"answer {i + 1}: {x!r} is not a valid option label; "
                        f"valid labels: {labels}"
                    )
        if not q.get("multiple") and len(ans) > 1:
            return False, f"answer {i + 1}: question is single-choice, got {len(ans)} answers"
    return True, ""


PERMISSION_REPLIES = ("once", "always", "reject")


def validate_permission_reply(reply: str) -> tuple[bool, str]:
    if reply in PERMISSION_REPLIES:
        return True, ""
    return False, f"reply must be one of {list(PERMISSION_REPLIES)}, got {reply!r}"


# --------------------------------------------------------------------------- #
# Durable turn state (survives controller restarts)
# --------------------------------------------------------------------------- #


def state_path(session_id: str) -> str:
    safe = session_id.replace("/", "_")
    return os.path.join(STATE_DIR, f"turn_{safe}.json")


def _turn_to_jsonable(turn: dict[str, Any]) -> dict[str, Any]:
    """Project a turn dict to a JSON-safe form (sets -> sorted lists)."""
    out = dict(turn)
    for key in ("ignore_ids", "tree"):
        if isinstance(out.get(key), (set, frozenset)):
            out[key] = sorted(out[key])
    return out


def save_turn_state(turn: dict[str, Any]) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = state_path(turn["session_id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_turn_to_jsonable(turn), f)
        os.replace(tmp, path)
    except OSError:
        pass  # state persistence is best-effort; the wait loop still works in-RAM


def load_turn_state(session_id: str) -> dict[str, Any] | None:
    try:
        with open(state_path(session_id), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data.get("session_id"):
            return None
        data["ignore_ids"] = set(data.get("ignore_ids") or [])
        if isinstance(data.get("tree"), list):
            data["tree"] = set(data["tree"])
        return data
    except (OSError, json.JSONDecodeError):
        return None


def clear_turn_state(session_id: str) -> None:
    try:
        os.remove(state_path(session_id))
    except OSError:
        pass


def new_turn(
    session_id: str,
    directory: str,
    submitted_at_ms: int | None = None,
    agent: str | None = None,
    ignore_ids: set[str] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "directory": directory,
        "agent": agent,
        "submitted_at_ms": submitted_at_ms or int(time.time() * 1000),
        "ignore_ids": set(ignore_ids or set()),
    }
