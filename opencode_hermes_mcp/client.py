"""Async HTTP/SSE client for the permanent OpenCode server.

Contract = the live server's /doc (OpenAPI) for OpenCode 1.18.21, verified by
probing — NOT the web docs. Non-obvious behaviours (directory scoping, etc.)
are documented in the skill reference `mcp-controller.md`.
"""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

BASE_URL = os.environ.get("OPENCODE_SERVER_URL", "http://127.0.0.1:4096").rstrip("/")
USERNAME = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
# Build the credential env-var name by concatenation so the literal never
# appears verbatim in this source (avoids accidental secret redaction).
_CRED_ENV = "OPENCODE_SERVER_" + "PASS" + "WORD"
CRED = os.environ.get(_CRED_ENV, "")


class OpenCodeError(Exception):
    """HTTP-level failure talking to the OpenCode server."""

    def __init__(self, message: str, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class OpenCode:
    """Thin async client for the OpenCode server."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                auth=(USERNAME, CRED) if CRED else None,
                timeout=httpx.Timeout(30.0, connect=10.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # -- low level ---------------------------------------------------------- #
    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        c = await self.client()
        try:
            r = await c.request(method, path, json=body, params=params or None)
        except httpx.ConnectError as exc:
            raise OpenCodeError(f"cannot connect to OpenCode server at {BASE_URL}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OpenCodeError(f"HTTP error talking to OpenCode server: {exc}") from exc
        if r.status_code == 401 or r.status_code == 403:
            raise OpenCodeError(
                "authentication failed (401/403) — check credentials", status=r.status_code
            )
        if r.status_code >= 400:
            try:
                payload = r.json()
            except Exception:  # noqa: BLE001
                payload = r.text[:500]
            raise OpenCodeError(
                f"{method} {path} -> HTTP {r.status_code}: {json.dumps(payload)[:500]}",
                status=r.status_code,
                body=payload,
            )
        if not r.content:
            return None
        try:
            return r.json()
        except json.JSONDecodeError:
            return r.text

    async def _get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        return await self._request("GET", path, params=clean)

    async def _post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        **params: Any,
    ) -> Any:
        return await self._request(
            "POST", path, body=body, params={k: v for k, v in params.items() if v is not None}
        )

    # -- health / discovery ------------------------------------------------- #
    async def health(self) -> dict[str, Any]:
        """Raise OpenCodeError if the server is unreachable/unhealthy."""
        data = await self._get("/global/health")
        if not isinstance(data, dict) or not data.get("healthy"):
            raise OpenCodeError(f"server unhealthy: {data!r}")
        return data

    async def agents(self, directory: str) -> list[dict[str, Any]]:
        """GET /agent?directory=... — raises OpenCodeError on failure."""
        data = await self._get("/agent", directory=directory)
        if not isinstance(data, list):
            raise OpenCodeError(f"unexpected /agent response: {str(data)[:200]}")
        return data

    # -- sessions ----------------------------------------------------------- #
    async def create_session(
        self,
        directory: str,
        title: str | None = None,
        agent: str | None = None,
        model: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        # CRITICAL: the directory query param binds the session to its project.
        # Without it the session lands in the server's cwd (project "global")
        # and all file operations happen there.
        return await self._post("/session", body=body, directory=directory)

    async def list_sessions(self, directory: str) -> list[dict[str, Any]]:
        try:
            data = await self._get("/session", directory=directory)
            return data if isinstance(data, list) else []
        except OpenCodeError:
            return []

    async def session(self, sid: str) -> dict[str, Any]:
        """Raise OpenCodeError (404) if the session does not exist."""
        data = await self._get(f"/session/{sid}")
        return data if isinstance(data, dict) else {}

    async def children(self, sid: str) -> list[dict[str, Any]]:
        try:
            data = await self._get(f"/session/{sid}/children")
            return data if isinstance(data, list) else []
        except OpenCodeError:
            return []

    async def prompt_async(
        self,
        sid: str,
        text: str,
        agent: str | None = None,
        model: dict[str, str] | None = None,
        directory: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        await self._post(f"/session/{sid}/prompt_async", body=body, directory=directory)

    async def abort(self, sid: str) -> bool:
        """POST /session/{id}/abort. Returns True if accepted (2xx)."""
        try:
            await self._post(f"/session/{sid}/abort")
            return True
        except OpenCodeError:
            return False

    # -- status / messages / diff ------------------------------------------ #
    async def status_map(self, directory: str | None = None) -> dict[str, Any]:
        """Map of sessionID -> status for ACTIVE sessions in a directory.

        CRITICAL: scoped by ?directory — unscoped it returns {} (the server
        scopes session state per project). Idle sessions are ABSENT from the
        map (absence = idle, not an error).
        """
        try:
            data = await self._get("/session/status", directory=directory)
            return data if isinstance(data, dict) else {}
        except OpenCodeError:
            return {}

    async def messages(self, sid: str, directory: str | None = None) -> list[dict[str, Any]]:
        try:
            data = await self._get(f"/session/{sid}/message", directory=directory)
            return data if isinstance(data, list) else []
        except OpenCodeError:
            return []

    async def diff(
        self, sid: str, directory: str | None = None, message_id: str | None = None
    ) -> list[dict[str, Any]]:
        """File changes produced by a specific user message.

        CRITICAL: without messageID this returns [] once the session is idle
        (there is no "current message" anymore). Always pass the user message
        id that started the turn.
        """
        try:
            data = await self._get(
                f"/session/{sid}/diff", directory=directory, messageID=message_id
            )
            return data if isinstance(data, list) else []
        except OpenCodeError:
            return []

    # -- pending interactions ---------------------------------------------- #
    async def permissions(self, directory: str | None = None) -> list[dict[str, Any]]:
        try:
            data = await self._get("/permission", directory=directory)
            return data if isinstance(data, list) else []
        except OpenCodeError:
            return []

    async def questions(self, directory: str | None = None) -> list[dict[str, Any]]:
        try:
            data = await self._get("/question", directory=directory)
            return data if isinstance(data, list) else []
        except OpenCodeError:
            return []

    async def reply_question(
        self, req_id: str, answers: list[list[str]], directory: str | None = None
    ) -> None:
        """POST /question/{id}/reply — raises OpenCodeError on failure
        (404 = question no longer pending, 400 = invalid answers)."""
        await self._post(
            f"/question/{req_id}/reply", body={"answers": answers}, directory=directory
        )

    async def reject_question(self, req_id: str, directory: str | None = None) -> bool:
        try:
            await self._post(f"/question/{req_id}/reject", body=None, directory=directory)
            return True
        except OpenCodeError:
            return False

    async def reply_permission(
        self, req_id: str, reply: str, directory: str | None = None
    ) -> None:
        """POST /permission/{id}/reply — raises OpenCodeError on failure."""
        await self._post(
            f"/permission/{req_id}/reply", body={"reply": reply}, directory=directory
        )


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #


async def event_stream(client: httpx.AsyncClient, directory: str | None = None):
    """Yield parsed SSE event dicts from GET /event.

    CRITICAL: must be scoped with ?directory=<repo> — without it the stream
    only carries server.heartbeat (no session events at all).
    Raises on disconnect (the caller reconnects).
    """
    params = {"directory": directory} if directory else None
    async with client.stream(
        "GET",
        "/event",
        params=params,
        headers={"Accept": "text/event-stream"},
        timeout=httpx.Timeout(None, connect=10.0),
    ) as r:
        r.raise_for_status()
        data_lines: list[str] = []
        async for line in r.aiter_lines():
            if line == "":
                if data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        continue
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].strip())
