"""Tests for client/auth.py: the CSRF-token login flow."""

from __future__ import annotations

import httpx
import pytest
import respx

from client import shared
from client.auth import authenticate
from client.exceptions import CannotConnect, InvalidAuth

LOGIN_PAGE = """
<html><body>
<form id="login-form" method="post" action="/app/capricorn?para=index&platform=&deviceOS=">
    <input type="hidden" name="jspCSRFToken" value="test-csrf-token" />
    <input type="text" name="accessCode" />
    <input type="password" name="password" />
    <input type="hidden" name="nextPara" value="" />
    <input type="hidden" name="nextPara_attr1" value="" />
</form>
</body></html>
"""

LOGIN_PAGE_NO_FORM = "<html><body>no form here</body></html>"

LOGIN_PAGE_NO_CSRF = """
<html><body>
<form id="login-form">
    <input type="text" name="accessCode" />
    <input type="password" name="password" />
</form>
</body></html>
"""

DASHBOARD_AFTER_LOGIN = "<html><body><div>Welcome</div></body></html>"


@respx.mock
async def test_authenticate_success(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/login.jsp").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    post_route = respx.post(f"{shared.BASE_URL}/app/capricorn?para=index&platform=&deviceOS=").mock(
        return_value=httpx.Response(200, text=DASHBOARD_AFTER_LOGIN)
    )

    async with httpx.AsyncClient() as client:
        await authenticate(client, "user@example.com", "hunter2")

    sent = post_route.calls.last.request
    body = sent.content.decode()
    assert "jspCSRFToken=test-csrf-token" in body
    assert "hunter2" in body  # sent to the portal, never logged


@respx.mock
async def test_authenticate_raises_invalid_auth_when_login_form_persists(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/login.jsp").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{shared.BASE_URL}/app/capricorn?para=index&platform=&deviceOS=").mock(
        return_value=httpx.Response(200, text=LOGIN_PAGE)
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(InvalidAuth):
            await authenticate(client, "user@example.com", "wrong")


@respx.mock
async def test_authenticate_raises_cannot_connect_when_login_form_missing(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/login.jsp").mock(return_value=httpx.Response(200, text=LOGIN_PAGE_NO_FORM))

    async with httpx.AsyncClient() as client:
        with pytest.raises(CannotConnect, match="login-form"):
            await authenticate(client, "u", "p")


@respx.mock
async def test_authenticate_raises_cannot_connect_when_csrf_token_missing(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/login.jsp").mock(return_value=httpx.Response(200, text=LOGIN_PAGE_NO_CSRF))

    async with httpx.AsyncClient() as client:
        with pytest.raises(CannotConnect, match="jspCSRFToken"):
            await authenticate(client, "u", "p")


@respx.mock
async def test_authenticate_raises_cannot_connect_on_http_error_fetching_login_page(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/login.jsp").mock(return_value=httpx.Response(500))

    async with httpx.AsyncClient() as client:
        with pytest.raises(CannotConnect):
            await authenticate(client, "u", "p")


@respx.mock
async def test_authenticate_raises_cannot_connect_on_http_error_posting_login(monkeypatch):
    monkeypatch.setattr(shared, "REQUEST_DELAY_SECONDS", 0)
    respx.get(f"{shared.BASE_URL}/app/login.jsp").mock(return_value=httpx.Response(200, text=LOGIN_PAGE))
    respx.post(f"{shared.BASE_URL}/app/capricorn?para=index&platform=&deviceOS=").mock(
        return_value=httpx.Response(500)
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(CannotConnect):
            await authenticate(client, "u", "p")
