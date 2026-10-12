"""The optional support link: https only, no credentials, never loaded by Kollektiv."""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from src.api.auth import is_public_path
from src.api.routes import create_app
from tests.test_api import StubOrchestrator


def test_support_is_a_public_path() -> None:
    assert is_public_path("/support")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("", None),
        ("https://example.org/donate", "https://example.org/donate"),
        ("http://example.org/donate", None),
        ("https://u@example.org/donate", None),  # credentials in the link are refused
        ("javascript:alert(1)", None),
        ("https://example.org/" + "a" * 300, None),  # over the 300-character limit
    ],
)
async def test_support_url_is_returned_only_when_safe(settings: Any, configured: str, expected: Any) -> None:
    configured_settings = settings.model_copy(update={"SUPPORT_URL": configured}, deep=True)
    app = create_app(settings=configured_settings, orchestrator=cast(Any, StubOrchestrator(configured_settings)))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        response = await c.get("/support")
    assert response.status_code == 200
    assert response.json() == {"url": expected}
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
