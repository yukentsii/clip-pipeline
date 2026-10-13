"""The HTTP API.  Run:  python -m clip_pipeline serve

One process owns everything: the database, the worker that turns sources into clips, and the
scheduler that publishes due posts. The Android app is a client of these endpoints:

    /pair       register a phone with a one-time code (the only endpoint open to strangers, besides /health)
    /sources    send a link or a video, watch it being processed
    /clips      the review feed; play, edit, approve, reject
    /posts      the schedule; edit or cancel what has not gone out
    /accounts   where clips are posted: logins and daily posting slots
    /devices    paired phones; revoke one
    /status     what is running and what needs attention      /events    what happened (for notifications)
    /insights   reach over time                               /settings  review score and render defaults

Every request except /pair and /health must be signed by a paired device (see clip_pipeline/auth.py).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from clip_pipeline.auth import (STREAMED_PREFIXES, AuthError, Devices, PairingError, RateLimiter,
                                RequestVerifier)
from clip_pipeline.config import Settings, get_settings
from clip_pipeline.db import Database
from clip_pipeline.publish.accounts import Accounts
from clip_pipeline.publish.queue import PostQueue
from clip_pipeline.publish.stats import StatsCollector
from clip_pipeline.publish.uploaders.base import BaseUploader
from clip_pipeline.store import Store
from clip_pipeline.worker import Worker

log = logging.getLogger("clip_pipeline")


class PairRequest(BaseModel):
    code: str = Field(min_length=8, max_length=128)
    name: str = Field(default="phone", max_length=60)          # what to call this device in the list
    public_key: str = Field(min_length=40, max_length=1024)    # base64 DER, ECDSA P-256


def create_app(settings: Settings | None = None, *,
               uploader_factories: dict[str, Callable[[Settings], BaseUploader]] | None = None,
               background: bool = True) -> FastAPI:
    """`background=False` builds the app without starting the worker or the scheduler (tests).
    `uploader_factories` replaces the real platform uploaders (tests)."""
    from clip_pipeline.api import accounts as accounts_api
    from clip_pipeline.api import clips, posts, sources, status

    s = settings or get_settings()
    db = Database(s.db_path)
    store = Store(db, s.sources_dir)
    queue = PostQueue(db)
    accounts = Accounts(db, s, uploader_factories)
    worker = Worker(store, queue, s, accounts)
    collector = StatsCollector(db, accounts)
    devices = Devices(db)
    verifier = RequestVerifier(devices)
    pairing_attempts = RateLimiter(limit=5, window=600)        # per IP: guessing a code is hopeless

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        if background:
            for acct in accounts.import_env():
                log.info("Created %s account %r from the logins in .env", acct["platform"], acct["name"])
            if not s.auth_required:
                log.warning("AUTH_REQUIRED is off: anyone who can reach this address can use the service")
            worker.start(poll_seconds=s.poll_seconds)
            if s.scheduler_enabled:
                from clip_pipeline.publish.scheduler import start_scheduler

                for acct in accounts.list():
                    log.info("account %-9s %-20s configured=%s", acct["platform"], acct["name"],
                             accounts.is_configured(acct))
                scheduler = start_scheduler(queue, accounts.uploader, s, on_result=worker.after_upload,
                                            collect_stats=collector.collect)
        yield
        if scheduler:
            scheduler.shutdown(wait=False)
        worker.shutdown()

    # The interactive docs describe every endpoint to whoever asks, so they exist only in development.
    app = FastAPI(title="clip-pipeline", lifespan=lifespan,
                  docs_url=None if s.auth_required else "/docs", redoc_url=None,
                  openapi_url=None if s.auth_required else "/openapi.json")
    app.state.settings, app.state.db, app.state.store = s, db, store
    app.state.queue, app.state.worker, app.state.accounts, app.state.collector = queue, worker, accounts, collector
    app.state.devices, app.state.verifier = devices, verifier

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    async def paired_device(request: Request) -> dict | None:
        """Dependency on every protected route: the request must be signed by a paired device."""
        if not s.auth_required:
            return None
        raw_path = request.scope.get("raw_path") or request.url.path.encode()
        query = request.scope.get("query_string", b"")
        path = raw_path.decode("latin-1") + ("?" + query.decode("latin-1") if query else "")
        streamed = request.url.path.startswith(STREAMED_PREFIXES)
        try:
            device = verifier.verify(method=request.method, path_and_query=path, headers=request.headers,
                                     body=None if streamed else await request.body(), client_ip=client_ip(request))
        except AuthError as exc:
            raise HTTPException(exc.status, str(exc))
        request.state.device = device
        request.state.claimed_sha256 = device["claimed_sha256"]
        return device

    protected = [Depends(paired_device)]

    # ---------------------------------------------------------------- open endpoints
    @app.get("/health", tags=["status"])
    def health():
        """Is the service up? Says nothing else: this one is open to anyone who can reach the address."""
        return {"ok": True}

    @app.post("/pair", status_code=201, tags=["devices"])
    def pair(body: PairRequest, request: Request):
        """Register this phone with the one-time code from `python -m clip_pipeline pair`."""
        if not pairing_attempts.allow(client_ip(request)):
            raise HTTPException(429, "too many pairing attempts; wait ten minutes")
        try:
            device = devices.pair(body.code, body.name, body.public_key)
        except PairingError as exc:
            raise HTTPException(403, str(exc))
        log.info("Paired device %r (%s)", device["name"], device["id"][:8])
        return {"device_id": device["id"], "name": device["name"]}

    # ---------------------------------------------------------------- protected endpoints
    @app.get("/system", tags=["status"], dependencies=protected)
    def system():
        from clip_pipeline.hardware import hardware_report

        hw = hardware_report()
        return {
            "schema": db.version(), "ffmpeg": bool(hw.ffmpeg), "encoder": hw.video_encoder,
            "gemini": bool(s.gemini_api_key), "gemini_models": list(s.gemini_models),
            "whisper_model": s.whisper_model, "review_min_score": worker.prefs()["review_min_score"],
            "sources_waiting": len(store.list_sources(status=("queued", "running"))),
            "clips_in_review": len(store.list_clips(status="review")),
        }

    @app.get("/devices", tags=["devices"], dependencies=protected)
    def list_devices(request: Request):
        me = getattr(request.state, "device", None)
        return {"devices": [{**Devices.public(d), "this_device": bool(me and d["id"] == me["id"])}
                            for d in devices.list()]}

    @app.delete("/devices/{device_id}", tags=["devices"], dependencies=protected)
    def revoke_device(device_id: str):
        """Cut a phone off (a lost one, say). It can no longer make any request; pair again to undo."""
        try:
            return {"revoked": devices.revoke(device_id)}
        except KeyError:
            raise HTTPException(404, "device not found")

    app.include_router(sources.build_router(store, worker), dependencies=protected)
    app.include_router(clips.build_router(store, worker), dependencies=protected)
    app.include_router(posts.build_router(queue, worker), dependencies=protected)
    app.include_router(accounts_api.build_router(accounts), dependencies=protected)
    app.include_router(status.build_router(store, queue, worker), dependencies=protected)
    return app
