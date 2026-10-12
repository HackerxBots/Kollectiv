"""Remote MCP: a bearer token gates network transports; without one, remote stays refused."""

from __future__ import annotations

from typing import Any, Dict, List, cast

import httpx
import pytest

from src.api.mcp_server import BearerAuthMiddleware, remote_exposure_refused

TOKEN = "t0ken-for-tests"


async def _inner(scope: Dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        return
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"mcp ok"})


def _client() -> httpx.AsyncClient:
    app = cast(Any, BearerAuthMiddleware(_inner, TOKEN))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://mcp.test")


@pytest.mark.asyncio
async def test_a_request_without_the_token_is_refused() -> None:
    async with _client() as client:
        response = await client.get("/sse")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in response.text


@pytest.mark.asyncio
async def test_a_wrong_token_is_refused() -> None:
    async with _client() as client:
        response = await client.get("/sse", headers={"authorization": "Bearer guess"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_the_right_token_gets_through() -> None:
    async with _client() as client:
        response = await client.get("/sse", headers={"authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.text == "mcp ok"


@pytest.mark.asyncio
async def test_token_without_the_bearer_scheme_is_refused() -> None:
    async with _client() as client:
        response = await client.get("/sse", headers={"authorization": TOKEN})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_lifespan_passes_through_so_the_app_still_starts() -> None:
    seen: List[str] = []

    async def recording(scope: Dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope["type"])
        await _inner(scope, receive, send)

    app = BearerAuthMiddleware(recording, TOKEN)
    messages: List[Dict[str, Any]] = []
    queue: List[Dict[str, Any]] = [{"type": "lifespan.startup"}]

    async def receive() -> Dict[str, Any]:
        return queue.pop(0)

    async def send(message: Dict[str, Any]) -> None:
        messages.append(message)

    await app({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]
    assert messages == [{"type": "lifespan.startup.complete"}]


def test_a_token_lets_a_remote_bind_start() -> None:
    assert remote_exposure_refused("sse", "0.0.0.0", False, has_token=True) is False
    assert remote_exposure_refused("streamable-http", "10.0.0.5", False, has_token=True) is False


def test_no_token_and_no_opt_in_still_refuses_a_remote_bind() -> None:
    assert remote_exposure_refused("sse", "0.0.0.0", False) is True
    assert remote_exposure_refused("sse", "0.0.0.0", True) is False
    assert remote_exposure_refused("stdio", "0.0.0.0", False, has_token=False) is False
