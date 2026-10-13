"""Who may talk to the service: paired devices, and nobody else.

Pairing (once per phone)
    The owner runs `python -m clip_pipeline pair` on the server and gets a one-time code (shown as a
    QR code). The app creates an ECDSA P-256 key pair in the phone's hardware keystore and sends the
    public half with the code to `POST /pair`. The private key never leaves the phone and the server
    stores nothing secret: there is no password or token to steal from it.

Every request after that carries five headers
    X-Device           the id returned by /pair
    X-Timestamp        unix seconds; must be within five minutes of the server's clock
    X-Nonce            16-64 random URL-safe characters, never reused
    X-Content-SHA256   hex SHA-256 of the request body (of the empty string when there is none)
    X-Signature        base64 ECDSA-SHA256 (DER) by the device key over these five lines joined by "\\n":
                           METHOD, path with query exactly as sent, X-Timestamp, X-Nonce, X-Content-SHA256

The signature ties a request to one device, one moment and one body, so a captured request cannot
be replayed or altered. A device is cut off by revoking it; the others keep working.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
import uuid
from collections import deque
from datetime import timedelta
from typing import Any, Callable

from clip_pipeline.config import Settings
from clip_pipeline.db import Database, iso, utcnow

MAX_CLOCK_SKEW = 300            # seconds a request's timestamp may differ from ours
PAIRING_CODE_TTL = timedelta(minutes=10)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
STREAMED_PREFIXES = ("/sources/upload/",)   # bodies too big to buffer: the route checks the hash itself
_NONCE_RE = re.compile(r"^[A-Za-z0-9_\-]{16,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class AuthError(Exception):
    """The request is not from a paired device. `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


class PairingError(ValueError):
    pass


def signing_string(method: str, path_and_query: str, timestamp: str, nonce: str, body_sha256: str) -> bytes:
    return "\n".join((method.upper(), path_and_query, timestamp, nonce, body_sha256)).encode()


def load_public_key(public_key_b64: str):
    """A device key: base64 of an X.509 SubjectPublicKeyInfo, ECDSA on P-256 (what Android's keystore makes)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    try:
        key = load_der_public_key(base64.b64decode(public_key_b64, validate=True))
    except Exception as exc:  # noqa: BLE001 - any malformed input
        raise PairingError("public_key is not a valid base64 DER public key") from exc
    if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
        raise PairingError("public_key must be an ECDSA P-256 key")
    return key


def signature_is_valid(public_key_b64: str, signature_b64: str, message: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec

    try:
        load_public_key(public_key_b64).verify(base64.b64decode(signature_b64, validate=True), message,
                                               ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, PairingError, ValueError):
        return False


class RateLimiter:
    """At most `limit` events per `window` seconds for each key (an IP address or a device id)."""

    def __init__(self, limit: int, window: float, now: Callable[[], float] = time.monotonic):
        self.limit, self.window, self._now = limit, window, now
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str) -> deque[float]:
        q = self._events.setdefault(key, deque())
        cutoff = self._now() - self.window
        while q and q[0] <= cutoff:
            q.popleft()
        return q

    def allow(self, key: str) -> bool:
        """Count one event; False if that takes `key` over the limit."""
        with self._lock:
            q = self._recent(key)
            if len(q) >= self.limit:
                return False
            q.append(self._now())
            return True

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def hit(self, key: str) -> None:
        with self._lock:
            self._recent(key).append(self._now())


class Devices:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- pairing
    def create_pairing_code(self, ttl: timedelta = PAIRING_CODE_TTL) -> str:
        """A one-time code for pairing one phone. Only its hash is stored."""
        code = secrets.token_urlsafe(24)
        now = utcnow()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM pairing_codes WHERE expires_at < ? OR used_at IS NOT NULL", (iso(now),))
            conn.execute("INSERT INTO pairing_codes (code_hash, created_at, expires_at) VALUES (?, ?, ?)",
                         (_hash(code), iso(now), iso(now + ttl)))
        return code

    def pair(self, code: str, name: str, public_key_b64: str) -> dict[str, Any]:
        """Register a device with a valid unused code. The code is spent even if two phones race for it."""
        name = (name or "").strip()[:60] or "phone"
        load_public_key(public_key_b64)
        device_id, now = uuid.uuid4().hex, iso(utcnow())
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute("UPDATE pairing_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL"
                               " AND expires_at >= ?", (now, _hash(code or ""), now))
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise PairingError("this pairing code is wrong, already used or expired; make a new one")
            conn.execute("INSERT INTO devices (id, name, public_key, created_at) VALUES (?, ?, ?, ?)",
                         (device_id, name, public_key_b64.strip(), now))
            conn.execute("COMMIT")
        return self.get(device_id)

    # ---------------------------------------------------------------- devices
    def get(self, device_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            raise KeyError(device_id)
        return dict(row)

    def list(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM devices ORDER BY created_at, rowid")]

    def revoke(self, device_id: str) -> bool:
        """Cut a device off. False if it was already revoked. Raises KeyError if there is no such device."""
        self.get(device_id)
        with self.db.connect() as conn:
            cur = conn.execute("UPDATE devices SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                               (iso(utcnow()), device_id))
            return cur.rowcount == 1

    def touch(self, device_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE devices SET last_seen_at = ? WHERE id = ?", (iso(utcnow()), device_id))

    @staticmethod
    def public(device: dict[str, Any]) -> dict[str, Any]:
        return {k: device[k] for k in ("id", "name", "created_at", "last_seen_at", "revoked_at")}


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class RequestVerifier:
    """Checks the five headers of a request. Framework-free so it can be tested and reasoned about alone."""

    def __init__(self, devices: Devices, now: Callable[[], float] = time.time):
        self.devices = devices
        self._now = now
        self._nonces: dict[str, float] = {}           # "device:nonce" -> when it stops mattering
        self._seen_at: dict[str, float] = {}
        self._lock = threading.Lock()
        self.failures = RateLimiter(limit=20, window=60)       # bad requests per IP
        self.requests = RateLimiter(limit=1200, window=60)     # good requests per device (video is chatty)

    def verify(self, *, method: str, path_and_query: str, headers, body: bytes | None, client_ip: str) -> dict[str, Any]:
        """Return the device that sent this request, or raise AuthError. `body=None` means the body is
        streamed: the signature is checked against the claimed hash, and whoever reads the stream must
        compare its real hash with `claimed_sha256` in the returned dict."""
        if self.failures.blocked(client_ip):
            raise AuthError("too many failed requests; slow down", 429)
        try:
            device = self._check(method, path_and_query, headers, body)
        except AuthError:
            self.failures.hit(client_ip)
            raise
        if not self.requests.allow(device["id"]):
            raise AuthError("too many requests; slow down", 429)
        return device

    def _check(self, method: str, path_and_query: str, headers, body: bytes | None) -> dict[str, Any]:
        device_id, timestamp = headers.get("x-device", ""), headers.get("x-timestamp", "")
        nonce, claimed = headers.get("x-nonce", ""), headers.get("x-content-sha256", "")
        signature = headers.get("x-signature", "")
        if not (device_id and timestamp and nonce and claimed and signature):
            raise AuthError("this request is not signed by a paired device")
        try:
            device = self.devices.get(device_id)
        except KeyError:
            raise AuthError("unknown device") from None
        if device["revoked_at"]:
            raise AuthError("this device has been revoked")
        now = self._now()
        if not timestamp.isdigit() or abs(now - int(timestamp)) > MAX_CLOCK_SKEW:
            raise AuthError("the request's timestamp is too far from the server's clock")
        if not _NONCE_RE.match(nonce) or not _SHA_RE.match(claimed):
            raise AuthError("malformed nonce or body hash")
        if body is not None and hashlib.sha256(body).hexdigest() != claimed:
            raise AuthError("the body does not match its signed hash")
        if not signature_is_valid(device["public_key"], signature,
                                  signing_string(method, path_and_query, timestamp, nonce, claimed)):
            raise AuthError("bad signature")
        key = f"{device_id}:{nonce}"
        with self._lock:                       # only a correctly signed request can use up a nonce
            for k in [k for k, until in self._nonces.items() if until < now]:
                del self._nonces[k]
            if key in self._nonces:
                raise AuthError("this request was already used (replay)")
            self._nonces[key] = now + 2 * MAX_CLOCK_SKEW
            stale = now - self._seen_at.get(device_id, 0) > 60
            if stale:
                self._seen_at[device_id] = now
        if stale:
            self.devices.touch(device_id)
        return {**device, "claimed_sha256": claimed}


def pairing_uri(settings: Settings, code: str) -> str:
    """What the QR code holds: where the server is and the one-time code."""
    from urllib.parse import quote

    return f"clip-pipeline://pair?url={quote(settings.public_url, safe='')}&code={code}"
