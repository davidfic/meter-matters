"""Google sign-in.

Signing in asks Google who someone is - name and email, nothing else. The app
gets no ongoing access to anything and stores no token.

Set ELEC_GOOGLE_CLIENT_ID and ELEC_GOOGLE_CLIENT_SECRET to switch it on, and
ELEC_BASE_URL to the address the app is served from. The redirect URI to
register with Google is  <ELEC_BASE_URL>/auth/google/callback
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPES_SIGN_IN = "openid email profile"

TIMEOUT = 20


class GoogleError(RuntimeError):
    """Google refused, or answered with something we can't use."""


def enabled() -> bool:
    return bool(os.environ.get("ELEC_GOOGLE_CLIENT_ID")
                and os.environ.get("ELEC_GOOGLE_CLIENT_SECRET"))


def redirect_uri() -> str:
    base = os.environ.get("ELEC_BASE_URL", "").rstrip("/")
    if not base:
        raise GoogleError("ELEC_BASE_URL is not set")
    return f"{base}/auth/google/callback"


def _call(url: str, *, data: dict | None = None, token: str | None = None) -> dict:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            problem = json.loads(exc.read() or b"{}")
        except ValueError:
            problem = {}
        code = problem.get("error")
        code = code.get("status", "") if isinstance(code, dict) else (code or "")
        raise GoogleError(f"Google answered {exc.code} {code}".strip()) from None
    except (urllib.error.URLError, OSError) as exc:
        raise GoogleError(f"couldn't reach Google: {exc}") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise GoogleError("Google sent something that wasn't JSON") from None


def authorize_url(state: str, *, login_hint: str = "") -> str:
    params = {
        "client_id": os.environ["ELEC_GOOGLE_CLIENT_ID"],
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES_SIGN_IN,
        "state": state,
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


def exchange(code: str) -> dict:
    """Trade the code from the redirect for tokens."""
    return _call(TOKEN_URL, data={
        "code": code,
        "client_id": os.environ["ELEC_GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["ELEC_GOOGLE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    })


def identity(access_token: str) -> dict:
    """Who this is. Fetched from Google directly, over TLS, so there is no
    signature of our own to check - and an unverified address is refused."""
    who = _call(USERINFO_URL, token=access_token)
    if not who.get("sub") or not who.get("email"):
        raise GoogleError("Google didn't say who this is")
    if who.get("email_verified") not in (True, "true"):
        raise GoogleError("that Google account's email address isn't verified")
    return {"sub": str(who["sub"]), "email": who["email"], "name": who.get("name", "")}
