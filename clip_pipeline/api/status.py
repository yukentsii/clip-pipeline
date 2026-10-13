"""Status, events, insights and settings: what is going on, what happened, and how it is doing."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from clip_pipeline.api.sources import source_json
from clip_pipeline.publish import stats
from clip_pipeline.publish.queue import PostQueue
from clip_pipeline.store import Store
from clip_pipeline.worker import Worker


class SettingsEdit(BaseModel):
    """Everything is optional; send only what changed. New values apply to sources processed from now on."""
    review_min_score: int | None = Field(default=None, ge=0, le=100)
    default_aspect: Literal["9:16", "1:1", "16:9"] | None = None
    default_layout: Literal["auto", "crop", "fit"] | None = None
    default_subtitle_style: Literal["karaoke", "static", "none"] | None = None


def build_router(store: Store, queue: PostQueue, worker: Worker) -> APIRouter:
    router = APIRouter(tags=["status"])
    accounts = worker.accounts

    def clip_counts() -> dict[str, int]:
        with store.db.connect() as conn:
            return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM clips GROUP BY status")}

    @router.get("/status")
    def status():
        """`needs_attention` is the list to show first: everything that failed, with the reason and what
        can be done about it."""
        sources = store.list_sources(limit=200)
        names = {s.id: s.title for s in sources}
        account_names = {a["id"]: a["name"] for a in accounts.list()}
        attention: list[dict[str, Any]] = []
        for s in sources:
            if s.status == "failed":
                attention.append({"kind": "source", "id": s.id, "title": s.title or s.url, "stage": s.stage,
                                  "reason": (s.error or "").strip()[:500], "action": f"POST /sources/{s.id}/retry"})
        for c in store.list_clips(status="failed"):
            attention.append({"kind": "clip", "id": c.id, "title": c.title, "source": names.get(c.source_id, ""),
                              "reason": (c.error or "").strip()[:500], "action": f"POST /clips/{c.id}/retry"})
        posts = queue.list_posts(limit=200)
        for p in posts:
            for t in p["targets"]:
                if t["status"] == "failed":
                    attention.append({"kind": "post", "id": p["id"], "title": p["title"], "platform": t["platform"],
                                      "account": account_names.get(t["account_id"] or ""),
                                      "reason": (t["error"] or "").strip()[:500],
                                      "action": f"POST /posts/{p['id']}/retry"})
        public = [accounts.public(a) for a in accounts.list()]
        for a in public:
            if a["enabled"] and not a["configured"]:
                attention.append({"kind": "account", "id": a["id"], "title": a["name"], "platform": a["platform"],
                                  "reason": "its login is missing or incomplete", "action": f"PATCH /accounts/{a['id']}"})
        post_counts: dict[str, int] = {}
        for p in posts:
            post_counts[p["status"]] = post_counts.get(p["status"], 0) + 1
        waiting = sorted(t["scheduled_at"] for p in posts for t in p["targets"] if t["status"] == "pending")
        return {
            "working_on": [source_json(store, s) for s in sources if s.status == "running"],
            "queued_sources": sum(s.status == "queued" for s in sources),
            "clips": clip_counts(),
            "posts": post_counts,
            "next_post_at": waiting[0] if waiting else None,
            "accounts": [{k: a[k] for k in ("id", "platform", "name", "enabled", "configured", "next_slot")}
                         for a in public],
            "needs_attention": attention,
        }

    @router.get("/events")
    def events(after: int | None = None, limit: int = 100):
        """What happened: clips ready, uploads done, failures. Pass the last id you saw as `after` to get
        only what is new (oldest first); without it you get the most recent ones (newest first)."""
        limit = max(1, min(limit, 500))
        rows = store.latest_events(limit) if after is None else store.list_events(after, limit)
        return {"events": rows, "last_id": max((e["id"] for e in rows), default=after or 0)}

    @router.get("/insights")
    def insights(days: int = 30):
        """Reach over the last `days` days: totals, the daily curve, this week against last week, the
        best clips, and average views grouped by how clips were made."""
        return stats.insights(store.db, worker.s, days=max(1, min(days, 365)))

    @router.get("/settings")
    def get_settings():
        return worker.prefs()

    @router.patch("/settings")
    def edit_settings(body: SettingsEdit):
        try:
            return worker.set_prefs(body.model_dump(exclude_unset=True, exclude_none=True))
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    return router
