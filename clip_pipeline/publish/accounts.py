"""Accounts: who we post as.

Each account belongs to one platform, carries its own login and has its own daily posting slots.
Credentials are stored in the database (the same trust boundary as `.env`) and are never returned
by the API or written to logs.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable

from clip_pipeline.config import Settings
from clip_pipeline.db import Database, iso, utcnow
from clip_pipeline.publish.uploaders.base import BaseUploader

PLATFORMS = ("youtube", "instagram", "tiktok")
DEFAULT_SLOTS = ("09:00", "15:00", "21:00")
# credential key -> the Settings field the platform's uploader reads
CREDENTIAL_FIELDS: dict[str, dict[str, str]] = {
    "youtube": {"client_id": "youtube_client_id", "client_secret": "youtube_client_secret",
                "refresh_token": "youtube_refresh_token", "privacy_status": "youtube_privacy_status",
                "category_id": "youtube_category_id"},
    "instagram": {"user_id": "instagram_user_id", "access_token": "instagram_access_token",
                  "graph_version": "instagram_graph_version"},
    "tiktok": {"storage_state": "tiktok_state_file", "user_data_dir": "tiktok_profile_dir"},
}
REQUIRED = {"youtube": ("client_id", "client_secret", "refresh_token"), "instagram": ("user_id", "access_token"),
            "tiktok": ()}
_SLOT_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
SLOT_LEAD = timedelta(minutes=10)   # never hand out a slot that is about to pass


class AccountError(ValueError):
    pass


class AccountInUse(RuntimeError):
    pass


def clean_slots(slots) -> list[str]:
    if not isinstance(slots, (list, tuple)) or not 1 <= len(slots) <= 24:
        raise AccountError("slots must be 1 to 24 times of day like \"09:00\"")
    out = sorted({str(s).strip() for s in slots})
    for s in out:
        if not _SLOT_RE.match(s):
            raise AccountError(f"{s!r} is not a time of day like \"09:00\"")
    return out


def local_timezone(settings: Settings) -> tzinfo:
    """TIMEZONE (an IANA name such as Europe/Istanbul) if set and known, else the machine's own."""
    if settings.timezone:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(settings.timezone)
        except Exception:  # noqa: BLE001 - unknown name or no tz database on this machine
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


class Accounts:
    def __init__(self, db: Database, settings: Settings,
                 factories: dict[str, Callable[[Settings], BaseUploader]] | None = None):
        self.db = db
        self.s = settings
        self._factories = factories
        self._uploaders: dict[tuple[str, str], BaseUploader] = {}

    # ---------------------------------------------------------------- CRUD
    def create(self, platform: str, name: str, credentials: dict[str, Any] | None = None,
               slots: list[str] | None = None) -> dict[str, Any]:
        if platform not in PLATFORMS:
            raise AccountError(f"platform must be one of {', '.join(PLATFORMS)}")
        name = (name or "").strip()
        if not name:
            raise AccountError("an account needs a name")
        creds = self._clean_credentials(platform, credentials or {})
        account_id, now = uuid.uuid4().hex, iso(utcnow())
        with self.db.connect() as conn:
            if conn.execute("SELECT 1 FROM accounts WHERE platform = ? AND name = ?", (platform, name)).fetchone():
                raise AccountError(f"there is already a {platform} account called {name!r}")
            conn.execute("INSERT INTO accounts (id, platform, name, credentials, slots, created_at, updated_at)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (account_id, platform, name, json.dumps(creds),
                          json.dumps(clean_slots(list(DEFAULT_SLOTS) if slots is None else slots)), now, now))
        return self.get(account_id)

    def get(self, account_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(account_id)
        return self._row(row)

    def list(self, platform: str | None = None, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM accounts", []
        where = []
        if platform:
            where.append("platform = ?")
            args.append(platform)
        if enabled_only:
            where.append("enabled = 1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.db.connect() as conn:
            return [self._row(r) for r in conn.execute(sql + " ORDER BY platform, created_at, rowid", args)]

    def update(self, account_id: str, *, name: str | None = None, slots: list[str] | None = None,
               enabled: bool | None = None, credentials: dict[str, Any] | None = None) -> dict[str, Any]:
        acct = self.get(account_id)
        sets: dict[str, Any] = {}
        if name is not None:
            if not name.strip():
                raise AccountError("an account needs a name")
            sets["name"] = name.strip()
        if slots is not None:
            sets["slots"] = json.dumps(clean_slots(slots))
        if enabled is not None:
            sets["enabled"] = int(bool(enabled))
        if credentials is not None:
            merged = {**acct["credentials"], **credentials}   # send only what changed
            sets["credentials"] = json.dumps(self._clean_credentials(acct["platform"], merged))
            sets["stats_note"] = ""                           # a new login may have new permissions
        if sets:
            with self.db.connect() as conn:
                dup = conn.execute("SELECT 1 FROM accounts WHERE platform = ? AND name = ? AND id != ?",
                                   (acct["platform"], sets.get("name", acct["name"]), account_id)).fetchone()
                if dup:
                    raise AccountError(f"there is already a {acct['platform']} account called {sets['name']!r}")
                conn.execute(f"UPDATE accounts SET {', '.join(k + ' = ?' for k in sets)}, updated_at = ? WHERE id = ?",
                             (*sets.values(), iso(utcnow()), account_id))
            self._uploaders = {k: v for k, v in self._uploaders.items() if k[1] != account_id}
        return self.get(account_id)

    def delete(self, account_id: str) -> None:
        self.get(account_id)
        with self.db.connect() as conn:
            busy = conn.execute("SELECT COUNT(*) FROM post_targets WHERE account_id = ?"
                                " AND status IN ('pending', 'processing')", (account_id,)).fetchone()[0]
            if busy:
                raise AccountInUse(f"{busy} scheduled post(s) still use this account; cancel or move them first")
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        self._uploaders = {k: v for k, v in self._uploaders.items() if k[1] != account_id}

    def set_stats_note(self, account_id: str, note: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE accounts SET stats_note = ? WHERE id = ?", (note[:300], account_id))

    def public(self, acct: dict[str, Any]) -> dict[str, Any]:
        """What the app may see: everything except the credentials themselves."""
        out = {k: v for k, v in acct.items() if k != "credentials"}
        out["configured"] = self.is_configured(acct)
        out["next_slot"] = iso(self.next_slot(acct["id"])) if acct["enabled"] else None
        return out

    # ---------------------------------------------------------------- logins
    def import_env(self) -> list[dict[str, Any]]:
        """First run with logins in `.env`: turn each into an account called "default", so an existing
        setup keeps posting. Does nothing for a platform that already has an account."""
        made = []
        for platform, fields in CREDENTIAL_FIELDS.items():
            if self.list(platform):
                continue
            creds = {k: str(getattr(self.s, f)) for k, f in fields.items() if getattr(self.s, f, "")}
            if platform == "tiktok":
                creds = {"storage_state": str(self.s.tiktok_storage_state)} if self.s.tiktok_storage_state.is_file() else {}
            if all(creds.get(k) for k in REQUIRED[platform]) and creds:
                made.append(self.create(platform, "default", creds))
        return made

    def settings_for(self, acct: dict[str, Any]) -> Settings:
        """The service settings with this account's login swapped in: what its uploader is built from."""
        fields = CREDENTIAL_FIELDS[acct["platform"]]
        return replace(self.s, **{fields[k]: v for k, v in acct["credentials"].items() if k in fields and v})

    def is_configured(self, acct: dict[str, Any]) -> bool:
        up = self._uploader_for(acct)
        return bool(up and up.is_configured())

    def default_for(self, platform: str) -> dict[str, Any] | None:
        accounts = self.list(platform, enabled_only=True)
        return accounts[0] if accounts else None

    def uploader(self, platform: str, account_id: str | None) -> BaseUploader | None:
        """The uploader for a target. A target without an account uses the platform's first enabled one."""
        try:
            acct = self.get(account_id) if account_id else self.default_for(platform)
        except KeyError:
            acct = None
        if acct is None or not acct["enabled"] or acct["platform"] != platform:
            return None
        return self._uploader_for(acct)

    def _uploader_for(self, acct: dict[str, Any]) -> BaseUploader | None:
        key = (acct["platform"], acct["id"])
        if key not in self._uploaders:
            factories = self._factories or _default_factories()
            factory = factories.get(acct["platform"])
            if factory is None:
                return None
            self._uploaders[key] = factory(self.settings_for(acct))
        return self._uploaders[key]

    # ---------------------------------------------------------------- slots
    def next_slot(self, account_id: str, after: datetime | None = None, taken: set[str] = frozenset()) -> datetime:
        """The account's next free posting slot, in UTC. `taken` adds times handed out but not saved yet."""
        acct = self.get(account_id)
        tz = local_timezone(self.s)
        after = (after or utcnow()) + SLOT_LEAD
        with self.db.connect() as conn:
            used = {r[0] for r in conn.execute(
                "SELECT scheduled_at FROM post_targets WHERE account_id = ? AND status IN ('pending', 'processing')",
                (account_id,))} | set(taken)
        day = after.astimezone(tz).date()
        for offset in range(0, 3660):
            for slot in acct["slots"]:
                hour, minute = map(int, slot.split(":"))
                when = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz) + timedelta(days=offset)
                when_utc = when.astimezone(timezone.utc)
                if when_utc > after and iso(when_utc) not in used:
                    return when_utc
        raise AccountError("no free slot in the next ten years")   # pragma: no cover

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _clean_credentials(platform: str, creds: dict[str, Any]) -> dict[str, str]:
        unknown = set(creds) - set(CREDENTIAL_FIELDS[platform])
        if unknown:
            raise AccountError(f"unknown {platform} credential(s): {', '.join(sorted(unknown))}")
        return {k: str(v).strip() for k, v in creds.items() if v is not None and str(v).strip()}

    @staticmethod
    def _row(row) -> dict[str, Any]:
        d = dict(row)
        d["credentials"] = json.loads(d["credentials"] or "{}")
        d["slots"] = json.loads(d["slots"] or "[]")
        d["enabled"] = bool(d["enabled"])
        return d


def _default_factories() -> dict[str, Callable[[Settings], BaseUploader]]:
    from clip_pipeline.publish.uploaders.instagram import InstagramUploader
    from clip_pipeline.publish.uploaders.tiktok import TikTokUploader
    from clip_pipeline.publish.uploaders.youtube import YouTubeUploader

    return {"youtube": YouTubeUploader, "instagram": InstagramUploader, "tiktok": TikTokUploader}
