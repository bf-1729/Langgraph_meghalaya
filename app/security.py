"""
Password hashing (PBKDF2-HMAC-SHA256, stdlib) and a minimal self-contained
HS256 JWT. No third-party crypto dependency — everything here is `hashlib` /
`hmac` / `secrets` from the standard library, which keeps the service installable
offline.

Password format stored in app.users.password_hash:
    pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

from app.config import settings

_PBKDF2_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"


# ── Passwords ──────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{_ALGO}${_PBKDF2_ITERATIONS}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt_b64), int(iters))
        return hmac.compare_digest(dk, _unb64(hash_b64))
    except Exception:  # noqa: BLE001 — any malformed hash is a failed verify
        return False


def needs_rehash(stored: str) -> bool:
    try:
        _, iters, _, _ = stored.split("$")
        return int(iters) < _PBKDF2_ITERATIONS
    except Exception:  # noqa: BLE001
        return True


# ── JWT (HS256) ───────────────────────────────────────────────────────────
class JWTError(Exception):
    pass


def issue_token(claims: dict, ttl_minutes: int | None = None) -> str:
    now = int(time.time())
    payload = {
        "iss": settings.JWT_ISSUER,
        "iat": now,
        "exp": now + 60 * (ttl_minutes or settings.JWT_TTL_MINUTES),
        **claims,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    segs = [_b64url(json.dumps(header, separators=(",", ":")).encode()),
            _b64url(json.dumps(payload, separators=(",", ":"), default=str).encode())]
    signing_input = ".".join(segs).encode()
    sig = hmac.new(settings.JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    segs.append(_b64url(sig))
    return ".".join(segs)


def decode_token(token: str) -> dict:
    try:
        h_b64, p_b64, sig_b64 = token.split(".")
    except ValueError as e:
        raise JWTError("malformed token") from e
    signing_input = f"{h_b64}.{p_b64}".encode()
    expected = hmac.new(settings.JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    # Garbage in any segment must surface as JWTError (-> 401), never as an
    # unhandled binascii/JSON error (-> 500).
    try:
        signature = _unb64url(sig_b64)
    except Exception as e:  # noqa: BLE001
        raise JWTError("malformed signature") from e
    if not hmac.compare_digest(expected, signature):
        raise JWTError("bad signature")
    try:
        payload = json.loads(_unb64url(p_b64))
    except Exception as e:  # noqa: BLE001
        raise JWTError("malformed payload") from e
    if not isinstance(payload, dict):
        raise JWTError("malformed payload")
    if payload.get("iss") != settings.JWT_ISSUER:
        raise JWTError("bad issuer")
    try:
        expires = int(payload.get("exp", 0))
    except (TypeError, ValueError) as e:
        raise JWTError("malformed expiry") from e
    if expires < int(time.time()):
        raise JWTError("token expired")
    return payload


# ── base64 helpers ────────────────────────────────────────────────────────
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
