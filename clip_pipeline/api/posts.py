"""Posts: what is scheduled, what went out, and changing or cancelling what has not gone out yet."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from clip_pipeline.api.clips import TargetSpec, _clean_tags
from clip_pipeline.publish.queue import PostBusy, PostQueue
from clip_pipeline.worker import Worker


class PostEdit(BaseModel):
    """Everything is optional; send only what changed."""
    title: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2200)
    tags: list[str] | None = Field(default=None, max_length=30)
    scheduled_at: datetime | None = None            # one new time for everything still waiting
    # The wanted set of accounts. New ones get their next free slot unless a time is given; waiting or
    # failed ones left out are dropped; give a time on one to move just that one.
    targets: list[TargetSpec] | None = Field(default=None, min_length=1, max_length=20)

    _tags = field_validator("tags")(_clean_tags)

    @field_validator("scheduled_at")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return None if v is None else (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def post_json(post: dict, account_names: dict[str, str] | None = None) -> dict:
    d = dict(post)
    d.pop("file_path", None)       # server-side paths are nobody else's business
    names = account_names or {}
    d["targets"] = [{**t, "account": names.get(t["account_id"] or "")} for t in post["targets"]]
    d["video_url"] = f"/clips/{post['clip_id']}/video" if post.get("clip_id") else None
    d["editable"] = any(t["status"] in ("pending", "failed") for t in post["targets"])
    return d


def build_router(queue: PostQueue, worker: Worker) -> APIRouter:
    router = APIRouter(prefix="/posts", tags=["posts"])

    def names() -> dict[str, str]:
        return {a["id"]: a["name"] for a in worker.accounts.list()}

    def get_or_404(post_id: str) -> dict:
        post = queue.get_post(post_id)
        if post is None:
            raise HTTPException(404, "post not found")
        return post

    @router.get("")
    def list_posts(status: Literal["pending", "processing", "completed", "failed", "partial"] | None = None,
                   limit: int = 100):
        """Newest first. Each post lists its targets: an account, its own time and its own state."""
        n = names()
        return {"posts": [post_json(p, n) for p in queue.list_posts(status=status, limit=max(1, min(limit, 500)))]}

    @router.get("/{post_id}")
    def get_post(post_id: str):
        return post_json(get_or_404(post_id), names())

    @router.patch("/{post_id}")
    def edit(post_id: str, body: PostEdit):
        """Change the text, the times or the accounts of a post that has not finished going out."""
        try:
            worker.edit_post(post_id, body.model_dump(exclude_unset=True, exclude_none=True))
        except KeyError:
            raise HTTPException(404, "post not found")
        except PostBusy as exc:
            raise HTTPException(409, str(exc))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return post_json(get_or_404(post_id), names())

    @router.delete("/{post_id}")
    def cancel(post_id: str):
        """Cancel what has not been posted yet. If nothing went out, the clip returns to review.
        Nothing is ever removed from a platform."""
        try:
            result = worker.cancel_post(post_id)
        except KeyError:
            raise HTTPException(404, "post not found")
        except PostBusy as exc:
            raise HTTPException(409, str(exc))
        return {"cancelled": result["cancelled"], "already_posted": result["kept"],
                "clip_back_in_review": bool(result["deleted"] and result["clip_id"])}

    @router.post("/{post_id}/retry")
    def retry(post_id: str):
        """Queue every failed target of this post again."""
        get_or_404(post_id)
        return {"requeued": queue.retry_failed(post_id)}

    return router
