from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from clip_pipeline.config import Settings, get_settings
from clip_pipeline.db import Database
from clip_pipeline.publish.queue import PostQueue, derive_status, iso
from clip_pipeline.publish.scheduler import process_due
from clip_pipeline.publish.uploaders.base import BaseUploader, UploadError, UploadJob, UploadResult, build_caption

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def settings(**kw) -> Settings:
    base = dict(interval_seconds=60, max_attempts=3, retry_backoff_seconds=60)
    base.update(kw)
    return replace(get_settings(), **base)


@pytest.fixture
def queue(tmp_path) -> PostQueue:
    return PostQueue(Database(tmp_path / "q.db"))


# ---------------------------------------------------------------- queue + scheduler

class FakeUploader(BaseUploader):
    def __init__(self, platform, outcomes):
        self.platform = platform
        self.outcomes = list(outcomes)
        self.jobs = []

    def is_configured(self):
        return True

    def upload(self, job: UploadJob) -> UploadResult:
        self.jobs.append(job)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def make_post(tmp_path, queue, when, platforms=("youtube",)):
    f = tmp_path / f"{len(list(tmp_path.glob('*.mp4')))}.mp4"
    f.write_bytes(MP4)
    return queue.create_post(title="t", description="d", tags=["x"], platforms=list(platforms),
                             file_path=f, scheduled_at=when)


def test_post_roundtrip_normalises_time_and_dedupes_platforms(tmp_path, queue):
    when = datetime(2030, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    pid = make_post(tmp_path, queue, when, platforms=("youtube", "tiktok", "youtube"))
    post = queue.get_post(pid)
    assert post["scheduled_at"] == "2030-01-01T10:00:00Z"
    assert post["status"] == "pending" and post["platforms"] == ["youtube", "tiktok"] and post["tags"] == ["x"]
    assert [p["id"] for p in queue.list_posts()] == [pid]
    assert queue.list_posts(status="completed") == [] and queue.get_post("nope") is None


def test_only_due_posts_are_processed(tmp_path, queue):
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    due = make_post(tmp_path, queue, now - timedelta(minutes=1))
    future = make_post(tmp_path, queue, now + timedelta(hours=1))
    yt = FakeUploader("youtube", [UploadResult("vid1", "https://youtube.com/shorts/vid1")])
    stats = process_due(queue, {"youtube": yt}, settings=settings(), now=now)
    assert stats == {"completed": 1, "retrying": 0, "failed": 0}
    assert queue.get_post(due)["status"] == "completed"
    assert queue.get_post(due)["targets"][0]["remote_url"].endswith("vid1")
    assert queue.get_post(future)["status"] == "pending"
    assert yt.jobs[0].upload_id == due and yt.jobs[0].title == "t"


def test_retry_with_backoff_then_fail(tmp_path, queue):
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    pid = make_post(tmp_path, queue, now)
    up = FakeUploader("youtube", [UploadError("503")] * 3)
    s = settings()

    assert process_due(queue, {"youtube": up}, settings=s, now=now)["retrying"] == 1
    t = queue.get_post(pid)["targets"][0]
    assert t["status"] == "pending" and t["next_attempt_at"] == iso(now + timedelta(seconds=60))
    assert process_due(queue, {"youtube": up}, settings=s, now=now + timedelta(seconds=30))["retrying"] == 0  # backing off

    process_due(queue, {"youtube": up}, settings=s, now=now + timedelta(seconds=61))     # attempt 2 -> wait 120 s
    stats = process_due(queue, {"youtube": up}, settings=s, now=now + timedelta(seconds=200))  # attempt 3
    assert stats["failed"] == 1
    assert queue.get_post(pid)["status"] == "failed" and len(up.jobs) == 3

    assert queue.retry_failed(pid) == 1
    assert queue.get_post(pid)["status"] == "pending"


def test_non_retryable_fails_immediately_and_platforms_independent(tmp_path, queue):
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    pid = make_post(tmp_path, queue, now, platforms=("youtube", "tiktok"))
    ups = {"youtube": FakeUploader("youtube", [UploadResult("v")]),
           "tiktok": FakeUploader("tiktok", [UploadError("captcha", retryable=False)])}
    stats = process_due(queue, ups, settings=settings(), now=now)
    assert stats == {"completed": 1, "retrying": 0, "failed": 1}
    assert queue.get_post(pid)["status"] == "partial"


def test_missing_uploader_and_unexpected_errors_never_kill_the_tick(tmp_path, queue):
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    pid = make_post(tmp_path, queue, now, platforms=("youtube", "myspace"))
    yt = FakeUploader("youtube", [RuntimeError("bug")])
    stats = process_due(queue, {"youtube": yt}, settings=settings(), now=now)
    assert stats == {"completed": 0, "retrying": 1, "failed": 1}
    errors = {t["platform"]: t["error"] for t in queue.get_post(pid)["targets"]}
    assert "no usable myspace account" in errors["myspace"] and "bug" in errors["youtube"]


def test_claim_is_exclusive_and_stale_recovery(tmp_path, queue):
    now = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
    make_post(tmp_path, queue, now)
    assert len(queue.claim_due(now)) == 1
    assert queue.claim_due(now) == []            # already processing
    assert queue.recover_stale() == 1
    assert len(queue.claim_due(now)) == 1


def test_derive_status():
    assert derive_status(["completed", "completed"]) == "completed"
    assert derive_status(["completed", "pending"]) == "pending"
    assert derive_status(["failed", "completed"]) == "partial"
    assert derive_status(["failed"]) == "failed"


def test_unconfigured_uploader_fails_without_calling_platform(tmp_path):
    from clip_pipeline.publish.uploaders.youtube import YouTubeUploader

    job = UploadJob("u", "youtube", tmp_path / "x.mp4", "t")
    with pytest.raises(UploadError) as e:
        YouTubeUploader(settings()).run(job)
    assert not e.value.retryable


# ---------------------------------------------------------------- uploaders

def test_build_caption_truncates_but_keeps_hashtags():
    cap = build_caption("Title", "x" * 3000, ["fun clip", "gaming"], max_len=200)
    assert len(cap) <= 200 and cap.endswith("#funclip #gaming")


def test_youtube_body_and_upload(tmp_path):
    from clip_pipeline.publish.uploaders.youtube import YouTubeUploader

    f = tmp_path / "v.mp4"
    f.write_bytes(MP4)
    captured = {}

    class Req:
        def __init__(self):
            self.n = 0

        def next_chunk(self):
            self.n += 1
            return (None, {"id": "abc123"}) if self.n > 1 else (type("S", (), {"progress": lambda s: 0.5})(), None)

    class Videos:
        def insert(self, part, body, media_body):
            captured.update(part=part, body=body)
            return Req()

    svc = type("Svc", (), {"videos": lambda self: Videos()})()
    s = settings(youtube_client_id="i", youtube_client_secret="s", youtube_refresh_token="r")
    up = YouTubeUploader(s, service_factory=lambda: svc)
    res = up.run(UploadJob("u", "youtube", f, "My clip", "desc", ["gaming"]))
    assert res.url == "https://youtube.com/shorts/abc123"
    assert captured["body"]["snippet"]["title"] == "My clip"
    assert "#Shorts" in captured["body"]["snippet"]["description"]
    assert captured["body"]["status"]["privacyStatus"] == "public"
    up.run(UploadJob("u", "youtube", f, "My clip", extra={"visibility": "private"}))
    assert captured["body"]["status"]["privacyStatus"] == "private"   # per-clip override


def test_instagram_resumable_flow(tmp_path):
    from clip_pipeline.publish.uploaders.instagram import InstagramUploader

    f = tmp_path / "v.mp4"
    f.write_bytes(MP4)
    seen = []
    statuses = iter(["IN_PROGRESS", "FINISHED"])

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.host, req.url.path))
        assert "access_token" not in str(req.url)                  # token only in header
        assert req.headers["authorization"] == "OAuth tok"
        if req.url.path.endswith("/media") and req.method == "POST":
            return httpx.Response(200, json={"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/v23.0/C1"})
        if req.url.host == "rupload.facebook.com":
            assert req.headers["offset"] == "0" and req.headers["file_size"] == str(len(MP4))
            return httpx.Response(200, json={"success": True})
        if req.url.path.endswith("/C1"):
            return httpx.Response(200, json={"status_code": next(statuses)})
        if req.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "M9"})
        if req.url.path.endswith("/M9"):
            return httpx.Response(200, json={"permalink": "https://instagram.com/reel/x"})
        return httpx.Response(404)

    s = settings(instagram_user_id="17841", instagram_access_token="tok")
    up = InstagramUploader(s, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda x: None)
    res = up.run(UploadJob("u", "instagram", f, "Title", "desc", ["tag"]))
    assert res.remote_id == "M9" and res.url == "https://instagram.com/reel/x"
    assert ("POST", "rupload.facebook.com", "/ig-api-upload/v23.0/C1") in seen


def test_instagram_preflight_never_publishes(tmp_path):
    from clip_pipeline.publish.uploaders.instagram import InstagramUploader

    f = tmp_path / "v.mp4"
    f.write_bytes(MP4)
    paths = []

    def handler(req: httpx.Request) -> httpx.Response:
        paths.append(req.url.path)
        if req.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "C1"})
        if req.url.host == "rupload.facebook.com":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"status_code": "FINISHED"})

    s = settings(instagram_user_id="1", instagram_access_token="t")
    msg = InstagramUploader(s, client=httpx.Client(transport=httpx.MockTransport(handler))).preflight(
        UploadJob("u", "instagram", f, "T"))
    assert "C1" in msg and not any(p.endswith("media_publish") for p in paths)


def test_instagram_error_classification(tmp_path):
    from clip_pipeline.publish.uploaders.instagram import InstagramUploader

    f = tmp_path / "v.mp4"
    f.write_bytes(MP4)
    transport = httpx.MockTransport(lambda r: httpx.Response(400, json={"error": {"message": "Invalid token"}}))
    s = settings(instagram_user_id="1", instagram_access_token="t")
    with pytest.raises(UploadError) as e:
        InstagramUploader(s, client=httpx.Client(transport=transport)).run(UploadJob("u", "instagram", f, "T"))
    assert not e.value.retryable and "Invalid token" in str(e.value)


def test_tiktok_unconfigured_without_profile(tmp_path):
    from clip_pipeline.publish.uploaders.tiktok import TikTokUploader

    s = settings(tiktok_profile_dir=str(tmp_path / "none"), tiktok_state_file=str(tmp_path / "state.json"))
    assert not TikTokUploader(s).is_configured()
    (tmp_path / "state.json").write_text('{"cookies": [], "origins": []}')
    assert TikTokUploader(s).is_configured()     # portable storage-state from a login on another machine
