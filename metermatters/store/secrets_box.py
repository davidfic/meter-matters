"""Encrypting the few secrets we are obliged to keep.

Only one thing goes in here: the Gmail refresh token of a user who chose to
stay connected. The key comes from the host's secret store (ELEC_TOKEN_KEY),
never from the database - so a copy of the database alone opens nothing.

What this does not do: protect against someone who has taken over the running
server, which necessarily holds the key. That is why keeping a token is the
user's choice, and the other two ways of importing bills keep nothing at all.

    python secrets_box.py      # print a fresh key for ELEC_TOKEN_KEY
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class Unavailable(RuntimeError):
    """No key is configured, so nothing may be stored."""


def _box() -> MultiFernet:
    # Comma-separated so a key can be rotated: encrypt with the first, still
    # decrypt with the rest.
    keys = [k.strip() for k in os.environ.get("ELEC_TOKEN_KEY", "").split(",") if k.strip()]
    if not keys:
        raise Unavailable("ELEC_TOKEN_KEY is not set")
    try:
        return MultiFernet([Fernet(k) for k in keys])
    except ValueError as exc:
        raise Unavailable(f"ELEC_TOKEN_KEY is not a valid key: {exc}") from None


def available() -> bool:
    try:
        _box()
    except Unavailable:
        return False
    return True


def seal(value: str) -> bytes:
    return _box().encrypt(value.encode("utf-8"))


def open_(sealed: bytes) -> str:
    """Raises Unavailable when the key is missing or is no longer the right one."""
    try:
        return _box().decrypt(bytes(sealed)).decode("utf-8")
    except InvalidToken:
        raise Unavailable("stored secret can't be decrypted with the current key") from None


if __name__ == "__main__":
    print(Fernet.generate_key().decode())
