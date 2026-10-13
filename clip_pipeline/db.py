"""The one SQLite database: connections and versioned schema migrations.

Every table lives in a single file so a source, its clips and their posts can be changed in one
transaction. `MIGRATIONS[n]` takes the schema from version n to n + 1; never edit an applied one,
append a new one.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

MIGRATIONS: list[str] = [
    # 0 -> 1: sources, clips and the post queue
    """
    CREATE TABLE sources (
        id                TEXT PRIMARY KEY,
        kind              TEXT NOT NULL,                      -- url | file
        url               TEXT NOT NULL DEFAULT '',
        file_path         TEXT,                               -- local video (kind = file)
        title             TEXT NOT NULL DEFAULT '',
        duration          REAL,
        content_type      TEXT,                               -- what the source turned out to be
        content_hint      TEXT,                               -- type the owner picked; overrides guessing
        status            TEXT NOT NULL DEFAULT 'queued',     -- queued | running | ready | failed
        stage             TEXT NOT NULL DEFAULT 'audio',      -- audio | analysis | whisper | llm | slices
        stage_progress    REAL NOT NULL DEFAULT 0,
        message           TEXT NOT NULL DEFAULT '',
        error             TEXT,
        candidate_minutes REAL,
        scoring_note      TEXT NOT NULL DEFAULT '',
        scoring_model     TEXT NOT NULL DEFAULT '',
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );
    CREATE INDEX idx_sources_status ON sources (status, created_at);

    CREATE TABLE clips (
        id                 TEXT PRIMARY KEY,
        source_id          TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        candidate_id       TEXT NOT NULL,                     -- c00, c01… names its files inside the source dir
        title              TEXT NOT NULL,
        description        TEXT NOT NULL DEFAULT '',
        tags               TEXT NOT NULL DEFAULT '[]',
        hook_title         TEXT NOT NULL DEFAULT '',
        hook_reason        TEXT NOT NULL DEFAULT '',
        virality_score     INTEGER NOT NULL DEFAULT 0,
        hook_score         INTEGER,
        engagement_score   INTEGER,
        value_score        INTEGER,
        shareability_score INTEGER,
        content_type       TEXT NOT NULL DEFAULT 'other',
        scorer             TEXT NOT NULL DEFAULT 'gemini',    -- gemini | heuristic
        start              REAL NOT NULL,
        "end"              REAL NOT NULL,
        slice_file         TEXT,                              -- paths are relative to the source dir
        slice_start        REAL,
        slice_end          REAL,
        thumb_file         TEXT,
        render_file        TEXT,
        encoder            TEXT,
        -- below_threshold | candidate | slicing | review | rendering | scheduled | rejected | failed
        status             TEXT NOT NULL DEFAULT 'candidate',
        error              TEXT,
        post_id            TEXT,
        created_at         TEXT NOT NULL,
        updated_at         TEXT NOT NULL,
        UNIQUE (source_id, candidate_id)
    );
    CREATE INDEX idx_clips_status ON clips (status, virality_score);

    CREATE TABLE posts (
        id           TEXT PRIMARY KEY,
        clip_id      TEXT REFERENCES clips(id) ON DELETE SET NULL,
        title        TEXT NOT NULL,
        description  TEXT NOT NULL DEFAULT '',
        tags         TEXT NOT NULL DEFAULT '[]',
        file_path    TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        extra        TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX idx_posts_sched ON posts (scheduled_at);

    CREATE TABLE post_targets (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id         TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        platform        TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'pending',      -- pending | processing | completed | failed
        attempts        INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        remote_id       TEXT,
        remote_url      TEXT,
        error           TEXT,
        updated_at      TEXT NOT NULL,
        UNIQUE (post_id, platform)
    );
    CREATE INDEX idx_targets_due ON post_targets (status, next_attempt_at);
    """,
    # 1 -> 2: recognising a source we have already seen, and knowing which files are ours to delete
    """
    ALTER TABLE sources ADD COLUMN fingerprint TEXT;                      -- same video => same fingerprint
    ALTER TABLE sources ADD COLUMN owns_file INTEGER NOT NULL DEFAULT 0;  -- 1 = taken from the inbox/an upload
    ALTER TABLE sources ADD COLUMN cleaned_at TEXT;                       -- when its big files were removed
    CREATE INDEX idx_sources_fingerprint ON sources (fingerprint);
    """,
    # 2 -> 3: how each clip is rendered (it is rendered before review), and owner-adjustable preferences
    """
    ALTER TABLE clips ADD COLUMN aspect TEXT NOT NULL DEFAULT '9:16';
    ALTER TABLE clips ADD COLUMN layout TEXT NOT NULL DEFAULT 'auto';
    ALTER TABLE clips ADD COLUMN subtitle_style TEXT NOT NULL DEFAULT 'karaoke';
    CREATE TABLE prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
    # 3 -> 4: several accounts per platform, a time per target, events, and reach statistics
    """
    CREATE TABLE accounts (
        id          TEXT PRIMARY KEY,
        platform    TEXT NOT NULL,                             -- youtube | instagram | tiktok
        name        TEXT NOT NULL,                             -- what the owner calls it
        credentials TEXT NOT NULL DEFAULT '{}',                -- JSON; never leaves the server
        slots       TEXT NOT NULL DEFAULT '["09:00", "15:00", "21:00"]',   -- daily posting times, local
        enabled     INTEGER NOT NULL DEFAULT 1,
        stats_note  TEXT NOT NULL DEFAULT '',                  -- why reach numbers can't be read, if they can't
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        UNIQUE (platform, name)
    );

    -- post_targets is rebuilt: each target now has its own time and account, and one post may go to
    -- two accounts on the same platform (the old UNIQUE (post_id, platform) forbade that).
    CREATE TABLE post_targets_v4 (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id          TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        platform         TEXT NOT NULL,
        account_id       TEXT REFERENCES accounts(id) ON DELETE SET NULL,   -- NULL = the platform's first account
        scheduled_at     TEXT NOT NULL,
        status           TEXT NOT NULL DEFAULT 'pending',      -- pending | processing | completed | failed
        attempts         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at  TEXT NOT NULL,
        remote_id        TEXT,
        remote_url       TEXT,
        error            TEXT,
        updated_at       TEXT NOT NULL,
        posted_at        TEXT,
        stats_checked_at TEXT
    );
    INSERT INTO post_targets_v4 (id, post_id, platform, scheduled_at, status, attempts, next_attempt_at,
                                 remote_id, remote_url, error, updated_at)
        SELECT t.id, t.post_id, t.platform, p.scheduled_at, t.status, t.attempts, t.next_attempt_at,
               t.remote_id, t.remote_url, t.error, t.updated_at
        FROM post_targets t JOIN posts p ON p.id = t.post_id;
    DROP TABLE post_targets;
    ALTER TABLE post_targets_v4 RENAME TO post_targets;
    CREATE UNIQUE INDEX idx_targets_unique ON post_targets (post_id, platform, COALESCE(account_id, ''));
    CREATE INDEX idx_targets_due ON post_targets (status, next_attempt_at);
    CREATE INDEX idx_targets_account ON post_targets (account_id, scheduled_at);

    CREATE TABLE events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        kind       TEXT NOT NULL,           -- clips_ready | source_failed | upload_done | upload_failed
        title      TEXT NOT NULL,
        body       TEXT NOT NULL DEFAULT '',
        data       TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL
    );

    CREATE TABLE target_stats (               -- one reading of a posted clip's numbers
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id INTEGER NOT NULL REFERENCES post_targets(id) ON DELETE CASCADE,
        taken_at  TEXT NOT NULL,
        views     INTEGER,
        likes     INTEGER,
        comments  INTEGER,
        shares    INTEGER
    );
    CREATE INDEX idx_target_stats ON target_stats (target_id, taken_at);

    CREATE TABLE account_stats (              -- one reading of an account's follower count
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        taken_at   TEXT NOT NULL,
        followers  INTEGER
    );
    CREATE INDEX idx_account_stats ON account_stats (account_id, taken_at);
    """,
    # 4 -> 5: paired devices. Only public keys and hashes of one-time codes: nothing here is a secret.
    """
    CREATE TABLE devices (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        public_key   TEXT NOT NULL,          -- base64 DER, ECDSA P-256
        created_at   TEXT NOT NULL,
        last_seen_at TEXT,
        revoked_at   TEXT
    );
    CREATE TABLE pairing_codes (
        code_hash  TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        used_at    TEXT
    );
    """,
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Fixed-width UTC format so string comparison == chronological comparison."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Autocommit connection; wrap multi-statement changes in BEGIN IMMEDIATE … COMMIT."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
        finally:
            conn.close()

    def version(self) -> int:
        with self.connect() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> int:
        """Apply every migration the file hasn't seen yet. Returns the resulting schema version."""
        with self.connect() as conn:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > len(MIGRATIONS):
                raise RuntimeError(f"{self.path} has schema v{current}, newer than this code (v{len(MIGRATIONS)})")
            for n in range(current, len(MIGRATIONS)):
                # executescript commits first, then runs the batch; BEGIN/COMMIT inside keep it atomic.
                conn.executescript(f"BEGIN;\n{MIGRATIONS[n]}\nPRAGMA user_version = {n + 1};\nCOMMIT;")
            return len(MIGRATIONS)
