"""Step 2: the inbox folder, links and uploads, no repeats, and cleaning up after a source."""
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from clip_pipeline import db as dbmod
from clip_pipeline import proc
from clip_pipeline.api.app import create_app
from clip_pipeline.config import get_settings
from clip_pipeline.db import Database
from clip_pipeline.sources import inbox
from clip_pipeline.sources.inbox import InboxWatcher, file_fingerprint, url_fingerprint
from clip_pipeline.worker import mark_overlaps

from test_worker import Stages, approve_opts, build, statuses  # noqa: F401 - shared fakes


@pytest.fixture
def stages(monkeypatch) -> Stages:
    return Stages(monkeypatch)


@pytest.fixture
def worker(tmp_path):
    w = build(tmp_path)
    yield w
    w.shutdown()


def drop(folder, name, data=b"video-bytes", age=60.0):
    """Put a file in the inbox as if its copy finished `age` seconds ago."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    stamp = datetime.now().timestamp() - age
    os.utime(path, (stamp, stamp))
    return path


# ---------------------------------------------------------------- fingerprints

@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=yjLcN0RFYTI", "https://youtu.be/yjLcN0RFYTI?si=abc123",
    "https://m.youtube.com/watch?feature=share&v=yjLcN0RFYTI&t=42s", "https://youtube.com/shorts/yjLcN0RFYTI",
    "https://www.youtube.com/live/yjLcN0RFYTI?utm_source=x",
])
def test_every_form_of_a_youtube_link_is_the_same_video(url):
    assert url_fingerprint(url) == "youtube:yjLcN0RFYTI"


def test_other_links_ignore_tracking_but_keep_what_identifies_the_video():
    assert url_fingerprint("https://www.twitch.tv/videos/123456?t=1h2m") == "twitch:123456"
    a = url_fingerprint("https://www.example.com/watch/?id=7&utm_source=tw&fbclid=zz")
    assert a == url_fingerprint("https://example.com/watch?id=7") == "url:example.com/watch?id=7"
    assert a != url_fingerprint("https://example.com/watch?id=8")
    assert url_fingerprint("https://youtu.be.evil.test/watch?x=yjLcN0RFYTI").startswith("url:")


def test_file_fingerprint_follows_content_not_name(tmp_path):
    a, b, c = tmp_path / "a.mp4", tmp_path / "renamed.mkv", tmp_path / "c.mp4"
    body = os.urandom(3 * 1024 * 1024 + 123)
    a.write_bytes(body)
    b.write_bytes(body)
    c.write_bytes(body[:-1] + bytes([body[-1] ^ 1]))           # same size, last byte differs
    assert file_fingerprint(a) == file_fingerprint(b) != file_fingerprint(c)
    assert file_fingerprint(a).startswith(f"file:{len(body)}:")


# ---------------------------------------------------------------- inbox watcher

def test_watcher_waits_until_a_copy_has_finished(tmp_path):
    clock = [1000.0]
    w = InboxWatcher(tmp_path / "inbox", settle_seconds=10, now=lambda: clock[0])
    assert w.ready_files() == []                               # creates the folder
    f = tmp_path / "inbox" / "movie.mp4"
    f.write_bytes(b"12")
    os.utime(f, (995, 995))
    assert w.ready_files() == []                               # first sighting: just remember it
    f.write_bytes(b"1234")                                     # still growing
    os.utime(f, (999, 999))
    assert w.ready_files() == []
    assert w.ready_files() == []                               # unchanged, but written only 1 s ago
    clock[0] = 1009.5
    assert w.ready_files() == [f]                              # unchanged and settled
    assert w.ready_files() == [f]                              # still offered until someone takes it


def test_watcher_ignores_partials_other_files_and_empty_files(tmp_path):
    folder = tmp_path / "inbox"
    for name, data in [("up.mp4.part", b"x"), ("notes.txt", b"x"), ("empty.mp4", b""), ("ok.MKV", b"x")]:
        drop(folder, name, data)
    (folder / "_duplicates").mkdir()
    drop(folder / "_duplicates", "old.mp4")
    w = InboxWatcher(folder, settle_seconds=10)
    w.ready_files()
    assert [p.name for p in w.ready_files()] == ["ok.MKV"]


# ---------------------------------------------------------------- taking files and links

def test_inbox_file_is_taken_processed_and_its_inbox_slot_freed(worker, stages, tmp_path):
    video = drop(worker.s.inbox_dir, "Great.Movie.mkv")
    assert worker.scan_inbox() == []                           # first scan only registers it
    (src,) = worker.scan_inbox()
    assert not video.exists()                                  # gone from the inbox = it was picked up
    assert worker.wait_idle(timeout=20)
    got = worker.store.get_source(src.id)
    original = worker.store.source_dir(src.id) / "original.mkv"
    assert got.owns_file and got.file_path == str(original) and original.read_bytes() == b"video-bytes"
    assert (got.status, got.title, got.fingerprint[:5]) == ("ready", "Local Movie", "file:")
    assert statuses(worker, src.id) == {"c00": "review", "c01": "review", "c02": "below_threshold"}
    assert worker.scan_inbox() == []


def test_same_video_dropped_again_is_parked_not_reprocessed(worker, stages):
    drop(worker.s.inbox_dir, "a.mp4")
    worker.scan_inbox()
    (first,) = worker.scan_inbox()
    assert worker.wait_idle(timeout=20)
    again = drop(worker.s.inbox_dir, "same video other name.mp4")
    worker.scan_inbox()
    assert worker.scan_inbox() == [] and not again.exists()
    assert (worker.s.inbox_dir / "_duplicates" / "same video other name.mp4").exists()
    assert [s.id for s in worker.store.list_sources()] == [first.id] and stages.calls["analyze"] == 1


def test_a_failed_source_may_be_tried_again(worker, stages):
    stages.fail.add("analyze")
    first, new1 = worker.add_url("https://youtu.be/yjLcN0RFYTI")
    assert worker.wait_idle(timeout=20) and worker.store.get_source(first.id).status == "failed"
    second, new2 = worker.add_url("https://www.youtube.com/watch?v=yjLcN0RFYTI&feature=share")
    assert new1 and new2 and second.id != first.id
    assert worker.wait_idle(timeout=20) and worker.store.get_source(second.id).status == "ready"
    third, new3 = worker.add_url("https://youtu.be/yjLcN0RFYTI?si=x")
    assert not new3 and third.id == second.id                  # now it is known


def test_locked_inbox_file_is_left_for_the_next_scan(worker, stages, monkeypatch):
    video = drop(worker.s.inbox_dir, "busy.mp4")
    real_move = inbox.shutil.move
    monkeypatch.setattr("clip_pipeline.worker.shutil.move", lambda *a: (_ for _ in ()).throw(PermissionError("in use")))
    worker.scan_inbox()
    assert worker.scan_inbox() == [] and video.exists()
    assert worker.store.list_sources() == []                   # no half-made source left behind
    assert list(worker.store.sources_dir.iterdir()) == []
    monkeypatch.setattr("clip_pipeline.worker.shutil.move", real_move)
    assert len(worker.scan_inbox()) == 1


# ---------------------------------------------------------------- overlapping clips

def test_overlapping_clips_keep_only_the_best():
    rows = [dict(candidate_id="a", start=0, end=40, virality_score=80, status="candidate"),
            dict(candidate_id="b", start=15, end=55, virality_score=90, status="candidate"),   # shares 25 s with a
            dict(candidate_id="c", start=50, end=90, virality_score=75, status="candidate"),   # shares 5 s with b
            dict(candidate_id="d", start=0, end=40, virality_score=60, status="below_threshold")]
    mark_overlaps(rows)
    assert {r["candidate_id"]: r["status"] for r in rows} == {
        "a": "duplicate", "b": "candidate", "c": "candidate", "d": "below_threshold"}


# ---------------------------------------------------------------- cleaning up

def test_owned_file_is_deleted_only_when_every_clip_is_decided(worker, stages):
    drop(worker.s.inbox_dir, "movie.mp4")
    worker.scan_inbox()
    (src,) = worker.scan_inbox()
    assert worker.wait_idle(timeout=20)
    sdir = worker.store.source_dir(src.id)
    original = sdir / "original.mp4"
    keep, toss = worker.store.list_clips(source_id=src.id, status="review")

    assert worker.reject(toss.id)
    assert not (sdir / "slices" / f"{toss.candidate_id}.mp4").exists()         # a rejected clip's cut goes at once
    assert original.exists() and worker.store.get_source(src.id).cleaned_at is None   # one clip still waiting

    worker.approve(keep.id, approve_opts())
    got = worker.store.get_source(src.id)
    assert not original.exists() and got.cleaned_at
    assert (sdir / "slices" / f"{keep.candidate_id}.mp4").exists()             # needed until it is posted
    assert (sdir / worker.store.get_clip(keep.id).render_file).exists()
    assert (sdir / "transcripts.json").exists()
    assert worker.cleanup_source(src.id) is False                              # nothing left to do


def test_downloaded_audio_is_removed_when_a_link_is_decided(worker, stages):
    src, _ = worker.add_url("https://youtu.be/yjLcN0RFYTI")
    assert worker.wait_idle(timeout=20)
    sdir = worker.store.source_dir(src.id)
    assert (sdir / "source" / "audio.opus").exists()
    for clip in worker.store.list_clips(source_id=src.id, status="review"):
        worker.reject(clip.id)
    assert not (sdir / "source").exists() and not (sdir / "audio.done").exists()
    assert worker.store.get_source(src.id).cleaned_at


def test_file_given_by_path_is_never_deleted(worker, stages, tmp_path):
    video = tmp_path / "mine.mp4"
    video.write_bytes(b"keep me")
    src, _ = worker.add_file(video)
    assert worker.wait_idle(timeout=20)
    for clip in worker.store.list_clips(source_id=src.id, status="review"):
        worker.reject(clip.id)
    assert worker.store.get_source(src.id).cleaned_at and video.read_bytes() == b"keep me"


def test_source_with_nothing_to_review_is_cleaned_straight_away(tmp_path, stages):
    w = build(tmp_path)
    w.s = replace(w.s, review_min_score=99)
    drop(w.s.inbox_dir, "dull.mp4")
    w.scan_inbox()
    (src,) = w.scan_inbox()
    assert w.wait_idle(timeout=20)
    w.shutdown()
    got = w.store.get_source(src.id)
    assert got.message == "No clip reached the review score" and got.cleaned_at
    assert not (w.store.source_dir(src.id) / "original.mp4").exists() and stages.calls["cut"] == 0


def test_failed_cut_keeps_the_source_until_it_is_dealt_with(worker, stages):
    stages.fail.add("cut")
    drop(worker.s.inbox_dir, "movie.mp4")
    worker.scan_inbox()
    (src,) = worker.scan_inbox()
    assert worker.wait_idle(timeout=20)
    ok = worker.store.list_clips(source_id=src.id, status="review")[0]
    worker.reject(ok.id)
    assert worker.store.get_source(src.id).cleaned_at is None                  # the failed clip may be retried
    assert (worker.store.source_dir(src.id) / "original.mp4").exists()


# ---------------------------------------------------------------- API

@pytest.fixture
def client(tmp_path, stages):
    app = create_app(replace(get_settings(), data_dir=tmp_path), background=False)
    with TestClient(app) as c:
        c.worker = app.state.worker
        yield c
    app.state.worker.shutdown()


def test_api_link_starts_by_itself_and_is_not_repeated(client):
    r = client.post("/sources", json={"url": "https://youtu.be/yjLcN0RFYTI", "content_type": "film"})
    assert r.status_code == 202 and r.json()["duplicate"] is False
    sid = r.json()["id"]
    assert client.worker.wait_idle(timeout=20)
    body = client.get(f"/sources/{sid}").json()
    assert body["status"] == "ready" and body["content_type"] == "film" and body["clips"] == {"review": 2, "below_threshold": 1}
    assert "file_path" not in body and "fingerprint" not in body
    again = client.post("/sources", json={"url": "https://www.youtube.com/watch?v=yjLcN0RFYTI"}).json()
    assert again == {"id": sid, "duplicate": True}
    assert [s["id"] for s in client.get("/sources").json()["sources"]] == [sid]


def test_api_rejects_bad_links_and_types(client):
    assert client.post("/sources", json={"url": "--exec calc"}).status_code == 422
    assert client.post("/sources", json={"url": "file:///etc/passwd"}).status_code == 422
    assert client.post("/sources", json={"url": "https://x.test/v", "content_type": "cats"}).status_code == 422
    assert client.get("/sources/doesnotexist").status_code == 404
    assert client.get("/sources/..%2F..%2Fetc").status_code == 404


def test_api_upload_goes_to_disk_and_is_queued(client, tmp_path):
    r = client.post("/sources/upload/My%20Clip.mp4?content_type=vlog", content=b"uploaded-bytes")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["size"] == 14 and body["duplicate"] is False
    assert client.worker.wait_idle(timeout=20)
    src = client.worker.store.get_source(body["id"])
    assert src.owns_file and src.content_hint == "vlog" and src.status == "ready"
    assert (client.worker.store.source_dir(src.id) / "original.mp4").read_bytes() == b"uploaded-bytes"
    assert list((tmp_path / "inbox").iterdir()) == []                          # nothing left behind, no .part
    dup = client.post("/sources/upload/again.mp4", content=b"uploaded-bytes").json()
    assert dup["duplicate"] is True and dup["id"] == body["id"]


def test_api_upload_refuses_non_video_empty_and_oversize(client, tmp_path):
    assert client.post("/sources/upload/run.exe", content=b"MZ").status_code == 415
    assert client.post("/sources/upload/a.mp4", content=b"").status_code == 400
    client.worker.s = replace(client.worker.s, max_upload_gb=1e-9)             # ~1 byte
    assert client.post("/sources/upload/big.mp4", content=b"too many bytes").status_code == 413
    assert list((tmp_path / "inbox").iterdir()) == []


# ---------------------------------------------------------------- misc

def test_old_database_is_upgraded_in_place(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(dbmod, "MIGRATIONS", dbmod.MIGRATIONS[:1])
    Database(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sources (id, kind, url, created_at, updated_at) VALUES ('s1', 'url', 'https://x.test', 'a', 'a')")
    conn.commit()
    conn.close()
    monkeypatch.undo()
    db = Database(path)
    assert db.version() == len(dbmod.MIGRATIONS) >= 2
    with db.connect() as c:
        row = c.execute("SELECT owns_file, fingerprint, cleaned_at FROM sources WHERE id = 's1'").fetchone()
    assert tuple(row) == (0, None, None)                                       # existing rows survive with defaults


def test_lowering_priority_reports_success_or_fails_quietly(monkeypatch):
    assert proc.lower_process_priority() is True
    if os.name != "nt":
        monkeypatch.setattr(os, "nice", lambda n: (_ for _ in ()).throw(PermissionError("no")))
        assert proc.lower_process_priority() is False


def test_inbox_file_keeps_the_name_it_arrived_with(worker, stages, monkeypatch):
    # a video with no title of its own: the real probe now reports "", not the name of the moved file
    monkeypatch.setattr("clip_pipeline.sources.local.probe", lambda p: {"title": "", "duration": 600.0, "categories": []})
    drop(worker.s.inbox_dir, "20250221_215937.mp4")
    worker.scan_inbox()
    (src,) = worker.scan_inbox()
    assert src.title == "20250221 215937"
    assert worker.wait_idle(timeout=20)
    assert worker.store.get_source(src.id).title == "20250221 215937"          # not "original"


def test_probe_never_takes_the_title_from_the_file_name(tmp_path):
    av = pytest.importorskip("av")
    import numpy as np
    from clip_pipeline.sources import local

    def make(path, title=None):
        with av.open(str(path), "w") as c:
            if title:
                c.metadata["title"] = title
            v = c.add_stream("mpeg4", rate=10)
            v.width, v.height = 64, 48
            a = c.add_stream("aac", rate=16000)
            for i in range(10):
                c.mux(v.encode(av.VideoFrame.from_ndarray(np.full((48, 64, 3), i * 20, np.uint8), format="rgb24")))
            c.mux(v.encode())
            frame = av.AudioFrame.from_ndarray(np.zeros((1, 16000), np.float32), format="fltp", layout="mono")
            frame.sample_rate = 16000
            c.mux(a.encode(frame))
            c.mux(a.encode())

    make(tmp_path / "original.mp4")
    assert local.probe(tmp_path / "original.mp4")["title"] == ""               # was "original"
    make(tmp_path / "x.mp4", title="  My  Real   Title ")
    assert local.probe(tmp_path / "x.mp4")["title"] == "My Real Title"          # a title in the file itself wins
