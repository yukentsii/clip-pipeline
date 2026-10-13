"""Sources: sending a link or a video file to the service, and watching it being processed."""
from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from clip_pipeline.sources import local
from clip_pipeline.sources.downloader import InvalidURLError
from clip_pipeline.store import SOURCE_STAGES, Source, Store
from clip_pipeline.worker import StateError, Worker

ContentHint = Literal["podcast", "gaming", "film", "music", "sports", "vlog", "education", "other"]


class LinkRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    content_type: ContentHint | None = None   # only if you already know it; otherwise it is detected


def source_json(store: Store, src: Source) -> dict:
    d = asdict(src)
    d.pop("fingerprint")
    d.pop("file_path")             # server-side paths are nobody else's business
    d["stages"] = list(SOURCE_STAGES)
    counts: dict[str, int] = {}
    for clip in store.list_clips(source_id=src.id):
        counts[clip.status] = counts.get(clip.status, 0) + 1
    d["clips"] = counts
    return d


def build_router(store: Store, worker: Worker) -> APIRouter:
    router = APIRouter(prefix="/sources", tags=["sources"])
    settings = worker.s

    @router.post("", status_code=202)
    def add_link(body: LinkRequest):
        """Queue a link. Processing starts by itself; a video already seen is returned, not redone."""
        try:
            src, is_new = worker.add_url(body.url, body.content_type)
        except InvalidURLError as exc:
            raise HTTPException(422, str(exc))
        return {"id": src.id, "duplicate": not is_new}

    @router.post("/upload/{filename}", status_code=202)
    async def upload(filename: str, request: Request, content_type: ContentHint | None = None):
        """Send a video as the raw request body. It is written straight to disk (no temporary copy)
        and queued as soon as the last byte has arrived."""
        try:
            dest = local.unused_inbox_path(filename, settings.inbox_dir)
        except local.LocalSourceError as exc:
            raise HTTPException(415, str(exc))
        limit = int(worker.s.max_upload_gb * 1024 ** 3)
        part = dest.with_name(dest.name + local.PARTIAL_SUFFIX)   # the inbox watcher ignores *.part
        size = 0
        digest = hashlib.sha256()
        try:
            with part.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"file exceeds {worker.s.max_upload_gb:g} GB")
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise HTTPException(400, "empty upload")
            # The signature was checked against the hash the device claimed; now that the bytes are
            # here, make sure they are the bytes it signed.
            signed = getattr(request.state, "claimed_sha256", None)
            if signed is not None and digest.hexdigest() != signed:
                raise HTTPException(401, "the uploaded bytes do not match their signed hash")
            part.replace(dest)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        src, is_new = worker.add_file(dest, content_type, take=True)
        return {"id": src.id, "duplicate": not is_new, "size": size}

    @router.get("")
    def list_sources(limit: int = 50):
        return {"sources": [source_json(store, s) for s in store.list_sources(limit=max(1, min(limit, 200)))]}

    @router.get("/{source_id}")
    def get_source(source_id: str):
        try:
            return source_json(store, store.get_source(source_id))
        except KeyError:
            raise HTTPException(404, "source not found")

    @router.post("/{source_id}/retry", status_code=202)
    def retry(source_id: str):
        """Queue a failed source again; it carries on from the stage that failed."""
        try:
            worker.retry_source(source_id)
        except KeyError:
            raise HTTPException(404, "source not found")
        except StateError as exc:
            raise HTTPException(409, str(exc))
        return {"status": "queued"}

    return router
