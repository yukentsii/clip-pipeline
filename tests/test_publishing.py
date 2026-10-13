"""Step 4 below the API: accounts and slots, publishing per account, clean-up, events, reach."""
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from clip_pipeline import db as dbmod
from clip_pipeline.config import get_settings
from clip_pipeline.db import Database, iso
from clip_pipeline.publish import stats
from clip_pipeline.publish.accounts import AccountError, Accounts, local_timezone
from clip_pipeline.publish.queue import PostQueue
from clip_pipeline.publish.scheduler import process_due
from clip_pipeline.publish.stats import Reading, StatsCollector, StatsUnavailable, reading_interval
from clip_pipeline.publish.uploaders.base import BaseUploader, UploadError, UploadResult
from clip_pipeline.store import Store
from clip_pipeline.worker import Worker

from test_worker import Stages, approve_opts  # noqa: F401

UTC = timezone.utc
T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


class RecordingUploader(BaseUploader):
    """Remembers which login it was built with and what it was asked to post."""
    posted: list[tuple[str, str, str]] = []

    def __init__(self, platform, settings, fail=None):
        self.platform, self.s, self.fail = platform, settings, fail

    def is_configured(self):
        return True

    def upload(self, job):
        login = self.s.youtube_refresh_token if self.platform == "youtube" else self.s.instagram_access_token
        if login == "broken":
            raise UploadError("login expired", retryable=False)
        RecordingUploader.posted.append((self.platform, login, job.title))
        return UploadResult(f"{self.platform}-{login}-{len(RecordingUploader.posted)}",
                            f"https://{self.platform}.test/{login}")


FACTORIES = {p: (lambda s, p=p: RecordingUploader(p, s)) for p in ("youtube", "instagram", "tiktok")}


@pytest.fixture
def stages(monkeypatch) -> Stages:
    return Stages(monkeypatch)


@pytest.fixture
def env(tmp_path):
    RecordingUploader.posted = []
    s = replace(get_settings(), data_dir=tmp_path, max_attempts=2, retry_backoff_seconds=60)
    db = Database(tmp_path / "p.db")
    accounts = Accounts(db, s, FACTORIES)
    worker = Worker(Store(db, tmp_path / "sources"), PostQueue(db), s, accounts)
    yield worker
    worker.shutdown()


def yt(worker, name, token, **kw):
    return worker.accounts.create("youtube", name, {"client_id": "i", "client_secret": "s", "refresh_token": token}, **kw)


def reviewed(worker):
    src = worker.store.create_source(url="https://youtu.be/x")
    worker.process(src)
    return worker.store.list_clips(source_id=src.id, status="review")


def tick(worker, now=None):
    return process_due(worker.queue, worker.accounts.uploader, settings=worker.s, now=now, on_result=worker.after_upload)


# ---------------------------------------------------------------- accounts and slots

def test_each_account_posts_with_its_own_login(env, stages):
    a, b = yt(env, "main", "tokenA"), yt(env, "second", "tokenB")
    ig = env.accounts.create("instagram", "brand", {"user_id": "1", "access_token": "igtok"})
    clip = reviewed(env)[0]
    env.approve(clip.id, {"targets": [{"account_id": x["id"], "scheduled_at": T0} for x in (a, b, ig)]})
    assert tick(env, now=T0) == {"completed": 3, "retrying": 0, "failed": 0}
    assert sorted(RecordingUploader.posted) == [("instagram", "igtok", "Title c00"), ("youtube", "tokenA", "Title c00"),
                                                ("youtube", "tokenB", "Title c00")]


def test_target_without_an_account_uses_the_platforms_first_enabled_one(env, stages):
    a, b = yt(env, "main", "tokenA"), yt(env, "second", "tokenB")
    clips = reviewed(env)
    env.approve(clips[0].id, {"platforms": ["youtube"], "scheduled_at": T0})
    tick(env, now=T0)
    env.accounts.update(a["id"], enabled=False)
    # the first clip's approval resolved to account "main"; a new approval now resolves to "second"
    env.approve(clips[1].id, {"platforms": ["youtube"], "scheduled_at": T0})
    tick(env, now=T0)
    assert [login for _, login, _ in RecordingUploader.posted] == ["tokenA", "tokenB"]


def test_missing_or_switched_off_account_fails_that_target_only(env, stages):
    a, b = yt(env, "main", "tokenA"), yt(env, "second", "tokenB")
    clip = reviewed(env)[0]
    pid = env.approve(clip.id, {"targets": [{"account_id": a["id"], "scheduled_at": T0},
                                            {"account_id": b["id"], "scheduled_at": T0}]})
    env.accounts.update(b["id"], enabled=False)
    assert tick(env, now=T0) == {"completed": 1, "retrying": 0, "failed": 1}
    errors = {t["account_id"]: t["error"] for t in env.queue.get_post(pid)["targets"]}
    assert errors[a["id"]] is None and "no usable youtube account" in errors[b["id"]]


def test_slots_follow_the_accounts_times_and_skip_taken_ones(env):
    acct = yt(env, "main", "t", slots=["21:00", "09:00"])
    tz = local_timezone(env.s)
    start = datetime(2030, 3, 10, 8, 0, tzinfo=tz)                        # 08:00 local
    first = env.accounts.next_slot(acct["id"], after=start)
    assert first.astimezone(tz) == datetime(2030, 3, 10, 9, 0, tzinfo=tz)
    just_before = datetime(2030, 3, 10, 8, 55, tzinfo=tz)                # inside the 10 minute lead: too late for 09:00
    assert env.accounts.next_slot(acct["id"], after=just_before).astimezone(tz).hour == 21
    second = env.accounts.next_slot(acct["id"], after=start, taken={iso(first)})
    third = env.accounts.next_slot(acct["id"], after=start, taken={iso(first), iso(second)})
    assert second.astimezone(tz) == datetime(2030, 3, 10, 21, 0, tzinfo=tz)
    assert third.astimezone(tz) == datetime(2030, 3, 11, 9, 0, tzinfo=tz)      # tomorrow


def test_approving_without_times_fills_slots_and_cancelling_frees_them(env, stages):
    acct = yt(env, "main", "t", slots=["10:00"])
    clips = reviewed(env)
    p1 = env.approve(clips[0].id, {"targets": [{"account_id": acct["id"]}]})
    p2 = env.approve(clips[1].id, {"targets": [{"account_id": acct["id"]}]})
    t1 = env.queue.get_post(p1)["targets"][0]["scheduled_at"]
    t2 = env.queue.get_post(p2)["targets"][0]["scheduled_at"]
    assert datetime.strptime(t2, "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(t1, "%Y-%m-%dT%H:%M:%SZ") == timedelta(days=1)
    env.cancel_post(p1)
    assert iso(env.accounts.next_slot(acct["id"])) == t1                  # the freed slot is offered again


def test_no_account_and_no_time_is_refused(env, stages):
    clip = reviewed(env)[0]
    with pytest.raises(ValueError, match="no tiktok account"):
        env.approve(clip.id, {"targets": [{"platform": "tiktok"}]})
    with pytest.raises(ValueError, match="at least one account"):
        env.approve(clip.id, {})
    assert env.store.get_clip(clip.id).status == "review" and env.queue.list_posts() == []


def test_logins_in_env_become_default_accounts_once(tmp_path):
    s = replace(get_settings(), data_dir=tmp_path, youtube_client_id="i", youtube_client_secret="s",
                youtube_refresh_token="r", instagram_user_id="17", instagram_access_token="")
    accounts = Accounts(Database(tmp_path / "a.db"), s, FACTORIES)
    made = accounts.import_env()
    assert [(a["platform"], a["name"]) for a in made] == [("youtube", "default")]      # instagram login is incomplete
    assert accounts.settings_for(made[0]).youtube_refresh_token == "r"
    assert accounts.import_env() == [] and len(accounts.list()) == 1


def test_account_credentials_are_validated_and_merged(env):
    acct = yt(env, "main", "old")
    with pytest.raises(AccountError, match="unknown youtube credential"):
        env.accounts.update(acct["id"], credentials={"password": "x"})
    env.accounts.update(acct["id"], credentials={"refresh_token": "new"})
    got = env.accounts.get(acct["id"])
    assert got["credentials"] == {"client_id": "i", "client_secret": "s", "refresh_token": "new"}
    assert "credentials" not in env.accounts.public(got)
    assert env.accounts.uploader("youtube", acct["id"]).s.youtube_refresh_token == "new"   # not a stale uploader


# ---------------------------------------------------------------- after an upload

def test_posted_everywhere_removes_video_files_and_marks_the_clip_posted(env, stages):
    a, b = yt(env, "main", "tokenA"), yt(env, "second", "tokenB")
    clip = reviewed(env)[0]
    sdir = env.store.source_dir(clip.source_id)
    render, cut = sdir / clip.render_file, sdir / clip.slice_file
    (render.parent / "thumb.jpg").write_bytes(b"jpg")
    pid = env.approve(clip.id, {"targets": [{"account_id": a["id"], "scheduled_at": T0},
                                            {"account_id": b["id"], "scheduled_at": T0 + timedelta(hours=1)}]})
    tick(env, now=T0)
    assert render.exists() and env.store.get_clip(clip.id).status == "scheduled"      # one account still to go
    tick(env, now=T0 + timedelta(hours=1))
    got = env.store.get_clip(clip.id)
    assert got.status == "posted" and not render.exists() and not cut.exists()
    assert (render.parent / "thumb.jpg").exists() and got.post_id == pid              # thumbnail kept for history
    kinds = [(e["kind"], e["title"]) for e in env.store.list_events()]
    assert kinds == [("clips_ready", "2 clips ready to review"), ("upload_done", "Posted to Youtube (main)"),
                     ("upload_done", "Posted to Youtube (second)")]
    assert env.store.list_events()[-1]["data"]["url"] == "https://youtube.test/tokenB"


def test_failed_upload_keeps_the_files_and_every_failure_is_reported(env, stages):
    good, bad = yt(env, "main", "tokenA"), yt(env, "second", "broken")
    ig = env.accounts.create("instagram", "brand", {"user_id": "1", "access_token": "igtok"})
    clip = reviewed(env)[0]
    render = env.store.source_dir(clip.source_id) / clip.render_file
    env.approve(clip.id, {"targets": [{"account_id": x["id"], "scheduled_at": T0} for x in (good, bad, ig)]})
    RecordingUploader.upload_backup = RecordingUploader.upload

    def flaky(self, job):
        if self.platform == "instagram" and not getattr(flaky, "done", False):
            flaky.done = True
            raise UploadError("503 from Instagram", retryable=True)
        return RecordingUploader.upload_backup(self, job)

    RecordingUploader.upload = flaky
    try:
        assert tick(env, now=T0) == {"completed": 1, "retrying": 1, "failed": 1}
        assert render.exists() and env.store.get_clip(clip.id).status == "scheduled"  # needed for the retries
        failures = [e for e in env.store.list_events() if e["kind"] == "upload_failed"]
        assert {(e["title"], e["data"]["final"]) for e in failures} == {
            ("Upload to Youtube (second) failed", True),
            ("Upload to Instagram (brand) failed; it will be tried again", False)}
        assert "login expired" in next(e["body"] for e in failures if e["data"]["final"])
        assert tick(env, now=T0 + timedelta(minutes=5))["completed"] == 1              # instagram retry works
        assert render.exists()                                                         # youtube/second still failed
    finally:
        RecordingUploader.upload = RecordingUploader.upload_backup


def test_a_broken_result_hook_never_undoes_an_upload(env, stages):
    acct = yt(env, "main", "tokenA")
    clip = reviewed(env)[0]
    pid = env.approve(clip.id, {"targets": [{"account_id": acct["id"], "scheduled_at": T0}]})
    stats_ = process_due(env.queue, env.accounts.uploader, settings=env.s, now=T0,
                         on_result=lambda *a: (_ for _ in ()).throw(RuntimeError("hook bug")))
    assert stats_["completed"] == 1 and env.queue.get_post(pid)["status"] == "completed"


# ---------------------------------------------------------------- database upgrade

def test_v3_database_with_scheduled_posts_is_upgraded(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(dbmod, "MIGRATIONS", dbmod.MIGRATIONS[:3])
    Database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO posts (id, title, file_path, scheduled_at, created_at) VALUES ('p1', 't', 'f.mp4', '2030-01-01T10:00:00Z', 'x')")
    conn.execute("INSERT INTO post_targets (post_id, platform, status, attempts, next_attempt_at, remote_id, updated_at)"
                 " VALUES ('p1', 'youtube', 'completed', 1, '2030-01-01T10:00:00Z', 'vid', 'x')")
    conn.execute("INSERT INTO post_targets (post_id, platform, next_attempt_at, updated_at) VALUES ('p1', 'tiktok', '2030-01-01T10:00:00Z', 'x')")
    conn.commit()
    conn.close()
    monkeypatch.undo()
    queue = PostQueue(Database(path))
    post = queue.get_post("p1")
    assert {t["platform"]: (t["status"], t["scheduled_at"], t["account_id"], t["remote_id"]) for t in post["targets"]} == {
        "youtube": ("completed", "2030-01-01T10:00:00Z", None, "vid"), "tiktok": ("pending", "2030-01-01T10:00:00Z", None, None)}
    assert [r["platform"] for r in queue.claim_due(datetime(2030, 1, 2, tzinfo=UTC))] == ["tiktok"]


# ---------------------------------------------------------------- reach

def posted(env, stages, accounts, when=T0):
    """Approve the best clip to `accounts` and publish it. Returns the post's targets."""
    clip = reviewed(env)[0]
    pid = env.approve(clip.id, {"targets": [{"account_id": a["id"], "scheduled_at": when} for a in accounts]})
    tick(env, now=when)
    return env.queue.get_post(pid)["targets"]


def test_reading_interval_slows_down_as_a_clip_ages():
    assert reading_interval(timedelta(hours=5)) == timedelta(hours=3)
    assert reading_interval(timedelta(days=5)) == timedelta(hours=12)
    assert reading_interval(timedelta(days=40)) == timedelta(days=1)
    assert reading_interval(timedelta(days=200)) == timedelta(days=7)


def test_collector_reads_due_clips_per_account_and_keeps_history(env, stages):
    a, b = yt(env, "main", "tokenA"), yt(env, "second", "tokenB")
    targets = posted(env, stages, [a, b])
    clock = [datetime.now(UTC)]
    seen = []

    def read(settings, ids):
        seen.append((settings.youtube_refresh_token, tuple(ids)))
        return {i: Reading(views=100 * len(seen), likes=7, comments=1) for i in ids}

    collector = StatsCollector(env.store.db, env.accounts, {"youtube": (read, lambda s: 1200)}, now=lambda: clock[0])
    assert collector.collect() == {"readings": 2, "followers": 2, "skipped": 0}
    assert sorted(login for login, _ in seen) == ["tokenA", "tokenB"]           # each account read with its own login
    assert collector.collect() == {"readings": 0, "followers": 0, "skipped": 0}     # nothing is due yet
    clock[0] += timedelta(hours=3, minutes=1)
    assert collector.collect()["readings"] == 2
    with env.store.db.connect() as conn:
        rows = conn.execute("SELECT views FROM target_stats WHERE target_id = ? ORDER BY id", (targets[0]["id"],)).fetchall()
    assert len(rows) == 2 and rows[0]["views"] < rows[1]["views"]               # history, not just the latest


def test_login_without_permission_is_noted_and_not_hammered(env, stages):
    acct = yt(env, "main", "tokenA")
    tt = env.accounts.create("tiktok", "tt")
    posted(env, stages, [acct, tt])
    calls = []

    def refuse(settings, ids):
        calls.append(ids)
        raise StatsUnavailable("this YouTube login can upload but not read view counts; log in again")

    collector = StatsCollector(env.store.db, env.accounts, {"youtube": (refuse, refuse)})
    assert collector.collect() == {"readings": 0, "followers": 0, "skipped": 2}
    assert "log in again" in env.accounts.get(acct["id"])["stats_note"]
    assert "TikTok does not report numbers" in env.accounts.get(tt["id"])["stats_note"]
    before = len(calls)
    collector.collect()
    assert len(calls) == before                                                 # asked once, then left alone
    env.accounts.update(acct["id"], credentials={"refresh_token": "fresh"})
    assert env.accounts.get(acct["id"])["stats_note"] == ""                     # a new login clears the note


def test_youtube_reader_batches_and_explains_missing_permission():
    class Call:
        def __init__(self, result):
            self.result = result

        def execute(self):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    class Service:
        def __init__(self, result):
            self.result, self.asked = result, []

        def videos(self):
            return self

        def channels(self):
            return self

        def list(self, **kw):
            self.asked.append(kw)
            if isinstance(self.result, Exception) or "mine" in kw:
                return Call(self.result)
            return Call({"items": [{"id": i, "statistics": {"viewCount": "1500", "likeCount": "30"}}
                                   for i in kw["id"].split(",")]})

    svc = Service(None)
    ids = [f"v{i}" for i in range(120)]
    out = stats.youtube_readings(get_settings(), ids, service=svc)
    assert len(out) == 120 and out["v7"] == Reading(1500, 30, None, None)
    assert [len(c["id"].split(",")) for c in svc.asked] == [50, 50, 20]
    assert stats.youtube_followers(get_settings(), Service({"items": [{"statistics": {"subscriberCount": "321"}}]})) == 321
    forbidden = type("HttpError", (Exception,), {"resp": type("R", (), {"status": 403})()})("insufficientPermissions")
    with pytest.raises(StatsUnavailable, match="log in again"):
        stats.youtube_readings(get_settings(), ["v1"], service=Service(forbidden))


def test_instagram_reader_uses_insights_and_falls_back_to_public_counts():
    s = replace(get_settings(), instagram_user_id="17", instagram_access_token="tok")

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "OAuth tok" and "access_token" not in str(req.url)
        path = req.url.path
        if path.endswith("/M1/insights"):
            return httpx.Response(200, json={"data": [{"name": "views", "values": [{"value": 900}]},
                                                      {"name": "likes", "values": [{"value": 40}]},
                                                      {"name": "shares", "values": [{"value": 3}]}]})
        if path.endswith("/M2/insights"):
            return httpx.Response(400, json={"error": {"message": "metric not supported", "code": 100}})
        if path.endswith("/M2"):
            return httpx.Response(200, json={"like_count": 5, "comments_count": 2})
        if path.endswith("/17"):
            return httpx.Response(200, json={"followers_count": 88})
        if path.endswith("/M3/insights"):
            return httpx.Response(403, json={"error": {"message": "no permission", "code": 10}})
        return httpx.Response(404, json={"error": {"message": "unknown", "code": 100}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = stats.instagram_readings(s, ["M1", "M2"], client)
    assert out["M1"] == Reading(900, 40, None, 3) and out["M2"] == Reading(None, 5, 2, None)
    assert stats.instagram_followers(s, client) == 88
    with pytest.raises(StatsUnavailable, match="insights permission") as denied:
        stats.instagram_readings(s, ["M3", "M2"], client)
    assert denied.value.readings == {"M2": Reading(None, 5, 2, None)}    # what it may read is still returned


def test_insights_show_growth_trend_and_what_works(env, stages):
    a, b = yt(env, "main", "tokenA"), yt(env, "second", "tokenB")
    now = datetime(2030, 6, 20, 12, tzinfo=UTC)
    main_t, second_t = posted(env, stages, [a, b], when=now - timedelta(days=16))
    with env.store.db.connect() as conn:
        def reading(target, days_ago, views):
            conn.execute("INSERT INTO target_stats (target_id, taken_at, views, likes, comments, shares) VALUES (?, ?, ?, ?, 2, NULL)",
                         (target["id"], iso(now - timedelta(days=days_ago)), views, views // 10))
        for days_ago, views in ((15, 100), (8, 400), (1, 1500)):
            reading(main_t, days_ago, views)
        for days_ago, views in ((15, 50), (8, 300), (1, 350)):
            reading(second_t, days_ago, views)
        for days_ago, followers in ((20, 1000), (1, 1180)):
            conn.execute("INSERT INTO account_stats (account_id, taken_at, followers) VALUES (?, ?, ?)",
                         (a["id"], iso(now - timedelta(days=days_ago)), followers))
        conn.execute("UPDATE post_targets SET posted_at = ?", (iso(now - timedelta(days=16)),))
    env.accounts.set_stats_note(b["id"], "needs a new login")

    out = stats.insights(env.store.db, env.s, days=14, now=now)
    assert out["totals"] == {"views": 1850, "likes": 185, "comments": 4, "shares": 0, "posts": 2, "measured_posts": 2}
    # last 7 days: 1850 - 700 = 1150 gained; the 7 before: 700 - 150 = 550
    assert out["trend"] == {"this_week": 1150, "last_week": 550, "change_pct": 109.1, "direction": "up"}
    assert len(out["timeline"]) == 14 and out["timeline"][-1]["views"] == 1850
    assert sum(d["gained"] for d in out["timeline"]) == 1850 - 150               # what the range added in total
    assert all(d["gained"] >= 0 for d in out["timeline"])
    assert [(r["key"], r["views"]) for r in out["by_account"]] == [("main", 1500), ("second", 350)]
    assert out["by_platform"] == [{"key": "youtube", "posts": 2, "views": 1850, "avg_views": 925}]
    assert out["followers"] == [{"account": "main", "platform": "youtube", "followers": 1180, "change": 180}]
    assert [(c["account"], c["views"], c["score"]) for c in out["top_clips"]] == [("main", 1500, 92), ("second", 350, 92)]
    works = out["what_works"]
    assert works["content_type"] == [{"key": "film", "posts": 2, "views": 1850, "avg_views": 925}]
    assert works["ai_score"][0]["key"] == "90-100" and works["length"][0]["key"] == "30-45 s"
    assert works["hook_title"][0]["key"] == "with title" and works["framing"][0]["key"] == "auto"
    assert out["not_measured"] == [{"account": "second", "platform": "youtube", "note": "needs a new login"}]


def test_insights_with_nothing_posted_is_all_zero(env):
    out = stats.insights(env.store.db, env.s, days=7)
    assert out["totals"]["views"] == 0 and out["trend"]["direction"] == "flat" and out["trend"]["change_pct"] is None
    assert out["top_clips"] == [] and len(out["timeline"]) == 7 and out["by_platform"] == []
