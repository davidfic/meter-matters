"""Google, for the two things a user may choose to use it for.

**Signing in** asks Google who someone is - name and email, nothing else. The
app gets no ongoing access to anything and stores no token.

**Connecting Gmail** asks to read the mailbox, so bills can be found without
the user forwarding or uploading anything. Google offers no narrower grant
than "read all mail", which is exactly why this is the user's choice and not
the default - and why they can ask for a one-time import that keeps nothing.

Set ELEC_GOOGLE_CLIENT_ID and ELEC_GOOGLE_CLIENT_SECRET to switch these on, and
ELEC_BASE_URL to the address the app is served from. The redirect URI to
register with Google is  <ELEC_BASE_URL>/auth/google/callback
"""

from __future__ import annotations

import base64
import email
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from email import policy

import billmail

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me"

SCOPES_SIGN_IN = "openid email profile"
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
SCOPES_GMAIL = f"openid email {SCOPE_GMAIL}"

TIMEOUT = 20
# A bill a month for each fuel: a few hundred covers well over a decade.
MAX_MESSAGES = 500


class GoogleError(RuntimeError):
    """Google refused, or answered with something we can't use."""


class Revoked(GoogleError):
    """The user withdrew access, or the grant expired. Not an outage."""


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
        if code == "invalid_grant" or exc.code == 401:
            raise Revoked("access was withdrawn or has expired") from None
        raise GoogleError(f"Google answered {exc.code} {code}".strip()) from None
    except (urllib.error.URLError, OSError) as exc:
        raise GoogleError(f"couldn't reach Google: {exc}") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise GoogleError("Google sent something that wasn't JSON") from None


def authorize_url(state: str, *, gmail: bool, login_hint: str = "") -> str:
    params = {
        "client_id": os.environ["ELEC_GOOGLE_CLIENT_ID"],
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": SCOPES_GMAIL if gmail else SCOPES_SIGN_IN,
        "state": state,
    }
    if gmail:
        # offline + consent is what makes Google issue a refresh token. We ask
        # even for a one-time import, then simply don't keep it.
        params.update(access_type="offline", prompt="consent",
                      include_granted_scopes="false")
    else:
        params["prompt"] = "select_account"
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


def refresh(refresh_token: str) -> str:
    """A fresh access token. Raises Revoked when the user has withdrawn access."""
    tokens = _call(TOKEN_URL, data={
        "refresh_token": refresh_token,
        "client_id": os.environ["ELEC_GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["ELEC_GOOGLE_CLIENT_SECRET"],
        "grant_type": "refresh_token",
    })
    if not tokens.get("access_token"):
        raise GoogleError("Google returned no access token")
    return tokens["access_token"]


def revoke(token: str) -> None:
    """Hand the grant back. Best effort - the user can also do it from Google."""
    try:
        _call(REVOKE_URL, data={"token": token})
    except GoogleError:
        pass


def identity(access_token: str) -> dict:
    """Who this is. Fetched from Google directly, over TLS, so there is no
    signature of our own to check - and an unverified address is refused."""
    who = _call(USERINFO_URL, token=access_token)
    if not who.get("sub") or not who.get("email"):
        raise GoogleError("Google didn't say who this is")
    if who.get("email_verified") not in (True, "true"):
        raise GoogleError("that Google account's email address isn't verified")
    return {"sub": str(who["sub"]), "email": who["email"], "name": who.get("name", "")}


def granted_gmail(tokens: dict) -> bool:
    """Did the user actually tick the mailbox box? Google lets them decline it."""
    return SCOPE_GMAIL in (tokens.get("scope") or "").split()


def fetch_notices(access_token: str, *, days: int | None = None,
                  sender: str = billmail.DEFAULT_SENDER) -> tuple[list, int]:
    """Bill notices from the mailbox, oldest first, and how many were unreadable.

    Only messages from the utility's sender are ever requested - the grant
    covers the whole mailbox, the queries do not.
    """
    query = f"from:{sender}" + (f" newer_than:{int(days)}d" if days else "")
    ids, page = [], None
    while len(ids) < MAX_MESSAGES:
        params = {"q": query, "maxResults": 100}
        if page:
            params["pageToken"] = page
        listing = _call(f"{GMAIL_URL}/messages?{urllib.parse.urlencode(params)}",
                        token=access_token)
        ids += [m["id"] for m in listing.get("messages", [])]
        page = listing.get("nextPageToken")
        if not page:
            break

    notices, unreadable, seen = [], 0, set()
    for message_id in ids[:MAX_MESSAGES]:
        item = _call(f"{GMAIL_URL}/messages/{message_id}?format=raw", token=access_token)
        try:
            raw = base64.urlsafe_b64decode(item.get("raw", "") + "==")
            msg = email.message_from_bytes(raw, policy=policy.default)
            notice = billmail.parse_message(msg)
        except (billmail.ParseError, ValueError):
            unreadable += 1      # a payment receipt, an outage notice, a new template
            continue
        key = notice.message_id or message_id
        if key not in seen:
            seen.add(key)
            notices.append(notice)
    notices.sort(key=lambda n: n.due)
    return notices, unreadable
