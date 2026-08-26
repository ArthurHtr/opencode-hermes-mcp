"""Durable delegation journal (append-only JSONL).

Records the start and terminal state of every controller run so the
delegation history survives sessions (the per-turn state file
turn_<sid>.json is cleared on completion). The journal is best-effort:
any write failure is logged and swallowed — it must never fail a run.

Path: ~/.local/state/opencode-hermes-mcp/delegations.jsonl by default,
overridable via the OPENCODE_HERMES_MCP_JOURNAL env var.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ENV_JOURNAL = "OPENCODE_HERMES_MCP_JOURNAL"
DEFAULT_JOURNAL = Path(
    os.path.join(
        os.path.expanduser("~"), ".local", "state", "opencode-hermes-mcp", "delegations.jsonl"
    )
)
TASK_MAX_CHARS = 500
CHANGED_FILES_MAX = 50


def journal_path() -> Path:
    """Journal path: env override (OPENCODE_HERMES_MCP_JOURNAL) or default."""
    return Path(os.environ.get(ENV_JOURNAL) or DEFAULT_JOURNAL)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _append(record: dict[str, Any]) -> None:
    """Append one JSON line (open 'a', one full write, flush + fsync).

    Never raises: a journal failure is logged and swallowed.
    """
    try:
        path = journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:  # noqa: BLE001 — the journal must never fail a run
        log.warning("journal: append %s failed: %s", record.get("kind"), exc)


def append_start(session_id: str, directory: str, agent: str | None, task: str) -> None:
    """Record a run start (session resolved, before the wait)."""
    _append(
        {
            "ts": _now_ms(),
            "kind": "start",
            "session_id": session_id,
            "directory": directory,
            "agent": agent,
            "task": (task or "")[:TASK_MAX_CHARS],
        }
    )


def append_end(
    session_id: str,
    directory: str,
    state: str,
    elapsed_ms: int | None = None,
    files: int | None = None,
    additions: int | None = None,
    deletions: int | None = None,
    changed_files: list[str] | None = None,
) -> None:
    """Record a run's terminal state (completed / error / aborted / timeout)."""
    _append(
        {
            "ts": _now_ms(),
            "kind": "end",
            "session_id": session_id,
            "directory": directory,
            "state": state,
            "elapsed_ms": elapsed_ms,
            "files": files,
            "additions": additions,
            "deletions": deletions,
            "changed_files": (changed_files or [])[:CHANGED_FILES_MAX],
        }
    )


def read_journal(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read the journal (read-only, for tests and consumers).

    Missing file -> []. Malformed lines are skipped.
    """
    p = Path(path) if path is not None else journal_path()
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records
