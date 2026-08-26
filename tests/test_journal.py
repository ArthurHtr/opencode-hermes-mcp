"""Unit tests for the durable delegation journal (opencode_hermes_mcp.journal).

Pure unit tests: no LLM, no server. Run with:
  .venv/bin/python -m pytest tests/test_journal.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from opencode_hermes_mcp import journal  # noqa: E402


def test_journal_path_default(monkeypatch):
    monkeypatch.delenv(journal.ENV_JOURNAL, raising=False)
    p = journal.journal_path()
    assert p.name == "delegations.jsonl"
    assert p.parent.name == "opencode-hermes-mcp"
    assert ".local" in p.parts and "state" in p.parts


def test_journal_path_env_override(tmp_path, monkeypatch):
    custom = tmp_path / "custom" / "delegations.jsonl"
    monkeypatch.setenv(journal.ENV_JOURNAL, str(custom))
    assert journal.journal_path() == custom


def test_append_start_end_jsonl_format(tmp_path, monkeypatch):
    custom = tmp_path / "sub" / "delegations.jsonl"
    monkeypatch.setenv(journal.ENV_JOURNAL, str(custom))
    journal.append_start("ses_1", "/repo", "build", "t" * 600)
    journal.append_end(
        "ses_1", "/repo", "completed",
        elapsed_ms=1234, files=2, additions=10, deletions=1,
        changed_files=[f"f{i}.py" for i in range(60)],
    )
    lines = custom.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    start, end = (json.loads(line) for line in lines)
    assert start["kind"] == "start"
    assert start["session_id"] == "ses_1"
    assert start["directory"] == "/repo"
    assert start["agent"] == "build"
    assert start["task"] == "t" * 500
    assert isinstance(start["ts"], int) and start["ts"] > 0
    assert end["kind"] == "end"
    assert end["session_id"] == "ses_1"
    assert end["state"] == "completed"
    assert end["elapsed_ms"] == 1234
    assert end["files"] == 2
    assert end["additions"] == 10
    assert end["deletions"] == 1
    assert len(end["changed_files"]) == 50
    assert end["ts"] >= start["ts"]


def test_read_journal(tmp_path, monkeypatch):
    custom = tmp_path / "d.jsonl"
    monkeypatch.setenv(journal.ENV_JOURNAL, str(custom))
    assert journal.read_journal() == []
    journal.append_start("ses_2", "/r", "plan", "hello")
    journal.append_end("ses_2", "/r", "error")
    recs = journal.read_journal()
    assert [r["kind"] for r in recs] == ["start", "end"]
    assert recs[1]["state"] == "error"
    assert journal.read_journal(custom) == recs


def test_read_journal_skips_bad_lines(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text('{"kind": "start", "session_id": "s"}\nnot-json\n\n', encoding="utf-8")
    recs = journal.read_journal(p)
    assert len(recs) == 1
    assert recs[0]["kind"] == "start"


def test_append_never_raises_on_unwritable_path(tmp_path, monkeypatch):
    monkeypatch.setenv(journal.ENV_JOURNAL, "/proc/no-such-dir-ocmcp/delegations.jsonl")
    journal.append_start("ses_x", "/r", "build", "task")
    journal.append_end("ses_x", "/r", "completed", elapsed_ms=1)
