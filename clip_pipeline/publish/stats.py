"""Reach: reading how posted clips are doing, and turning the readings into insights.

Every posted clip is read a few times a day while it is fresh and less often as it ages, and each
reading is kept. That history is what shows whether things are going up or down.

YouTube and Instagram report numbers through their official APIs. TikTok clips are posted through
the browser, which gives us no video id to look up, so TikTok has no numbers yet.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable

import httpx

from clip_pipeline.config import Settings
from clip_pipeline.db import Database, iso, utcnow
from clip_pipeline.publish.accounts import Accounts, local_timezone

log = logging.getLogger(__name__)

TOKEN_URI = "https://oauth2.googleapis.com/token"
METRICS = ("views", "likes", "comments", "shares")
FOLLOWER_INTERVAL = timedelta(hours=24)


@dataclass
class Reading:
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None


class StatsUnavailable(Exception):
    """This account's login is not allowed to read numbers. The message says what to do about it.
    `readings` carries whatever could still be read (e.g. likes and comments without view counts)."""

    def __init__(self, message: str, readings: "dict[str, Reading] | None" = None):
        super().__init__(message)
        self.readings = readings or {}


def reading_interval(age: timedelta) -> timedelta:
    """How long to wait between readings of a clip that was posted `age` ago."""
    if age < timedelta(days=2):
        return timedelta(hours=3)
    if age < timedelta(days=14):
        return timedelta(hours=12)
    if age < timedelta(days=90):
        return timedelta(days=1)
    return timedelta(days=7)


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ============================================================================ YouTube

def _youtube_service(s: Settings):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    # No scopes passed: the token is refreshed with whatever the login granted (upload-only or more).
    creds = Credentials(token=None, refresh_token=s.youtube_refresh_token, token_uri=TOKEN_URI,
                        client_id=s.youtube_client_id, client_secret=s.youtube_client_secret)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _youtube_call(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - googleapiclient raises HttpError, auth raises RefreshError
        text = str(exc)
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in (401, 403) or "insufficient" in text.lower() or "invalid_scope" in text.lower():
            raise StatsUnavailable("this YouTube login can upload but not read view counts; "
                                   "log in again to allow reading them") from exc
        raise


def youtube_readings(s: Settings, video_ids: list[str], service=None) -> dict[str, Reading]:
    service = service or _youtube_service(s)
    out: dict[str, Reading] = {}
    for i in range(0, len(video_ids), 50):      # the API takes 50 ids per call, for one quota unit
        batch = video_ids[i:i + 50]
        resp = _youtube_call(lambda: service.videos().list(part="statistics", id=",".join(batch)).execute())
        for item in resp.get("items", []):
            st = item.get("statistics", {})
            out[item["id"]] = Reading(_int(st.get("viewCount")), _int(st.get("likeCount")),
                                      _int(st.get("commentCount")), None)
    return out


def youtube_followers(s: Settings, service=None) -> int | None:
    service = service or _youtube_service(s)
    resp = _youtube_call(lambda: service.channels().list(part="statistics", mine=True).execute())
    items = resp.get("items") or []
    return _int(items[0].get("statistics", {}).get("subscriberCount")) if items else None


# ============================================================================ Instagram

def _ig_get(client: httpx.Client, s: Settings, path: str, params: dict) -> dict:
    resp = client.get(f"https://graph.facebook.com/{s.instagram_graph_version}/{path}", params=params,
                      headers={"Authorization": f"OAuth {s.instagram_access_token}"})
    try:
        data = resp.json()
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data, dict) else None
    if err:
        if err.get("code") in (10, 200) or err.get("type") == "OAuthException" and "permission" in str(err).lower():
            raise StatsUnavailable("this Instagram login is not allowed to read insights; "
                                   "add the insights permission to its token")
        raise httpx.HTTPError(f"Instagram {path}: {err.get('message')}")
    resp.raise_for_status()
    return data


def instagram_readings(s: Settings, media_ids: list[str], client: httpx.Client | None = None) -> dict[str, Reading]:
    own = client is None
    client = client or httpx.Client(timeout=30)
    out: dict[str, Reading] = {}
    denied: StatsUnavailable | None = None
    try:
        for media_id in media_ids:
            reading = Reading()
            if denied is None:
                try:
                    data = _ig_get(client, s, f"{media_id}/insights", {"metric": "views,likes,comments,shares"})
                    values = {m["name"]: (m.get("values") or [{}])[0].get("value") for m in data.get("data", [])}
                    reading = Reading(*(_int(values.get(k)) for k in METRICS))
                except StatsUnavailable as exc:
                    denied = exc            # no insights permission: don't ask again for the other clips
                except httpx.HTTPError as exc:
                    log.info("Instagram insights for %s unavailable (%s); falling back to public counts", media_id, exc)
            if reading.likes is None:       # likes and comments are readable without the insights permission
                try:
                    data = _ig_get(client, s, media_id, {"fields": "like_count,comments_count"})
                    reading.likes, reading.comments = _int(data.get("like_count")), _int(data.get("comments_count"))
                except (httpx.HTTPError, StatsUnavailable) as exc:
                    log.info("Instagram counts for %s unavailable (%s)", media_id, exc)
                    continue
            out[media_id] = reading
    finally:
        if own:
            client.close()
    if denied is not None:
        raise StatsUnavailable("this Instagram login can read likes and comments but not views; "
                               "add the insights permission to its token", out)
    return out


def instagram_followers(s: Settings, client: httpx.Client | None = None) -> int | None:
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        return _int(_ig_get(client, s, s.instagram_user_id, {"fields": "followers_count"}).get("followers_count"))
    finally:
        if own:
            client.close()


READERS: dict[str, tuple[Callable, Callable]] = {
    "youtube": (youtube_readings, youtube_followers),
    "instagram": (instagram_readings, instagram_followers),
}
NO_NUMBERS = {"tiktok": "TikTok does not report numbers for clips posted this way yet"}


# ============================================================================ collecting

class StatsCollector:
    def __init__(self, db: Database, accounts: Accounts, readers: dict[str, tuple[Callable, Callable]] | None = None,
                 now: Callable[[], datetime] = utcnow):
        self.db, self.accounts, self._now = db, accounts, now
        self.readers = READERS if readers is None else readers

    def due_targets(self) -> list[dict[str, Any]]:
        now = self._now()
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, platform, account_id, remote_id, posted_at, updated_at, stats_checked_at"
                " FROM post_targets WHERE status = 'completed' AND remote_id IS NOT NULL")]
        due = []
        for r in rows:
            posted = _parse(r["posted_at"] or r["updated_at"])
            checked = _parse(r["stats_checked_at"]) if r["stats_checked_at"] else None
            if checked is None or now - checked >= reading_interval(now - posted):
                due.append(r)
        return due

    def collect(self) -> dict[str, int]:
        """Read every clip that is due. One account's failure never stops the others."""
        counts = {"readings": 0, "followers": 0, "skipped": 0}
        by_account: dict[str, list[dict[str, Any]]] = {}
        for t in self.due_targets():
            acct = self._account(t)
            if acct is None:
                counts["skipped"] += 1
                continue
            by_account.setdefault(acct["id"], []).append(t)
        stamp = iso(self._now())
        for account_id, targets in by_account.items():
            acct = self.accounts.get(account_id)
            reader = self.readers.get(acct["platform"])
            readings: dict[str, Reading] = {}
            if reader is None:
                self._note(acct, NO_NUMBERS.get(acct["platform"], "no numbers for this platform"))
            else:
                try:
                    readings = reader[0](self.accounts.settings_for(acct), [t["remote_id"] for t in targets])
                    self._note(acct, "")
                except StatsUnavailable as exc:
                    readings = exc.readings      # whatever the login was still allowed to read
                    self._note(acct, str(exc))
                except Exception:  # noqa: BLE001 - a network blip; these targets are simply read next time
                    log.exception("Reading %s numbers for account %s failed", acct["platform"], acct["name"])
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for t in targets:
                    r = readings.get(t["remote_id"])
                    if r is not None:
                        conn.execute("INSERT INTO target_stats (target_id, taken_at, views, likes, comments, shares)"
                                     " VALUES (?, ?, ?, ?, ?, ?)", (t["id"], stamp, r.views, r.likes, r.comments, r.shares))
                        counts["readings"] += 1
                    else:
                        counts["skipped"] += 1
                    # stamped either way, so a login without permission is not asked again every tick
                    conn.execute("UPDATE post_targets SET stats_checked_at = ? WHERE id = ?", (stamp, t["id"]))
                conn.execute("COMMIT")
        counts["followers"] = self.collect_followers()
        return counts

    def collect_followers(self) -> int:
        now, done = self._now(), 0
        for acct in self.accounts.list(enabled_only=True):
            reader = self.readers.get(acct["platform"])
            if reader is None or not self.accounts.is_configured(acct):
                continue
            with self.db.connect() as conn:
                last = conn.execute("SELECT MAX(taken_at) FROM account_stats WHERE account_id = ?",
                                    (acct["id"],)).fetchone()[0]
            if last and now - _parse(last) < FOLLOWER_INTERVAL:
                continue
            try:
                followers = reader[1](self.accounts.settings_for(acct))
            except StatsUnavailable as exc:
                self._note(acct, str(exc))
                followers = None
            except Exception:  # noqa: BLE001
                log.exception("Reading %s followers for account %s failed", acct["platform"], acct["name"])
                continue
            with self.db.connect() as conn:   # stored even when None, so a refused login is asked once a day
                conn.execute("INSERT INTO account_stats (account_id, taken_at, followers) VALUES (?, ?, ?)",
                             (acct["id"], iso(now), followers))
            done += followers is not None
        return done

    def _account(self, target: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return self.accounts.get(target["account_id"]) if target["account_id"] \
                else self.accounts.default_for(target["platform"])
        except KeyError:
            return None

    def _note(self, acct: dict[str, Any], note: str):
        if acct.get("stats_note", "") != note:
            self.accounts.set_stats_note(acct["id"], note)


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ============================================================================ insights

def insights(db: Database, settings: Settings, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """Everything the app's Insights screen draws, for the last `days` days."""
    now = now or utcnow()
    tz = local_timezone(settings)
    with db.connect() as conn:
        targets = [dict(r) for r in conn.execute(
            "SELECT t.id, t.platform, t.account_id, t.posted_at, t.remote_url, a.name AS account_name,"
            "       p.title, p.clip_id, c.virality_score, c.content_type, c.layout, c.hook_title, c.start, c.\"end\""
            " FROM post_targets t JOIN posts p ON p.id = t.post_id"
            " LEFT JOIN clips c ON c.id = p.clip_id LEFT JOIN accounts a ON a.id = t.account_id"
            " WHERE t.status = 'completed'")]
        readings: dict[int, list[tuple[datetime, dict[str, int | None]]]] = {}
        for r in conn.execute("SELECT target_id, taken_at, views, likes, comments, shares FROM target_stats ORDER BY taken_at, id"):
            readings.setdefault(r["target_id"], []).append((_parse(r["taken_at"]), {m: r[m] for m in METRICS}))
        followers_rows = [dict(r) for r in conn.execute(
            "SELECT s.account_id, s.taken_at, s.followers, a.name, a.platform FROM account_stats s"
            " JOIN accounts a ON a.id = s.account_id WHERE s.followers IS NOT NULL ORDER BY s.taken_at, s.id")]
        notes = [{"account": r["name"], "platform": r["platform"], "note": r["stats_note"]} for r in
                 conn.execute("SELECT name, platform, stats_note FROM accounts WHERE stats_note != ''")]

    def latest(tid: int, at: datetime | None = None) -> dict[str, int | None]:
        last: dict[str, int | None] = dict.fromkeys(METRICS)
        for taken, values in readings.get(tid, []):
            if at is not None and taken > at:
                break
            last = values
        return last

    def total(at: datetime | None = None, metric: str = "views") -> int:
        return sum(latest(t["id"], at)[metric] or 0 for t in targets)

    # ---- timeline: total views at the end of each local day, and how much that day added
    today = now.astimezone(tz).date()
    timeline, prev_total = [], None
    for back in range(days, -1, -1):
        day = today - timedelta(days=back)
        end = min(datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1), now.astimezone(tz))
        views = total(end.astimezone(timezone.utc))
        if prev_total is not None:
            timeline.append({"date": day.isoformat(), "views": views, "gained": views - prev_total})
        prev_total = views

    def gained(start_back: int, end_back: int) -> int:
        """Views added between `start_back` and `end_back` days ago."""
        return total(now - timedelta(days=end_back)) - total(now - timedelta(days=start_back))

    this_week, last_week = gained(7, 0), gained(14, 7)
    trend = {"this_week": this_week, "last_week": last_week,
             "change_pct": round(100 * (this_week - last_week) / last_week, 1) if last_week else None,
             "direction": "up" if this_week > last_week else "down" if this_week < last_week else "flat"}

    for t in targets:
        t["now"] = latest(t["id"])

    def group(key_fn: Callable[[dict], Any]) -> list[dict[str, Any]]:
        buckets: dict[Any, list[dict]] = {}
        for t in targets:
            key = key_fn(t)
            if key is not None:
                buckets.setdefault(key, []).append(t)
        rows = []
        for key, items in buckets.items():
            measured = [i["now"]["views"] for i in items if i["now"]["views"] is not None]
            rows.append({"key": key, "posts": len(items), "views": sum(measured),
                         "avg_views": round(sum(measured) / len(measured)) if measured else None})
        return sorted(rows, key=lambda r: (-(r["avg_views"] or -1), str(r["key"])))

    def score_bucket(t):
        s = t["virality_score"]
        return None if s is None else "90-100" if s >= 90 else "80-89" if s >= 80 else "70-79" if s >= 70 else "under 70"

    def length_bucket(t):
        if t["start"] is None:
            return None
        length = t["end"] - t["start"]
        return "under 30 s" if length < 30 else "30-45 s" if length < 45 else "45-60 s" if length < 60 else "over 60 s"

    def hour_bucket(t):
        return f"{_parse(t['posted_at']).astimezone(tz).hour:02d}:00" if t["posted_at"] else None

    followers: dict[str, dict[str, Any]] = {}
    cutoff = now - timedelta(days=days)
    for r in followers_rows:
        f = followers.setdefault(r["account_id"], {"account": r["name"], "platform": r["platform"], "start": None})
        if f["start"] is None or _parse(r["taken_at"]) <= cutoff:
            f["start"] = r["followers"]
        f["followers"] = r["followers"]
    for f in followers.values():
        f["change"] = f["followers"] - f.pop("start")

    top = sorted((t for t in targets if t["now"]["views"] is not None), key=lambda t: -t["now"]["views"])
    return {
        "days": days,
        "totals": {**{m: total(metric=m) for m in METRICS}, "posts": len(targets),
                   "measured_posts": sum(bool(readings.get(t["id"])) for t in targets)},
        "trend": trend,
        "timeline": timeline,
        "by_platform": group(lambda t: t["platform"]),
        "by_account": group(lambda t: t["account_name"] or f"{t['platform']} (default)"),
        "followers": list(followers.values()),
        "top_clips": [{"title": t["title"], "platform": t["platform"], "account": t["account_name"],
                       "url": t["remote_url"], "posted_at": t["posted_at"], "score": t["virality_score"], **t["now"]}
                      for t in top[:10]],
        # what seems to work: average views per post, grouped by how the clip was made
        "what_works": {
            "content_type": group(lambda t: t["content_type"]),
            "ai_score": group(score_bucket),
            "length": group(length_bucket),
            "framing": group(lambda t: t["layout"]),
            "hook_title": group(lambda t: None if t["hook_title"] is None else "with title" if t["hook_title"] else "no title"),
            "posting_hour": group(hour_bucket),
        },
        "not_measured": notes,
    }
