"""The post queue.

posts         - one row per approved clip that is due to be published (file + text)
post_targets  - one row per place it goes: a platform, an account and its own time. Each target
                succeeds, fails and retries independently of the others.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from clip_pipeline.db import Database, iso, utcnow

__all__ = ["PostBusy", "PostQueue", "derive_status", "iso", "utcnow"]

OPEN = ("pending", "failed")   # target states that can still be changed or cancelled


class PostBusy(RuntimeError):
    """The change can't be made right now (an upload is in progress, or nothing is left to change)."""


def derive_status(statuses: list[str]) -> str:
    s = set(statuses)
    if not s:
        return "pending"
    if s == {"completed"}:
        return "completed"
    if "processing" in s:
        return "processing"
    if "pending" in s:
        return "pending"
    if s == {"failed"}:
        return "failed"
    return "partial"  # mix of completed + failed


def _key(target: dict[str, Any] | sqlite3.Row) -> tuple[str, str]:
    return target["platform"], target["account_id"] or ""


class PostQueue:
    def __init__(self, db: Database):
        self.db = db

    # ---------------------------------------------------------------- writes
    def create_post(self, *, title: str, description: str, tags: list[str], file_path: Path | str,
                    targets: list[dict[str, Any]] | None = None, platforms: list[str] | None = None,
                    scheduled_at: datetime | None = None, extra: dict | None = None,
                    clip_id: str | None = None, conn: sqlite3.Connection | None = None) -> str:
        """Queue a rendered file. `targets` is a list of {platform, account_id, scheduled_at}; the short
        form `platforms` + `scheduled_at` means "each platform's first account, all at that time".
        Pass `conn` to make it part of a transaction the caller already opened."""
        if targets is None:
            if not platforms or scheduled_at is None:
                raise ValueError("a post needs targets, or platforms and a time")
            targets = [{"platform": p, "account_id": None, "scheduled_at": scheduled_at} for p in platforms]
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for t in targets:
            unique.setdefault(_key(t), t)
        if not unique:
            raise ValueError("a post needs at least one target")
        post_id = uuid.uuid4().hex
        now = iso(utcnow())
        times = [iso(t["scheduled_at"]) for t in unique.values()]

        def insert(c: sqlite3.Connection):
            c.execute(
                "INSERT INTO posts (id, clip_id, title, description, tags, file_path, scheduled_at, created_at, extra)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, clip_id, title, description, json.dumps(tags), str(file_path), min(times), now,
                 json.dumps(extra or {})),
            )
            c.executemany(
                "INSERT INTO post_targets (post_id, platform, account_id, scheduled_at, next_attempt_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [(post_id, t["platform"], t["account_id"], when, when, now) for t, when in zip(unique.values(), times)],
            )

        if conn is not None:
            insert(conn)
            return post_id
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            insert(c)
            c.execute("COMMIT")
        return post_id

    def claim_due(self, now: datetime | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Atomically move due pending targets to 'processing' and return them with their post's data."""
        now_s = iso(now or utcnow())
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT p.*, t.id AS target_id, t.platform, t.account_id, t.attempts, t.scheduled_at AS target_time"
                " FROM post_targets t JOIN posts p ON p.id = t.post_id"
                " WHERE t.status = 'pending' AND t.scheduled_at <= ? AND t.next_attempt_at <= ?"
                " ORDER BY t.scheduled_at, t.id LIMIT ?",
                (now_s, now_s, limit),
            ).fetchall()
            conn.executemany("UPDATE post_targets SET status = 'processing', updated_at = ? WHERE id = ?",
                             [(now_s, r["target_id"]) for r in rows])
            conn.execute("COMMIT")
        return [self._row(r) for r in rows]

    def mark_completed(self, target_id: int, remote_id: str, remote_url: str | None = None):
        now = iso(utcnow())
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE post_targets SET status = 'completed', remote_id = ?, remote_url = ?, error = NULL,"
                " attempts = attempts + 1, updated_at = ?, posted_at = ? WHERE id = ?",
                (remote_id, remote_url, now, now, target_id),
            )

    def mark_failed(self, target_id: int, error: str, *, retryable: bool, max_attempts: int,
                    backoff_seconds: int, now: datetime | None = None) -> str:
        """Record a failure; re-queue with exponential backoff unless out of attempts. Returns new status."""
        now = now or utcnow()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT attempts FROM post_targets WHERE id = ?", (target_id,)).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            status = "pending" if retryable and attempts < max_attempts else "failed"
            next_at = now + timedelta(seconds=backoff_seconds * (2 ** (attempts - 1)))
            conn.execute(
                "UPDATE post_targets SET status = ?, attempts = ?, error = ?, next_attempt_at = ?, updated_at = ?"
                " WHERE id = ?",
                (status, attempts, error[:2000], iso(next_at), iso(now), target_id),
            )
            conn.execute("COMMIT")
        return status

    def recover_stale(self) -> int:
        """Targets stuck in 'processing' (crash/restart) go back to pending."""
        with self.db.connect() as conn:
            cur = conn.execute("UPDATE post_targets SET status = 'pending', updated_at = ? WHERE status = 'processing'",
                               (iso(utcnow()),))
            return cur.rowcount

    def retry_failed(self, post_id: str) -> int:
        now = iso(utcnow())
        with self.db.connect() as conn:
            cur = conn.execute(
                "UPDATE post_targets SET status = 'pending', attempts = 0, error = NULL, next_attempt_at = ?,"
                " updated_at = ? WHERE post_id = ? AND status = 'failed'",
                (now, now, post_id),
            )
            return cur.rowcount

    def update_post(self, post_id: str, *, title: str | None = None, description: str | None = None,
                    tags: list[str] | None = None, scheduled_at: datetime | None = None,
                    targets: list[dict[str, Any]] | None = None) -> None:
        """Change a post that has not finished going out.

        Text applies to the uploads still waiting. `scheduled_at` moves every waiting target to one
        time. `targets` ({platform, account_id, scheduled_at}) is the wanted set of targets: new ones
        are added, waiting or failed ones that are not listed are dropped, and a listed one with a
        time is moved to it. A target that is uploading or already posted is never touched.
        Raises KeyError / PostBusy."""
        now = iso(utcnow())
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
                if post is None:
                    raise KeyError(post_id)
                existing = {_key(t): t for t in
                            conn.execute("SELECT * FROM post_targets WHERE post_id = ?", (post_id,))}
                open_keys = [k for k, t in existing.items() if t["status"] in OPEN]
                adds_new = bool(targets) and any(_key(t) not in existing for t in targets)
                if not open_keys and not adds_new:
                    raise PostBusy("this post has already gone out everywhere; nothing is left to change")
                conn.execute("UPDATE posts SET title = ?, description = ?, tags = ? WHERE id = ?",
                             (title if title is not None else post["title"],
                              description if description is not None else post["description"],
                              json.dumps(tags) if tags is not None else post["tags"], post_id))
                if scheduled_at is not None:
                    when = iso(scheduled_at)
                    conn.execute("UPDATE post_targets SET scheduled_at = ?, next_attempt_at = ?, updated_at = ?"
                                 " WHERE post_id = ? AND status = 'pending'", (when, when, now, post_id))
                if targets is not None:
                    wanted = {_key(t): t for t in targets}
                    for k in open_keys:
                        if k not in wanted:
                            conn.execute("DELETE FROM post_targets WHERE id = ?", (existing[k]["id"],))
                    for k, t in wanted.items():
                        when = iso(t["scheduled_at"]) if t.get("scheduled_at") else None
                        if k not in existing:
                            when = when or (iso(scheduled_at) if scheduled_at else post["scheduled_at"])
                            conn.execute(
                                "INSERT INTO post_targets (post_id, platform, account_id, scheduled_at,"
                                " next_attempt_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (post_id, t["platform"], t["account_id"], when, when, now))
                        elif when and existing[k]["status"] == "pending":
                            conn.execute("UPDATE post_targets SET scheduled_at = ?, next_attempt_at = ?, updated_at = ?"
                                         " WHERE id = ?", (when, when, now, existing[k]["id"]))
                    if not conn.execute("SELECT 1 FROM post_targets WHERE post_id = ?", (post_id,)).fetchone():
                        raise PostBusy("a post needs at least one target; cancel it instead")
                first = conn.execute("SELECT MIN(scheduled_at) FROM post_targets WHERE post_id = ?",
                                     (post_id,)).fetchone()[0]
                conn.execute("UPDATE posts SET scheduled_at = ? WHERE id = ?", (first, post_id))
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def cancel_post(self, post_id: str, conn: sqlite3.Connection) -> dict[str, Any]:
        """Inside the caller's transaction: drop every upload that has not happened yet. If nothing was
        posted the post itself goes too. Nothing is ever taken down from a platform."""
        post = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if post is None:
            raise KeyError(post_id)
        targets = conn.execute("SELECT * FROM post_targets WHERE post_id = ? ORDER BY id", (post_id,)).fetchall()
        if any(t["status"] == "processing" for t in targets):
            raise PostBusy("an upload for this post is in progress; try again in a minute")
        cancelled = [t["platform"] for t in targets if t["status"] in OPEN]
        kept = [t["platform"] for t in targets if t["status"] == "completed"]
        conn.execute("DELETE FROM post_targets WHERE post_id = ? AND status IN ('pending', 'failed')", (post_id,))
        if not kept:
            conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        return {"deleted": not kept, "cancelled": cancelled, "kept": kept, "clip_id": post["clip_id"],
                "file_path": post["file_path"]}

    # ---------------------------------------------------------------- reads
    def get_post(self, post_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
            if row is None:
                return None
            targets = conn.execute("SELECT * FROM post_targets WHERE post_id = ? ORDER BY id", (post_id,)).fetchall()
        return self._post(row, targets)

    def list_posts(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM posts ORDER BY scheduled_at DESC LIMIT ?", (limit * 4,)).fetchall()
            ids = [r["id"] for r in rows]
            targets: dict[str, list[sqlite3.Row]] = {i: [] for i in ids}
            if ids:
                marks = ",".join("?" * len(ids))
                for t in conn.execute(f"SELECT * FROM post_targets WHERE post_id IN ({marks}) ORDER BY id", ids):
                    targets[t["post_id"]].append(t)
        out = [self._post(r, targets[r["id"]]) for r in rows]
        if status:
            out = [p for p in out if p["status"] == status]
        return out[:limit]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["extra"] = json.loads(d.get("extra") or "{}")
        return d

    def _post(self, row: sqlite3.Row, targets: list[sqlite3.Row]) -> dict[str, Any]:
        d = self._row(row)
        d["targets"] = [dict(t) for t in targets]
        d["platforms"] = list(dict.fromkeys(t["platform"] for t in targets))
        d["status"] = derive_status([t["status"] for t in targets])
        return d
