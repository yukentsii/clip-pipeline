"""Step 5: only paired devices may use the service, and a signed request can't be replayed or altered.

`Phone` below does exactly what the Android app has to do, so it doubles as a reference client.
"""
import base64
import hashlib
import json
import secrets
import time
from dataclasses import replace
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient

from clip_pipeline import __main__ as cli
from clip_pipeline import auth
from clip_pipeline.api.app import create_app
from clip_pipeline.auth import Devices, PairingError, RateLimiter
from clip_pipeline.config import get_settings
from clip_pipeline.db import Database

from test_worker import Stages


def public_b64(private_key) -> str:
    return base64.b64encode(private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


class Phone:
    """A paired device: holds a P-256 key and signs every request."""

    def __init__(self, client: TestClient, name="Pixel"):
        self.client, self.name = client, name
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.device_id = None

    def pair(self, code: str):
        r = self.client.post("/pair", json={"code": code, "name": self.name, "public_key": public_b64(self.key)})
        if r.status_code == 201:
            self.device_id = r.json()["device_id"]
        return r

    def headers(self, method: str, path: str, body: bytes = b"", *, timestamp=None, nonce=None, body_hash=None) -> dict:
        timestamp = str(int(time.time()) if timestamp is None else timestamp)
        nonce = nonce or secrets.token_urlsafe(18)
        body_hash = body_hash or hashlib.sha256(body).hexdigest()
        message = "\n".join((method.upper(), path, timestamp, nonce, body_hash)).encode()
        signature = self.key.sign(message, ec.ECDSA(hashes.SHA256()))
        return {"X-Device": self.device_id, "X-Timestamp": timestamp, "X-Nonce": nonce,
                "X-Content-SHA256": body_hash, "X-Signature": base64.b64encode(signature).decode()}

    def request(self, method: str, path: str, body=None, **sign):
        raw = b"" if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = self.headers(method, path, raw, **sign)
        if body is not None and not isinstance(body, bytes):
            headers["Content-Type"] = "application/json"
        return self.client.request(method, path, content=raw or None, headers=headers)

    def get(self, path, **sign):
        return self.request("GET", path, **sign)


@pytest.fixture
def stages(monkeypatch) -> Stages:
    return Stages(monkeypatch)


@pytest.fixture
def app(tmp_path, stages):
    app = create_app(replace(get_settings(), data_dir=tmp_path, auth_required=True), background=False)
    yield app
    app.state.worker.shutdown()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def phone(app, client) -> Phone:
    p = Phone(client)
    assert p.pair(app.state.devices.create_pairing_code()).status_code == 201
    return p


# ---------------------------------------------------------------- strangers

PROTECTED = [("GET", "/sources"), ("GET", "/clips"), ("GET", "/posts"), ("GET", "/accounts"), ("GET", "/status"),
             ("GET", "/events"), ("GET", "/insights"), ("GET", "/settings"), ("GET", "/system"), ("GET", "/devices"),
             ("POST", "/sources"), ("POST", "/accounts"), ("PATCH", "/settings"), ("DELETE", "/devices/x"),
             ("POST", "/sources/upload/a.mp4"), ("GET", "/clips/x/video"), ("POST", "/clips/x/approve")]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_nothing_works_without_a_paired_device(client, method, path):
    r = client.request(method, path, json={} if method != "GET" else None)
    assert r.status_code == 401 and "not signed" in r.json()["detail"]


def test_only_health_and_pairing_are_open_and_docs_are_off(client):
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404
    assert client.post("/pair", json={"code": "x" * 12, "public_key": "A" * 60}).status_code == 403


# ---------------------------------------------------------------- pairing

def test_pairing_code_works_once_for_one_phone(app, client):
    code = app.state.devices.create_pairing_code()
    first, second = Phone(client, "Pixel"), Phone(client, "Tablet")
    r = first.pair(code)
    assert r.status_code == 201 and r.json()["name"] == "Pixel"
    assert second.pair(code).status_code == 403                                    # the code is spent
    assert first.get("/status").status_code == 200
    assert [d["name"] for d in app.state.devices.list()] == ["Pixel"]


def test_wrong_and_expired_codes_are_refused(app, client):
    assert Phone(client).pair("definitely-not-the-code").status_code == 403
    expired = app.state.devices.create_pairing_code(ttl=timedelta(seconds=-1))
    assert Phone(client).pair(expired).status_code == 403
    assert app.state.devices.list() == []


def test_only_p256_keys_are_accepted(app):
    devices = app.state.devices
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for bad in (public_b64(rsa_key), public_b64(ec.generate_private_key(ec.SECP384R1())), "not base64!!", "QUJD"):
        code = devices.create_pairing_code()
        with pytest.raises(PairingError):
            devices.pair(code, "x", bad)
        devices.pair(code, "ok", public_b64(ec.generate_private_key(ec.SECP256R1())))   # the code was not burned
    assert all(d["name"] == "ok" for d in devices.list())


def test_pairing_attempts_are_rate_limited(client):
    for _ in range(5):
        assert Phone(client).pair("guess-" + secrets.token_hex(4)).status_code == 403
    assert Phone(client).pair("guess-again").status_code == 429


def test_stored_pairing_code_is_only_a_hash(app):
    code = app.state.devices.create_pairing_code()
    with app.state.db.connect() as conn:
        stored = [r[0] for r in conn.execute("SELECT code_hash FROM pairing_codes")]
    assert stored == [hashlib.sha256(code.encode()).hexdigest()] and code not in stored


# ---------------------------------------------------------------- signed requests

def test_signed_requests_work_for_reads_writes_and_queries(phone):
    assert phone.get("/status").status_code == 200
    assert phone.get("/clips?status=review&limit=5").json() == {"clips": []}       # the query is part of what is signed
    r = phone.request("PATCH", "/settings", {"review_min_score": 80})
    assert r.status_code == 200 and r.json()["review_min_score"] == 80
    r = phone.request("POST", "/accounts", {"platform": "youtube", "name": "main"})
    assert r.status_code == 201 and phone.request("DELETE", f"/accounts/{r.json()['id']}").status_code == 204


def test_a_captured_request_cannot_be_replayed(phone):
    headers = phone.headers("GET", "/status")
    assert phone.client.get("/status", headers=headers).status_code == 200
    again = phone.client.get("/status", headers=headers)
    assert again.status_code == 401 and "already used" in again.json()["detail"]


def test_a_signed_request_cannot_be_altered(phone):
    body = json.dumps({"review_min_score": 80}).encode()
    headers = {**phone.headers("PATCH", "/settings", body), "Content-Type": "application/json"}
    tampered = phone.client.patch("/settings", content=json.dumps({"review_min_score": 1}).encode(), headers=headers)
    assert tampered.status_code == 401 and "body does not match" in tampered.json()["detail"]
    assert phone.client.get("/clips?status=scheduled", headers=phone.headers("GET", "/clips?status=review")).status_code == 401
    assert phone.client.get("/posts", headers=phone.headers("GET", "/clips")).status_code == 401       # other path
    assert phone.client.post("/status", headers=phone.headers("GET", "/status")).status_code in (401, 405)
    assert phone.get("/settings").json()["review_min_score"] == 70                 # none of it took effect


def test_old_or_future_timestamps_are_refused(phone):
    assert phone.get("/status", timestamp=int(time.time()) - 400).status_code == 401
    assert phone.get("/status", timestamp=int(time.time()) + 400).status_code == 401
    assert phone.get("/status", timestamp=int(time.time()) - 200).status_code == 200      # inside the 5 minute window
    assert phone.get("/status", timestamp="yesterday").status_code == 401


def test_malformed_headers_and_foreign_keys_are_refused(app, client, phone):
    assert phone.get("/status", nonce="short").status_code == 401
    assert phone.get("/status", body_hash="zz").status_code == 401
    intruder = Phone(client, "intruder")
    intruder.device_id = phone.device_id                       # knows the id, lacks the key
    r = intruder.get("/status")
    assert r.status_code == 401 and r.json()["detail"] == "bad signature"
    intruder.device_id = "0" * 32
    assert intruder.get("/status").json()["detail"] == "unknown device"


def test_repeated_failures_from_one_address_are_blocked(client, phone):
    intruder = Phone(client)
    intruder.device_id = phone.device_id
    for _ in range(20):
        assert intruder.get("/status").status_code == 401
    assert intruder.get("/status").status_code == 429
    assert phone.get("/status").status_code == 429             # same address: the block is per IP, not per key


# ---------------------------------------------------------------- devices

def test_devices_are_listed_without_keys_and_can_be_revoked(app, client, phone):
    tablet = Phone(client, "Tablet")
    tablet.pair(app.state.devices.create_pairing_code())
    listed = phone.get("/devices").json()["devices"]
    assert [(d["name"], d["this_device"]) for d in listed] == [("Pixel", True), ("Tablet", False)]
    assert all("public_key" not in d for d in listed) and listed[0]["last_seen_at"]

    assert phone.request("DELETE", f"/devices/{tablet.device_id}").json() == {"revoked": True}
    r = tablet.get("/status")
    assert r.status_code == 401 and "revoked" in r.json()["detail"]
    assert phone.get("/status").status_code == 200                                 # the other phone is unaffected
    assert phone.request("DELETE", f"/devices/{tablet.device_id}").json() == {"revoked": False}
    assert phone.request("DELETE", "/devices/missing").status_code == 404
    assert phone.request("DELETE", f"/devices/{phone.device_id}").json() == {"revoked": True}
    assert phone.get("/devices").status_code == 401                                # a phone can lock itself out


# ---------------------------------------------------------------- uploads (streamed, so hashed on arrival)

def test_signed_upload_is_accepted_and_tampered_upload_is_thrown_away(app, phone, tmp_path):
    video = b"real-video-bytes" * 1000
    path = "/sources/upload/My%20Clip.mp4?content_type=film"
    r = phone.client.post(path, content=video, headers=phone.headers("POST", path, video))
    assert r.status_code == 202 and r.json()["size"] == len(video)
    assert app.state.worker.wait_idle(timeout=20)

    other = b"something-else-entirely" * 500
    r = phone.client.post(path, content=other, headers=phone.headers("POST", path, video))    # signed for `video`
    assert r.status_code == 401 and "do not match" in r.json()["detail"]
    assert list((tmp_path / "inbox").iterdir()) == [] and len(app.state.store.list_sources()) == 1
    assert phone.client.post(path, content=video).status_code == 401               # unsigned: refused before reading


# ---------------------------------------------------------------- pieces

def test_rate_limiter_window_slides():
    clock = [0.0]
    limiter = RateLimiter(limit=2, window=10, now=lambda: clock[0])
    assert limiter.allow("a") and limiter.allow("a") and not limiter.allow("a")
    assert limiter.allow("b")                                  # keys are independent
    clock[0] = 10.1
    assert not limiter.blocked("a") and limiter.allow("a")


def test_verifier_forgets_nonces_after_the_window(app, phone):
    verifier = app.state.verifier
    clock = [time.time()]
    verifier._now = lambda: clock[0]
    headers = {k.lower(): v for k, v in phone.headers("GET", "/status", timestamp=int(clock[0])).items()}
    args = dict(method="GET", path_and_query="/status", headers=headers, body=b"", client_ip="1.2.3.4")
    verifier.verify(**args)
    with pytest.raises(auth.AuthError, match="already used"):
        verifier.verify(**args)
    clock[0] += 3 * auth.MAX_CLOCK_SKEW                        # the nonce is forgotten, but the timestamp is stale
    with pytest.raises(auth.AuthError, match="timestamp"):
        verifier.verify(**args)
    fresh = {k.lower(): v for k, v in phone.headers("GET", "/status", timestamp=int(clock[0])).items()}
    verifier.verify(**{**args, "headers": fresh})              # the next good request sweeps out expired nonces
    assert list(verifier._nonces) == [f"{phone.device_id}:{fresh['x-nonce']}"]


def test_cli_pair_and_devices(tmp_path, monkeypatch, capsys):
    s = replace(get_settings(), data_dir=tmp_path, bind_host="192.168.1.20", port=8123)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    assert cli.main(["pair"]) == 0
    out = capsys.readouterr().out
    code = next(line.split()[-1] for line in out.splitlines() if line.startswith("code:"))
    assert "server: http://192.168.1.20:8123" in out
    assert f"clip-pipeline://pair?url=http%3A%2F%2F192.168.1.20%3A8123&code={code}" in out
    devices = Devices(Database(s.db_path))
    device = devices.pair(code, "My Phone", public_b64(ec.generate_private_key(ec.SECP256R1())))
    assert cli.main(["devices"]) == 0 and "My Phone" in capsys.readouterr().out
    assert cli.main(["devices", "revoke", device["id"][:8]]) == 0
    assert "revoked" in capsys.readouterr().out and devices.get(device["id"])["revoked_at"]
    assert cli.main(["devices", "revoke", "nope"]) == 2


def test_pairing_note_when_only_listening_locally(tmp_path, monkeypatch, capsys):
    s = replace(get_settings(), data_dir=tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    cli.main(["pair"])
    assert "only listens on this machine" in capsys.readouterr().out
