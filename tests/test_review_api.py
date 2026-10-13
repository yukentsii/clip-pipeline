"""Steps 3 and 4 over HTTP: review clips, pick accounts, manage the schedule, see status."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from clip_pipeline.api.app import create_app
from clip_pipeline.config import get_settings
from clip_pipeline.publish.uploaders.base import BaseUploader

from test_worker import Stages

WHEN = "2030-05-01T18:30:00+02:00"
WHEN_UTC = "2030-05-01T16:30:00Z"
PAST = "2020-01-01T00:00:00Z"


class FakeUploader(BaseUploader):
    """Configured exactly when the account it was built for carries a login."""

    def __init__(self, platform, configured):
        self.platform, self.configured = platform, configured

    def is_configured(self):
        return self.configured

    def upload(self, job):
        raise AssertionError("nothing is published in these tests")


FACTORIES = {
    "youtube": lambda s: FakeUploader("youtube", bool(s.youtube_refresh_token)),
    "instagram": lambda s: FakeUploader("instagram", bool(s.instagram_access_token)),
    "tiktok": lambda s: FakeUploader("tiktok", False),
}
YT_LOGIN = {"client_id": "i", "client_secret": "s", "refresh_token": "r"}


@pytest.fixture
def stages(monkeypatch) -> Stages:
    return Stages(monkeypatch)


@pytest.fixture
def client(tmp_path, stages):
    app = create_app(replace(get_settings(), data_dir=tmp_path), uploader_factories=FACTORIES, background=False)
    with TestClient(app) as c:
        c.worker, c.store, c.queue = app.state.worker, app.state.store, app.state.queue
        c.yt = c.post("/accounts", json={"platform": "youtube", "name": "main", "credentials": YT_LOGIN}).json()["id"]
        c.yt2 = c.post("/accounts", json={"platform": "youtube", "name": "second", "credentials": YT_LOGIN,
                                          "slots": ["12:00"]}).json()["id"]
        c.tt = c.post("/accounts", json={"platform": "tiktok", "name": "tt"}).json()["id"]
        yield c
    app.state.worker.shutdown()


@pytest.fixture
def feed(client):
    """One processed source; returns the review feed (best clip first)."""
    client.post("/sources", json={"url": "https://youtu.be/yjLcN0RFYTI"})
    assert client.worker.wait_idle(timeout=20)
    clips = client.get("/clips").json()["clips"]
    assert [c["candidate_id"] for c in clips] == ["c00", "c01"]
    return clips


def approve(client, clip_id, when=WHEN, accounts=None, **kw):
    targets = [{"account_id": a, **({"scheduled_at": when} if when else {})} for a in accounts or [client.yt, client.tt]]
    return client.post(f"/clips/{clip_id}/approve", json={"targets": targets, **kw})


def target_states(post):
    return {t["account"]: t["status"] for t in post["targets"]}


# ---------------------------------------------------------------- review feed

def test_feed_has_everything_the_review_screen_needs(client, feed):
    clip = feed[0]
    assert clip["status"] == "review" and clip["virality_score"] == 92 and clip["source_title"] == "A Stream"
    assert (clip["hook_score"], clip["engagement_score"], clip["value_score"], clip["shareability_score"]) == (23, 23, 23, 23)
    assert clip["title"] == "Title c00" and clip["hook_title"] == "Wait for it" and clip["duration"] == 40.0
    assert (clip["trim_min"], clip["trim_max"]) == (0.0, 55.0)
    assert not {"slice_file", "render_file", "thumb_file"} & set(clip)             # no server paths leak out
    video = client.get(clip["video_url"])
    assert video.status_code == 200 and video.content == b"video" and video.headers["content-type"] == "video/mp4"
    part = client.get(clip["video_url"], headers={"Range": "bytes=1-3"})          # players seek with ranges
    assert part.status_code == 206 and part.content == b"ide"
    assert clip["thumb_url"] is None and client.get(f"/clips/{clip['id']}/thumb").status_code == 404
    assert client.get(f"/clips/{clip['id']}").json()["id"] == clip["id"]


def test_feed_filters_and_unknown_ids(client, feed):
    assert [c["candidate_id"] for c in client.get("/clips?status=below_threshold").json()["clips"]] == ["c02"]
    assert client.get("/clips?status=nonsense").status_code == 422
    assert client.get("/clips/missing").status_code == 404
    assert client.get("/clips/missing/video").status_code == 404
    assert approve(client, "missing").status_code == 404


def test_edit_text_is_instant_and_picture_edit_rerenders(client, feed, stages):
    cid, renders = feed[0]["id"], stages.calls["render"]
    r = client.patch(f"/clips/{cid}", json={"title": "Better title", "tags": ["#Film", "film", " drama "]})
    assert r.status_code == 200 and r.json()["rerendering"] is False
    assert (r.json()["title"], r.json()["tags"], r.json()["status"]) == ("Better title", ["Film", "drama"], "review")
    assert stages.calls["render"] == renders

    queued = []
    client.worker.submit_render = queued.append                                    # hold the render so we can look
    r = client.patch(f"/clips/{cid}", json={"start": 2.0, "end": 30.0, "layout": "fit", "hook_title": ""})
    assert r.status_code == 200 and r.json()["rerendering"] is True and r.json()["status"] == "sliced"
    assert approve(client, cid).status_code == 409                                 # not while it is re-rendering
    assert queued == [cid] and client.worker.render_for_review(cid)
    after = client.get(f"/clips/{cid}").json()
    assert (after["status"], after["start"], after["end"], after["layout"], after["hook_title"]) == ("review", 2.0, 30.0, "fit", "")
    assert after["video_url"] != feed[0]["video_url"] and stages.calls["render"] == renders + 1


def test_edit_rejects_bad_input(client, feed):
    cid = feed[0]["id"]
    assert client.patch(f"/clips/{cid}", json={"start": 10, "end": 11}).status_code == 422       # under 3 s
    assert client.patch(f"/clips/{cid}", json={"aspect": "4:3"}).status_code == 422
    assert client.patch(f"/clips/{cid}", json={"title": ""}).status_code == 422
    assert client.patch(f"/clips/{cid}", json={"title": "x" * 101}).status_code == 422
    assert client.get(f"/clips/{cid}").json()["title"] == "Title c00"


# ---------------------------------------------------------------- approving: accounts and times

def test_approve_with_final_edits_then_no_second_decision(client, feed):
    cid = feed[0]["id"]
    assert client.post(f"/clips/{cid}/approve", json={"targets": []}).status_code == 422
    assert client.post(f"/clips/{cid}/approve", json={"targets": [{"scheduled_at": WHEN}]}).status_code == 422
    assert approve(client, cid, accounts=["no-such-account"]).status_code == 422
    assert client.get(f"/clips/{cid}").json()["status"] == "review"                # nothing half-done

    r = approve(client, cid, title="Final title", description="Final text", visibility="unlisted")
    assert r.status_code == 201
    post = client.get(f"/posts/{r.json()['post_id']}").json()
    assert (post["title"], post["description"], post["scheduled_at"]) == ("Final title", "Final text", WHEN_UTC)
    assert post["platforms"] == ["youtube", "tiktok"] and post["extra"]["visibility"] == "unlisted" and post["editable"]
    assert [(t["account"], t["account_id"], t["scheduled_at"]) for t in post["targets"]] == [
        ("main", client.yt, WHEN_UTC), ("tt", client.tt, WHEN_UTC)]
    assert "file_path" not in post and post["video_url"] == f"/clips/{cid}/video"
    assert approve(client, cid).status_code == 409 and client.post(f"/clips/{cid}/reject").status_code == 409
    assert [c["id"] for c in client.get("/clips").json()["clips"]] == [feed[1]["id"]]        # gone from the feed


def test_each_account_can_have_its_own_time_and_two_accounts_share_a_platform(client, feed):
    r = client.post(f"/clips/{feed[0]['id']}/approve", json={"targets": [
        {"account_id": client.yt, "scheduled_at": "2030-05-01T10:00:00Z"},
        {"account_id": client.yt2, "scheduled_at": "2030-05-02T10:00:00Z"},
        {"account_id": client.tt, "scheduled_at": "2030-05-01T20:00:00Z"}]})
    assert r.status_code == 201, r.text
    post = client.get(f"/posts/{r.json()['post_id']}").json()
    assert {t["account"]: t["scheduled_at"] for t in post["targets"]} == {
        "main": "2030-05-01T10:00:00Z", "second": "2030-05-02T10:00:00Z", "tt": "2030-05-01T20:00:00Z"}
    assert post["scheduled_at"] == "2030-05-01T10:00:00Z" and post["platforms"] == ["youtube", "tiktok"]


def test_without_a_time_each_account_gets_its_next_free_slot(client, feed):
    upcoming = client.get(f"/accounts/{client.yt}/next-slots?count=3").json()["slots"]
    assert len(set(upcoming)) == 3 and upcoming == sorted(upcoming)
    a = approve(client, feed[0]["id"], when=None, accounts=[client.yt, client.yt2]).json()["post_id"]
    b = approve(client, feed[1]["id"], when=None, accounts=[client.yt]).json()["post_id"]
    first = {t["account"]: t["scheduled_at"] for t in client.get(f"/posts/{a}").json()["targets"]}
    second = client.get(f"/posts/{b}").json()["targets"][0]["scheduled_at"]
    assert first["main"] == upcoming[0] and second == upcoming[1]                  # slots fill in order, no clashes
    assert first["second"].endswith(":00:00Z") and first["second"] != first["main"]
    assert client.get(f"/accounts/{client.yt}").json()["next_slot"] == upcoming[2]
    assert client.get("/status").json()["next_post_at"] == min(first["main"], first["second"], second)


# ---------------------------------------------------------------- reject

def test_reject_removes_it_from_the_feed(client, feed):
    cid = feed[1]["id"]
    assert client.post(f"/clips/{cid}/reject").json() == {"status": "rejected"}
    assert client.post(f"/clips/{cid}/reject").status_code == 409
    assert client.get(f"/clips/{cid}/video").status_code == 404
    assert [c["id"] for c in client.get("/clips").json()["clips"]] == [feed[0]["id"]]


# ---------------------------------------------------------------- schedule

def test_edit_a_scheduled_post(client, feed):
    pid = approve(client, feed[0]["id"]).json()["post_id"]
    r = client.patch(f"/posts/{pid}", json={"description": "changed my mind", "scheduled_at": "2031-01-01T09:00:00Z"})
    assert r.status_code == 200, r.text
    post = r.json()
    assert post["description"] == "changed my mind" and post["title"] == "Title c00"
    assert {t["scheduled_at"] for t in post["targets"]} == {t["next_attempt_at"] for t in post["targets"]} == {"2031-01-01T09:00:00Z"}

    # swap tiktok for the second youtube account, and move only the main one
    r = client.patch(f"/posts/{pid}", json={"targets": [
        {"account_id": client.yt, "scheduled_at": "2031-02-02T08:00:00Z"}, {"account_id": client.yt2}]})
    assert r.status_code == 200, r.text
    times = {t["account"]: t["scheduled_at"] for t in r.json()["targets"]}
    assert set(times) == {"main", "second"} and times["main"] == "2031-02-02T08:00:00Z"
    assert times["second"] != "2031-01-01T09:00:00Z"                               # a new account takes its own slot
    assert r.json()["scheduled_at"] == min(times.values())

    assert client.patch(f"/posts/{pid}", json={"targets": []}).status_code == 422
    assert client.patch(f"/posts/{pid}", json={"targets": [{"account_id": "nope"}]}).status_code == 422
    assert client.patch("/posts/missing", json={"title": "x"}).status_code == 404
    assert [p["id"] for p in client.get("/posts").json()["posts"]] == [pid]
    assert client.get("/posts?status=completed").json()["posts"] == []


def test_posted_targets_are_locked_and_finished_posts_cannot_change(client, feed):
    pid = approve(client, feed[0]["id"], when=PAST).json()["post_id"]
    yt, tt = client.queue.claim_due()
    client.queue.mark_completed(yt["target_id"], "vid", "https://youtube.com/shorts/vid")
    client.queue.mark_failed(tt["target_id"], "login expired", retryable=False, max_attempts=3, backoff_seconds=60)
    # main already went out: it stays whatever is asked; the failed tiktok one can be swapped
    post = client.patch(f"/posts/{pid}", json={"targets": [{"account_id": client.yt2, "scheduled_at": PAST}]}).json()
    assert target_states(post) == {"main": "completed", "second": "pending"}
    nxt = client.queue.claim_due()[0]
    client.queue.mark_completed(nxt["target_id"], "m1")
    assert client.patch(f"/posts/{pid}", json={"title": "too late"}).status_code == 409
    assert client.get(f"/posts/{pid}").json()["editable"] is False


def test_cancel_a_scheduled_post(client, feed):
    cid = feed[0]["id"]
    pid = approve(client, cid).json()["post_id"]
    r = client.delete(f"/posts/{pid}")
    assert r.status_code == 200 and r.json()["clip_back_in_review"] is True and r.json()["already_posted"] == []
    assert client.get(f"/posts/{pid}").status_code == 404 and client.delete(f"/posts/{pid}").status_code == 404
    assert client.get(f"/clips/{cid}").json()["status"] == "review"

    pid = approve(client, cid, when=PAST).json()["post_id"]
    client.queue.claim_due()
    assert client.delete(f"/posts/{pid}").status_code == 409                       # uploads are running right now


def test_retry_failed_target(client, feed):
    pid = approve(client, feed[0]["id"], when=PAST).json()["post_id"]
    for t in client.queue.claim_due():
        client.queue.mark_failed(t["target_id"], "boom", retryable=False, max_attempts=3, backoff_seconds=60)
    assert client.get(f"/posts/{pid}").json()["status"] == "failed"
    assert client.post(f"/posts/{pid}/retry").json() == {"requeued": 2}
    assert client.get(f"/posts/{pid}").json()["status"] == "pending"
    assert client.post("/posts/missing/retry").status_code == 404


# ---------------------------------------------------------------- accounts

def test_accounts_never_reveal_their_login(client):
    accounts = {a["name"]: a for a in client.get("/accounts").json()["accounts"]}
    assert set(accounts) == {"main", "second", "tt"}
    assert all("credentials" not in a for a in accounts.values()) and "refresh_token" not in client.get("/accounts").text
    assert (accounts["main"]["configured"], accounts["tt"]["configured"]) == (True, False)
    assert accounts["main"]["slots"] == ["09:00", "15:00", "21:00"] and accounts["second"]["slots"] == ["12:00"]
    assert accounts["main"]["next_slot"]


def test_account_create_edit_and_validation(client):
    bad = [{"platform": "myspace", "name": "x"}, {"platform": "youtube", "name": ""},
           {"platform": "youtube", "name": "main"},                                 # name taken on that platform
           {"platform": "youtube", "name": "y", "credentials": {"password": "hunter2"}},
           {"platform": "youtube", "name": "y", "slots": ["25:00"]}, {"platform": "youtube", "name": "y", "slots": []}]
    for body in bad:
        assert client.post("/accounts", json=body).status_code == 422, body
    r = client.post("/accounts", json={"platform": "instagram", "name": "main", "slots": ["18:30", "07:05", "18:30"]})
    assert r.status_code == 201 and r.json()["slots"] == ["07:05", "18:30"] and r.json()["configured"] is False
    aid = r.json()["id"]
    r = client.patch(f"/accounts/{aid}", json={"credentials": {"user_id": "1", "access_token": "t"}, "name": "brand"})
    assert r.status_code == 200 and r.json()["configured"] is True and r.json()["name"] == "brand"
    assert client.patch(f"/accounts/{aid}", json={"enabled": False}).json()["next_slot"] is None
    assert client.patch(f"/accounts/{aid}", json={"slots": ["9am"]}).status_code == 422
    assert client.patch("/accounts/missing", json={"name": "x"}).status_code == 404
    assert client.delete(f"/accounts/{aid}").status_code == 204 and client.get(f"/accounts/{aid}").status_code == 404


def test_switched_off_or_busy_accounts(client, feed):
    client.patch(f"/accounts/{client.yt2}", json={"enabled": False})
    assert approve(client, feed[0]["id"], accounts=[client.yt2]).status_code == 422            # switched off
    approve(client, feed[0]["id"], accounts=[client.yt])
    assert client.delete(f"/accounts/{client.yt}").status_code == 409                          # a post still needs it
    assert client.delete(f"/accounts/{client.yt2}").status_code == 204


# ---------------------------------------------------------------- status, events and settings

def test_status_summarises_and_lists_what_needs_attention(client, feed, stages):
    stages.fail.add("analyze")
    bad = client.post("/sources", json={"url": "https://youtu.be/AAAAAAAAAAA"}).json()["id"]
    assert client.worker.wait_idle(timeout=20)
    pid = approve(client, feed[0]["id"], when=PAST).json()["post_id"]
    yt, tt = client.queue.claim_due()
    client.queue.mark_completed(yt["target_id"], "vid")
    client.queue.mark_failed(tt["target_id"], "TikTok login expired", retryable=False, max_attempts=3, backoff_seconds=60)
    client.store.update_clip(feed[1]["id"], status="failed", error="Render failed: disk full")

    body = client.get("/status").json()
    assert body["working_on"] == [] and body["queued_sources"] == 0
    assert body["clips"] == {"scheduled": 1, "failed": 1, "below_threshold": 1}
    assert body["posts"] == {"partial": 1} and body["next_post_at"] is None
    assert {a["name"]: a["configured"] for a in body["accounts"]} == {"main": True, "second": True, "tt": False}
    attention = {a["kind"]: a for a in body["needs_attention"]}
    assert attention["source"]["id"] == bad and "analyze blew up" in attention["source"]["reason"]
    assert attention["clip"]["reason"] == "Render failed: disk full" and attention["clip"]["source"] == "A Stream"
    assert attention["post"] == {"kind": "post", "id": pid, "title": "Title c00", "platform": "tiktok", "account": "tt",
                                 "reason": "TikTok login expired", "action": f"POST /posts/{pid}/retry"}
    assert attention["account"]["title"] == "tt" and "login" in attention["account"]["reason"]

    # each listed action works
    assert client.post(f"/sources/{bad}/retry").status_code == 202 and client.worker.wait_idle(timeout=20)
    assert client.get(f"/sources/{bad}").json()["status"] == "ready"
    assert client.post(f"/sources/{bad}/retry").status_code == 409
    assert client.post(f"/clips/{feed[1]['id']}/retry").json() == {"status": "sliced"}
    assert client.post(f"/clips/{feed[1]['id']}/retry").status_code == 409
    assert client.post("/sources/missing/retry").status_code == 404


def test_status_shows_progress(client, feed):
    src = client.store.create_source(url="https://youtu.be/BBBBBBBBBBB")
    client.store.update_source(src.id, status="running", stage="whisper", stage_progress=0.4, message="Transcribing")
    (running,) = client.get("/status").json()["working_on"]
    assert (running["stage"], running["stage_progress"], running["message"]) == ("whisper", 0.4, "Transcribing")
    assert running["stages"] == ["audio", "analysis", "whisper", "llm", "slices", "render"]


def test_events_report_ready_clips_and_failures_and_can_be_followed(client, feed, stages):
    first = client.get("/events").json()
    assert [(e["kind"], e["title"], e["body"]) for e in first["events"]] == [("clips_ready", "2 clips ready to review", "A Stream")]
    stages.fail.add("analyze")
    client.post("/sources", json={"url": "https://youtu.be/AAAAAAAAAAA"})
    assert client.worker.wait_idle(timeout=20)
    news = client.get(f"/events?after={first['last_id']}").json()
    assert [e["kind"] for e in news["events"]] == ["source_failed"] and "analyze blew up" in news["events"][0]["body"]
    assert news["events"][0]["data"]["stage"] == "analysis"
    assert client.get(f"/events?after={news['last_id']}").json() == {"events": [], "last_id": news["last_id"]}


def test_settings_roundtrip_and_validation(client):
    assert client.get("/settings").json() == {"review_min_score": 70, "default_aspect": "9:16",
                                              "default_layout": "auto", "default_subtitle_style": "karaoke"}
    r = client.patch("/settings", json={"review_min_score": 85, "default_layout": "fit"})
    assert r.status_code == 200 and r.json()["review_min_score"] == 85 and r.json()["default_layout"] == "fit"
    assert client.get("/system").json()["review_min_score"] == 85
    assert client.patch("/settings", json={"review_min_score": 150}).status_code == 422
    assert client.patch("/settings", json={"default_aspect": "4:3"}).status_code == 422
    assert client.get("/settings").json()["default_aspect"] == "9:16"
