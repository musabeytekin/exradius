"""Secret encryption at rest, password hashing and run tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections.abc import Iterable

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class SecretBox:
    """Encrypts secret values with a Fernet key from the Exradius environment.

    ``key`` (``EXRADIUS_SECRET_KEY``) is the primary key: it encrypts, and it is tried first
    to decrypt. ``previous`` (``EXRADIUS_SECRET_KEY_PREVIOUS``) are keys being rotated out:
    they only decrypt, until ``exradius rotate-key`` has re-encrypted every stored value with
    the primary (docs/kubernetes.md §10.1).
    """

    def __init__(self, key: str, previous: Iterable[str] = ()):
        if not key:
            raise RuntimeError("EXRADIUS_SECRET_KEY is not set; it is required to encrypt secrets")
        fernets = [_fernet(key, "EXRADIUS_SECRET_KEY")]
        fernets += [_fernet(k, "EXRADIUS_SECRET_KEY_PREVIOUS") for k in previous]
        self._fernet = MultiFernet(fernets)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:  # pragma: no cover - depends on a wrong key
            raise RuntimeError("cannot decrypt secret: wrong EXRADIUS_SECRET_KEY?") from exc

    def rotate(self, token: str) -> str:
        """``token`` re-encrypted with the primary key, whichever known key encrypted it."""
        try:
            return self._fernet.rotate(token.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError(
                "cannot decrypt secret with EXRADIUS_SECRET_KEY or EXRADIUS_SECRET_KEY_PREVIOUS"
            ) from exc


def _fernet(key: str | bytes, setting: str) -> Fernet:
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except ValueError:  # never the value itself in the message
        raise RuntimeError(f"{setting} holds a value that is not a Fernet key") from None


def generate_secret_key() -> str:
    return Fernet.generate_key().decode()


_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = encoded.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    actual = hashlib.scrypt(
        password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p)
    )
    return hmac.compare_digest(actual, base64.b64decode(digest))


def new_run_token() -> str:
    return secrets.token_urlsafe(32)


def new_hook_token() -> str:
    """The token of a project's config hook URL (docs/integration-api.md §2)."""
    return secrets.token_urlsafe(32)


INTEGRATION_TOKEN_PREFIX = "exr_"


def new_integration_token() -> str:
    """An integration token (docs/integration-api.md §5): ``exr_`` and 32 random bytes."""
    return INTEGRATION_TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
