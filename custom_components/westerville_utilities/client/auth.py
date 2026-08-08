"""Login flow for the AUS Capricorn portal at billpay.westerville.org.

Ported from the `utility-reader` project. Plain CSRF-token form POST (no
JS/AJAX auth) confirmed against the real login page: GET `/app/login.jsp`
for a session cookie + `jspCSRFToken`, then POST `accessCode`/`password` to
`/app/capricorn?para=index...`.
"""

from __future__ import annotations

import logging

import httpx
from bs4 import BeautifulSoup

from .exceptions import CannotConnect, InvalidAuth
from .shared import BASE_URL, get

_LOGGER = logging.getLogger(__name__)


def _has_login_form(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return soup.find("input", {"name": "password"}) is not None


async def authenticate(client: httpx.AsyncClient, username: str, password: str) -> None:
    """Log in to the Westerville portal, populating `client`'s cookie jar.

    Never logs `username`/`password` -- the username is the account's
    "access code" and may itself be account-identifying, so it's treated as
    sensitive alongside the password.
    """
    try:
        resp = await get(client, "/app/login.jsp")
    except httpx.HTTPError as err:
        raise CannotConnect(f"Could not reach the Westerville login page: {err}") from err

    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", id="login-form")
    if form is None:
        raise CannotConnect(
            "Westerville login page did not contain the expected #login-form; "
            "the portal may be down or its layout changed"
        )
    csrf_input = form.find("input", {"name": "jspCSRFToken"})
    if csrf_input is None or not csrf_input.get("value"):
        raise CannotConnect("Westerville login form did not contain a jspCSRFToken value")
    csrf_token = csrf_input["value"]
    _LOGGER.debug("Fetched login page; csrf token acquired (%d chars)", len(csrf_token))

    payload = {
        "jspCSRFToken": csrf_token,
        "accessCode": username,
        "password": password,
        "nextPara": "",
        "nextPara_attr1": "",
    }
    _LOGGER.debug("Submitting Westerville login")
    try:
        login_resp = await client.post(
            f"{BASE_URL}/app/capricorn?para=index&platform=&deviceOS=", data=payload
        )
        login_resp.raise_for_status()
    except httpx.HTTPError as err:
        raise CannotConnect(f"Westerville login request failed: {err}") from err

    if _has_login_form(login_resp.text):
        raise InvalidAuth("Westerville rejected the supplied username/password")

    _LOGGER.debug("Westerville login succeeded")
