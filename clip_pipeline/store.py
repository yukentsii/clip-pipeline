"""Sources and clips: the typed read/write layer over the database.

A source is one long video (a link or a local file). A clip is one scored moment inside it,
tracked from candidate through review to a scheduled post.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from clip_pipeline.db import Database, iso, utcnow

SOURCE_STAGES = ("audio", "analysis", "whisper", "llm", "slices", "render")
# What a restart finds mid-flight, and the state each goes back to so the work is simply done again.
INTERRUPTED_CLIP_STATES = {"slicing": "candidate", "rendering": "sliced"}


@dataclass
class Source:
    id: str
    kind: str                          # url | file
    created_at: str
    updated_at: str
    url: str = ""
    file_path: str | None = None
    title: str = ""
    duration: float | None = None
    content_type: str | None = None
    content_hint: str | None = None
    status: str = "queued"             # queued | running | ready | failed
    stage: str = "audio"
    stage_progress: float = 0.0
    message: str = ""
    error: str | None = None
    candidate_minutes: float | None = None
    scoring_note: str = ""
    scoring_model: str = ""
    fingerprint: str | None = None     # identifies the video itself, so the same one is not processed twice
    owns_file: bool = False            # the file came through the inbox / an upload: ours to delete when done
    cleaned_at: str | None = None      # set once the source file and downloaded audio have been removed


@dataclass
class Clip:
    id: str
    source_id: str
    candidate_id: str
    title: str
    start: float
    end: float
    created_at: str
    updated_at: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    hook_title: str = ""
    hook_reason: str = ""
    virality_score: int = 0
    hook_score: int | None = None
    engagement_score: int | None = None
    value_score: int | None = None
    shareability_score: int | None = None
    content_type: str = "other"
    scorer: str = "gemini"             # gemini | heuristic
    slice_file: str | None = None      # relative to the source dir
    slice_start: float | None = None
    slice_end: float | None = None
    thumb_file: str | None = None
    render_file: str | None = None
    encoder: str | None = None
    aspect: str = "9:16"               # how it is rendered: 9:16 | 1:1 | 16:9
    layout: str = "auto"               # framing: auto | crop | fit
    subtitle_style: str = "karaoke"    # karaoke | static | none
    # below_threshold | duplicate          never cut
    # candidate -> slicing -> sliced -> rendering -> review -> scheduled -> posted | rejected
    # failed = needs a retry
    status: str = "candidate"
    error: str | None = None
    post_id: str | None = None


# A clip in one of these still needs something from its source (a cut, a decision, a retry).
UNDECIDED_CLIP_STATES = ("candidate", "slicing", "sliced", "rendering", "review", "failed")

_SOURCE_COLS = {f.name for f in fields(Source)} - {"id", "created_at", "updated_at"}
_CLIP_COLS = {f.name for f in fields(Clip)} - {"id", "source_id", "candidate_id", "created_at", "updated_at"}


def _q(col: str) -> str:
    return '"end"' if col == "end" else col


class Store:
    def __init__(self, db: Database, sources_dir: Path):
        self.db = db
        self.sources_dir = sources_dir
        self.sources_dir.mkdir(parents=True, exist_ok=True)

    def source_dir(self, source_id: str) -> Path:
        """Where a source's working files live (audio, analysis, slices, renders)."""
        if not source_id.isalnum():
            raise KeyError(source_id)
        return self.sources_dir / source_id

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    # ---------------------------------------------------------------- sources
    def create_source(self, *, url: str = "", file_path: Path | str | None = None, title: str = "",
                      content_hint: str | None = None, fingerprint: str | None = None,
                      owns_file: bool = False, source_id: str | None = None) -> Source:
        if bool(url) == bool(file_path):
            raise ValueError("a source is either a url or a file")
        now = iso(utcnow())
        src = Source(id=source_id or self.new_id(), kind="file" if file_path else "url", url=url,
                     file_path=str(file_path) if file_path else None, title=title, content_hint=content_hint,
                     fingerprint=fingerprint, owns_file=owns_file, created_at=now, updated_at=now)
        cols = [f.name for f in fields(Source)]
        with self.db.connect() as conn:
            conn.execute(f"INSERT INTO sources ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [getattr(src, c) for c in cols])
        return src

    def get_source(self, source_id: str) -> Source:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row is None:
            raise KeyError(source_id)
        return self._source(row)

    def list_sources(self, status: str | tuple[str, ...] | None = None, limit: int = 100) -> list[Source]:
        sql, args = "SELECT * FROM sources", []
        if status:
            wanted = (status,) if isinstance(status, str) else tuple(status)
            sql += f" WHERE status IN ({', '.join('?' * len(wanted))})"
            args += wanted
        with self.db.connect() as conn:
            rows = conn.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._source(r) for r in rows]

    def update_source(self, source_id: str, **changes: Any) -> None:
        self._update("sources", _SOURCE_COLS, source_id, changes)

    def find_by_fingerprint(self, fingerprint: str | None) -> Source | None:
        """The newest source for the same video that did not fail (a failed one may be tried again)."""
        if not fingerprint:
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE fingerprint = ? AND status != 'failed'"
                               " ORDER BY created_at DESC, rowid DESC LIMIT 1", (fingerprint,)).fetchone()
        return self._source(row) if row else None

    def delete_source(self, source_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))

    def undecided_clips(self, source_id: str) -> int:
        marks = ", ".join("?" * len(UNDECIDED_CLIP_STATES))
        with self.db.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM clips WHERE source_id = ? AND status IN ({marks})",
                                (source_id, *UNDECIDED_CLIP_STATES)).fetchone()[0]

    def next_queued_source(self) -> Source | None:
        """The oldest source still waiting for (or in the middle of) processing."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE status IN ('queued', 'running')"
                               " ORDER BY created_at, rowid LIMIT 1").fetchone()
        return self._source(row) if row else None

    # ---------------------------------------------------------------- clips
    def add_clips(self, source_id: str, clips: list[dict[str, Any]]) -> list[Clip]:
        """Insert a source's scored clips in one transaction (all or none)."""
        now = iso(utcnow())
        out = [Clip(**{"id": uuid.uuid4().hex, "source_id": source_id, "created_at": now, "updated_at": now, **c})
               for c in clips]
        cols = [f.name for f in fields(Clip)]
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                f"INSERT INTO clips ({', '.join(map(_q, cols))}) VALUES ({', '.join('?' * len(cols))})",
                [[json.dumps(c.tags) if col == "tags" else getattr(c, col) for col in cols] for c in out])
            conn.execute("COMMIT")
        return out

    def get_clip(self, clip_id: str) -> Clip:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if row is None:
            raise KeyError(clip_id)
        return self._clip(row)

    def list_clips(self, *, source_id: str | None = None, status: str | tuple[str, ...] | None = None,
                   limit: int = 500) -> list[Clip]:
        where, args = [], []
        if source_id:
            where.append("source_id = ?")
            args.append(source_id)
        if status:
            wanted = (status,) if isinstance(status, str) else tuple(status)
            where.append(f"status IN ({', '.join('?' * len(wanted))})")
            args += wanted
        sql = "SELECT * FROM clips" + (" WHERE " + " AND ".join(where) if where else "")
        with self.db.connect() as conn:
            rows = conn.execute(sql + " ORDER BY virality_score DESC, candidate_id LIMIT ?", (*args, limit)).fetchall()
        return [self._clip(r) for r in rows]

    def update_clip(self, clip_id: str, *, conn: sqlite3.Connection | None = None, **changes: Any) -> None:
        if "tags" in changes:
            changes["tags"] = json.dumps(changes["tags"])
        self._update("clips", _CLIP_COLS, clip_id, changes, conn)

    def claim_clip(self, clip_id: str, from_status: tuple[str, ...], to_status: str) -> bool:
        """Move a clip to `to_status` only if it is still in one of `from_status`. False = someone got there first."""
        marks = ", ".join("?" * len(from_status))
        with self.db.connect() as conn:
            cur = conn.execute(f"UPDATE clips SET status = ?, error = NULL, updated_at = ? WHERE id = ? AND status IN ({marks})",
                               (to_status, iso(utcnow()), clip_id, *from_status))
            return cur.rowcount == 1

    # ---------------------------------------------------------------- preferences
    def get_prefs(self) -> dict[str, Any]:
        """Owner-adjustable settings saved in the database (they override the .env defaults)."""
        with self.db.connect() as conn:
            return {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM prefs")}

    def set_prefs(self, values: dict[str, Any]) -> None:
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany("INSERT INTO prefs (key, value) VALUES (?, ?)"
                             " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                             [(k, json.dumps(v)) for k, v in values.items()])
            conn.execute("COMMIT")

    # ---------------------------------------------------------------- events
    def add_event(self, kind: str, title: str, body: str = "", data: dict[str, Any] | None = None) -> int:
        """Record something the owner should hear about. The app reads these (and later gets them pushed)."""
        with self.db.connect() as conn:
            cur = conn.execute("INSERT INTO events (kind, title, body, data, created_at) VALUES (?, ?, ?, ?, ?)",
                               (kind, title[:200], body[:1000], json.dumps(data or {}), iso(utcnow())))
            return cur.lastrowid

    def list_events(self, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Events with an id greater than `after`, oldest first: pass the last id you saw to get only news."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, limit)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    def latest_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    # ---------------------------------------------------------------- restart
    def recover_interrupted(self) -> dict[str, int]:
        """After a restart nothing is actually running. Put half-done work back where it will be picked
        up again: sources resume from their saved stage, clips go back one step."""
        now = iso(utcnow())
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sources = conn.execute("UPDATE sources SET status = 'queued', message = 'Resuming after restart',"
                                   " updated_at = ? WHERE status = 'running'", (now,)).rowcount
            clips = 0
            for stuck, back_to in INTERRUPTED_CLIP_STATES.items():
                clips += conn.execute("UPDATE clips SET status = ?, updated_at = ? WHERE status = ?",
                                      (back_to, now, stuck)).rowcount
            conn.execute("COMMIT")
        return {"sources": sources, "clips": clips}

    # ---------------------------------------------------------------- helpers
    def _update(self, table: str, allowed: set[str], row_id: str, changes: dict[str, Any],
                conn: sqlite3.Connection | None = None) -> None:
        unknown = set(changes) - allowed
        if unknown:
            raise KeyError(f"not updatable on {table}: {sorted(unknown)}")
        if not changes:
            return
        sets = ", ".join(f"{_q(k)} = ?" for k in changes)
        sql = f"UPDATE {table} SET {sets}, updated_at = ? WHERE id = ?"
        args = (*changes.values(), iso(utcnow()), row_id)
        if conn is not None:
            conn.execute(sql, args)
            return
        with self.db.connect() as c:
            c.execute(sql, args)

    @staticmethod
    def _source(row: sqlite3.Row) -> Source:
        d = dict(row)
        d["owns_file"] = bool(d["owns_file"])
        return Source(**d)

    @staticmethod
    def _clip(row: sqlite3.Row) -> Clip:
        d = dict(row)
        d["tags"] = json.loads(d.get("tags") or "[]")
        return Clip(**d)
