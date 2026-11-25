"""Password hashing helpers for local dashboard accounts."""

import base64
import hashlib
import hmac
import secrets


_SCHEME = "scrypt"
_N = 1 << 14
_R = 8
_P = 1
_KEY_LENGTH = 32


def hash_passphrase(passphrase):
    """Hash a dashboard passphrase with a fresh salt."""
    if not isinstance(passphrase, str) or not passphrase:
        raise ValueError("passphrase must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
        dklen=_KEY_LENGTH,
    )
    return "$".join((
        _SCHEME, str(_N), str(_R), str(_P),
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    ))


def verify_passphrase(passphrase, encoded):
    """Verify one passphrase; malformed hashes fail closed."""
    if not isinstance(passphrase, str) or not isinstance(encoded, str):
        return False
    try:
        scheme, n_text, r_text, p_text, salt_text, digest_text = encoded.split("$", 5)
        if scheme != _SCHEME:
            return False
        n, r, p = int(n_text), int(r_text), int(p_text)
        if n < 2 or n & (n - 1) or r < 1 or p < 1:
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.scrypt(
            passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p,
            dklen=len(expected),
        )
    except (ValueError, TypeError, UnicodeError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)
