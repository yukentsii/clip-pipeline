"""Clips: the review feed, and deciding what happens to each clip."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from clip_pipeline.store import Clip, Store
from clip_pipeline.worker import StateError, Worker

Platform = Literal["youtube", "instagram", "tiktok"]
ClipStatus = Literal["below_threshold", "duplicate", "candidate", "slicing", "sliced", "rendering", "review",
                     "scheduled", "posted", "rejected", "failed"]


def _clean_tags(v: list[str] | None) -> list[str] | None:
    if v is None:
        return None
    out: list[str] = []
    for tag in v:
        tag = tag.strip().lstrip("#")[:60]
        if tag and tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
    return out


class ClipEdit(BaseModel):
    """Everything is optional; send only what changed."""
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2200)
    tags: list[str] | None = Field(default=None, max_length=30)
    hook_title: str | None = Field(default=None, max_length=80)     # "" removes the on-screen title
    start: float | None = Field(default=None, ge=0)
    end: float | None = Field(default=None, gt=0)
    aspect: Literal["9:16", "1:1", "16:9"] | None = None
    layout: Literal["auto", "crop", "fit"] | None = None
    subtitle_style: Literal["karaoke", "static", "none"] | None = None

    _tags = field_validator("tags")(_clean_tags)


class TargetSpec(BaseModel):
    """One place a clip goes. Name the account; leave `scheduled_at` out to use its next free slot."""
    account_id: str | None = Field(default=None, max_length=64)
    platform: Platform | None = None           # alternative to account_id: that platform's first account
    scheduled_at: datetime | None = None

    @field_validator("scheduled_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

    @model_validator(mode="after")
    def _one_of(self):
        if not self.account_id and not self.platform:
            raise ValueError("give an account_id or a platform")
        return self


class ApproveRequest(BaseModel):
    targets: list[TargetSpec] = Field(min_length=1, max_length=20)
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2200)
    tags: list[str] | None = Field(default=None, max_length=30)
    visibility: Literal["public", "unlisted", "private"] | None = None   # YouTube only

    _tags = field_validator("tags")(_clean_tags)


def clip_json(clip: Clip, source_title: str = "") -> dict:
    d = asdict(clip)
    for server_side in ("slice_file", "render_file", "thumb_file"):
        d.pop(server_side)
    d["source_title"] = source_title
    d["duration"] = round(clip.end - clip.start, 2)
    d["trim_min"], d["trim_max"] = clip.slice_start, clip.slice_end    # how far the trim handles can go
    # The file name is part of the URL, so a re-rendered clip is never served from the player's cache.
    d["video_url"] = f"/clips/{clip.id}/video?v={clip.render_file.rsplit('/', 1)[-1]}" if clip.render_file else None
    d["thumb_url"] = f"/clips/{clip.id}/thumb" if clip.thumb_file else None
    return d


def build_router(store: Store, worker: Worker) -> APIRouter:
    router = APIRouter(prefix="/clips", tags=["clips"])

    def get_or_404(clip_id: str) -> Clip:
        try:
            return store.get_clip(clip_id)
        except KeyError:
            raise HTTPException(404, "clip not found")

    def titles() -> dict[str, str]:
        return {s.id: s.title for s in store.list_sources(limit=1000)}

    @router.get("")
    def list_clips(status: ClipStatus = "review", source_id: str | None = None, limit: int = 100):
        """The review feed by default: finished clips waiting for a decision, best first."""
        names = titles()
        clips = store.list_clips(status=status, source_id=source_id, limit=max(1, min(limit, 500)))
        return {"clips": [clip_json(c, names.get(c.source_id, "")) for c in clips]}

    @router.get("/{clip_id}")
    def get_clip(clip_id: str):
        clip = get_or_404(clip_id)
        return clip_json(clip, store.get_source(clip.source_id).title)

    @router.get("/{clip_id}/video")
    def video(clip_id: str):
        """The rendered clip, exactly as it will be posted. Supports seeking (HTTP range requests)."""
        clip = get_or_404(clip_id)
        path = store.source_dir(clip.source_id) / clip.render_file if clip.render_file else None
        if path is None or not path.is_file():
            raise HTTPException(404, "this clip has no rendered video (yet)")
        return FileResponse(path, media_type="video/mp4")

    @router.get("/{clip_id}/thumb")
    def thumb(clip_id: str):
        clip = get_or_404(clip_id)
        path = store.source_dir(clip.source_id) / clip.thumb_file if clip.thumb_file else None
        if path is None or not path.is_file():
            raise HTTPException(404, "this clip has no thumbnail (yet)")
        return FileResponse(path, media_type="image/jpeg")

    @router.patch("/{clip_id}")
    def edit(clip_id: str, body: ClipEdit):
        """Edit a clip in review. Text is saved at once; a change to the picture (trim, framing,
        captions, hook title) re-renders it, and the clip is back in review a few seconds later."""
        get_or_404(clip_id)
        try:
            status = worker.edit_clip(clip_id, body.model_dump(exclude_unset=True, exclude_none=True))
        except StateError as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {**clip_json(store.get_clip(clip_id)), "rerendering": status != "review"}

    @router.post("/{clip_id}/approve", status_code=201)
    def approve(clip_id: str, body: ApproveRequest):
        get_or_404(clip_id)
        try:
            post_id = worker.approve(clip_id, body.model_dump())
        except StateError as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {"post_id": post_id, "status": "scheduled"}

    @router.post("/{clip_id}/reject")
    def reject(clip_id: str):
        clip = get_or_404(clip_id)
        if not worker.reject(clip_id):
            raise HTTPException(409, f"clip is {clip.status}; only clips waiting for review can be rejected")
        return {"status": "rejected"}

    @router.post("/{clip_id}/retry", status_code=202)
    def retry(clip_id: str):
        """Try a failed clip again from the step that failed."""
        get_or_404(clip_id)
        try:
            return {"status": worker.retry_clip(clip_id)}
        except StateError as exc:
            raise HTTPException(409, str(exc))

    return router
