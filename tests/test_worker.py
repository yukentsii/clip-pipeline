"""The worker's flow, with every heavy stage (yt-dlp, Whisper, Gemini, ffmpeg) replaced by a counter."""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from clip_pipeline.analysis import audio_analyzer, llm_scorer, transcriber
from clip_pipeline.analysis.audio_analyzer import Candidate
from clip_pipeline.analysis.transcriber import Transcript, Word
from clip_pipeline.config import get_settings
from clip_pipeline.db import Database
from clip_pipeline.publish.queue import PostQueue
from clip_pipeline.render import face_tracker, subtitle_gen, video_renderer
from clip_pipeline.sources import downloader, local
from clip_pipeline.store import Store
from clip_pipeline.publish.queue import PostBusy
from clip_pipeline.worker import StateError, Worker, candidate_count_for

SCORES = {"c00": 92, "c01": 75, "c02": 55}      # c02 is under the review threshold of 70


class Stages:
    """Fakes for every stage; `calls` counts how often each really ran, `fail` makes one raise once."""

    def __init__(self, monkeypatch):
        self.calls = {k: 0 for k in ("download", "analyze", "whisper", "score", "slice", "cut", "render")}
        self.fail: set[str] = set()
        monkeypatch.setattr(downloader, "download_audio", self.download)
        monkeypatch.setattr(audio_analyzer, "analyze", self.analyze)
        monkeypatch.setattr(transcriber, "transcribe_candidates", self.whisper)
        monkeypatch.setattr(llm_scorer, "score_candidates", self.score)
        monkeypatch.setattr(downloader, "download_section", self.section)
        monkeypatch.setattr(local, "cut_section", self.cut)
        monkeypatch.setattr(local, "probe", lambda p: {"title": "Local Movie", "duration": 600.0, "categories": []})
        monkeypatch.setattr(video_renderer, "extract_thumbnail", lambda video, out, at: None)
        monkeypatch.setattr(face_tracker, "analyze_video", lambda *a, **k: SimpleNamespace(to_dict=lambda: {"mode": "fit"}))
        monkeypatch.setattr(subtitle_gen, "write_ass", self.write_ass)
        monkeypatch.setattr(video_renderer, "render", self.render)
        self.ass_kwargs = {}

    def _step(self, name):
        self.calls[name] += 1
        if name in self.fail:
            self.fail.discard(name)
            raise RuntimeError(f"{name} blew up")

    def download(self, url, out_dir, progress=None, **kw):
        self._step("download")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "audio.opus").write_bytes(b"x")
        (out_dir / "audio.info.json").write_text('{"title": "A Stream", "duration": 600, "categories": ["Gaming"]}')

    def analyze(self, audio_path, chat_path, **kw):
        self._step("analyze")
        cands = [Candidate(id=c, start=100.0 * i, end=100.0 * i + 60, peak_time=100.0 * i + 30, score=3.0 - i,
                           audio_score=2.0, chat_score=1.0) for i, c in enumerate(SCORES)]
        return SimpleNamespace(duration=600.0, candidates=cands, summary=lambda: {
            "duration": 600.0, "chat_messages": 0, "candidates": [c.to_dict() for c in cands], "candidate_minutes": 3.0})

    def whisper(self, audio_path, candidates, progress=None):
        self._step("whisper")
        return [Transcript(c.id, c.start, c.end, [Word(c.start + 1, c.start + 1.4, "hello")]) for c in candidates]

    def score(self, candidates, transcripts, **kw):
        self._step("score")
        self.score_kwargs = kw
        return [llm_scorer.ScoredClip(candidate_id=c.id, title=f"Title {c.id}", hook_title="Wait for it",
                                      content_type="film", start_time="0", end_time="0", start=c.start + 5,
                                      end=c.start + 45, hook_score=SCORES[c.id] // 4, engagement_score=SCORES[c.id] // 4,
                                      value_score=SCORES[c.id] // 4, shareability_score=SCORES[c.id] - 3 * (SCORES[c.id] // 4),
                                      model="gemini-test") for c in candidates]

    def section(self, url, start, end, out_path, **kw):
        self._step("slice")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"slice")
        return out_path

    def cut(self, src, start, end, out, **kw):
        self._step("cut")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"slice")
        return out

    def write_ass(self, path, words, start, end, **kw):
        self.ass_kwargs = {"words": list(words), "start": start, "end": end, **kw}
        path.write_text("ass")
        return path

    def render(self, req):
        self._step("render")
        self.render_req = req
        req.output.write_bytes(b"video")
        return req.output, "libx264"


def build(tmp_path) -> Worker:
    db = Database(tmp_path / "w.db")
    s = replace(get_settings(), data_dir=tmp_path, review_min_score=70)
    return Worker(Store(db, tmp_path / "sources"), PostQueue(db), s)


@pytest.fixture
def stages(monkeypatch) -> Stages:
    return Stages(monkeypatch)


@pytest.fixture
def worker(tmp_path):
    w = build(tmp_path)
    yield w
    w.shutdown()


def statuses(worker, source_id):
    return {c.candidate_id: c.status for c in worker.store.list_clips(source_id=source_id)}


def test_url_source_ends_with_clips_waiting_for_review(worker, stages):
    src = worker.store.create_source(url="https://youtu.be/x")
    worker.process(src)
    got = worker.store.get_source(src.id)
    assert (got.status, got.title, got.content_type, got.scoring_model) == ("ready", "A Stream", "film", "gemini-test")
    assert got.message == "2 clips ready for review" and got.candidate_minutes == 3.0
    assert statuses(worker, src.id) == {"c00": "review", "c01": "review", "c02": "below_threshold"}
    assert stages.calls["slice"] == 2                          # nothing is cut for a clip below the threshold
    best = worker.store.list_clips(source_id=src.id)[0]
    assert (best.virality_score, best.slice_file, best.slice_start, best.slice_end) == (92, "slices/c00.mp4", 0.0, 55.0)
    assert stages.score_kwargs["categories"] == ["Gaming"] and stages.score_kwargs["stream_type"] == "gaming"


def test_local_file_is_read_in_place_and_hint_wins(worker, stages, tmp_path):
    video = tmp_path / "My.Movie.2024.mkv"
    video.write_bytes(b"v")
    src, is_new = worker.add_file(video, content_hint="music")
    assert is_new and worker.wait_idle(timeout=20)
    got = worker.store.get_source(src.id)
    assert (got.status, got.kind, got.title, got.content_type) == ("ready", "file", "Local Movie", "music")
    assert stages.calls["download"] == 0 and stages.calls["slice"] == 0 and stages.calls["cut"] == 2
    assert {c.content_type for c in worker.store.list_clips(source_id=src.id)} == {"music"}   # not the LLM's "film"
    assert video.exists() and not got.owns_file                # a file given by path is never moved or deleted


def test_missing_local_file_fails_the_source_and_queue_moves_on(worker, stages, tmp_path):
    gone = worker.store.create_source(file_path=tmp_path / "gone.mp4")
    nxt = worker.store.create_source(url="https://youtu.be/x")
    worker.kick()
    assert worker.wait_idle(timeout=20)
    assert worker.store.get_source(gone.id).status == "failed" and "gone" in worker.store.get_source(gone.id).error
    assert worker.store.get_source(nxt.id).status == "ready"


@pytest.mark.parametrize("failing,rerun", [
    ("analyze", {"download": 1, "analyze": 2, "whisper": 1, "score": 1}),
    ("whisper", {"download": 1, "analyze": 1, "whisper": 2, "score": 1}),
    ("score",   {"download": 1, "analyze": 1, "whisper": 1, "score": 2}),
    ("slice",   {"download": 1, "analyze": 1, "whisper": 1, "score": 1}),
])
def test_restart_resumes_from_the_stage_that_was_interrupted(tmp_path, stages, failing, rerun):
    first = build(tmp_path)
    src = first.store.create_source(url="https://youtu.be/x")
    stages.fail.add(failing)
    if failing == "slice":
        first.process(src)                                     # a failed cut fails that clip, not the source
        assert sorted(statuses(first, src.id).values()) == ["below_threshold", "failed", "review"]
    else:
        with pytest.raises(RuntimeError):
            first.process(src)                                 # the process "dies" mid-stage, status stays running
        assert first.store.get_source(src.id).status == "running"
    first.shutdown()

    second = build(tmp_path)                                   # same database and files, fresh process
    second.start()
    assert second.wait_idle(timeout=20)
    second.shutdown()
    assert second.store.get_source(src.id).status == "ready"
    for stage, count in rerun.items():
        assert stages.calls[stage] == count, stage             # finished stages were not repeated
    assert len(second.store.list_clips(source_id=src.id)) == 3  # and clips were not inserted twice


def test_interrupted_slice_of_a_finished_source_is_redone_at_start(tmp_path, stages):
    first = build(tmp_path)
    src = first.store.create_source(url="https://youtu.be/x")
    first.process(src)
    clip = first.store.list_clips(source_id=src.id, status="review")[0]
    first.store.update_clip(clip.id, status="slicing", slice_file=None)   # crashed while cutting this one
    first.shutdown()

    second = build(tmp_path)
    assert second.start() == {"sources": 0, "clips": 1}
    second.finish_background()
    assert second.store.get_clip(clip.id).status == "review" and stages.calls["slice"] == 3   # cut again, then rendered


def approve_opts(**kw):
    return {"platforms": ["youtube", "tiktok"], "scheduled_at": datetime(2030, 5, 1, 16, 30, tzinfo=timezone.utc), **kw}


def reviewed(worker, stages):
    """A processed source and its best clip, waiting for review."""
    src = worker.store.create_source(url="https://youtu.be/x")
    worker.process(src)
    return src, worker.store.list_clips(source_id=src.id, status="review")[0]      # c00: 5-45 s, slice 0-55 s


def test_clips_are_rendered_before_review_with_their_saved_settings(worker, stages):
    worker.store.set_prefs({"default_layout": "fit", "default_subtitle_style": "static"})
    src, clip = reviewed(worker, stages)
    assert stages.calls["render"] == 2 and worker.store.get_source(src.id).stage == "render"
    assert (clip.aspect, clip.layout, clip.subtitle_style, clip.encoder) == ("9:16", "fit", "static", "libx264")
    sdir = worker.store.source_dir(src.id)
    assert clip.render_file.startswith("renders/c00/c00_9x16_") and (sdir / clip.render_file).read_bytes() == b"video"
    # the default trim is the scored window, and the clip's own hook title and words are burned in
    assert (stages.render_req.trim_start, stages.render_req.trim_end) in ((5.0, 45.0), (105.0 - 95.0, 145.0 - 95.0))
    assert stages.ass_kwargs["hook_title"] == "Wait for it" and stages.ass_kwargs["style"] == "static"
    assert [w.text for w in stages.ass_kwargs["words"]] == ["hello"]


def test_approve_schedules_the_reviewed_render_without_rendering_again(worker, stages):
    src, clip = reviewed(worker, stages)
    renders = stages.calls["render"]
    post_id = worker.approve(clip.id, approve_opts(title="Edited title", tags=["x"]))

    got = worker.store.get_clip(clip.id)
    assert (got.status, got.post_id, got.title, got.tags) == ("scheduled", post_id, "Edited title", ["x"])
    assert stages.calls["render"] == renders                   # approving is instant: the file already exists
    post = worker.queue.get_post(post_id)
    assert post["title"] == "Edited title" and post["tags"] == ["x"] and post["description"] == ""
    assert post["platforms"] == ["youtube", "tiktok"] and post["scheduled_at"] == "2030-05-01T16:30:00Z"
    assert post["clip_id"] == clip.id and Path(post["file_path"]).read_bytes() == b"video"
    assert post["extra"] == {"source_url": "https://youtu.be/x", "virality_score": 92, "visibility": None}
    with pytest.raises(StateError):
        worker.approve(clip.id, approve_opts())                # a second tap can't schedule it twice
    assert len(worker.queue.list_posts()) == 1


def test_text_edits_are_saved_without_rendering(worker, stages):
    _, clip = reviewed(worker, stages)
    renders = stages.calls["render"]
    assert worker.edit_clip(clip.id, {"title": "New", "description": "d", "tags": ["a"], "layout": clip.layout}) == "review"
    got = worker.store.get_clip(clip.id)
    assert (got.title, got.description, got.tags, got.status) == ("New", "d", ["a"], "review")
    assert stages.calls["render"] == renders


def test_picture_edits_rerender_and_return_to_review(worker, stages):
    src, clip = reviewed(worker, stages)
    old_file = worker.store.source_dir(src.id) / clip.render_file
    status = worker.edit_clip(clip.id, {"start": 1.0, "end": 500.0, "layout": "crop", "hook_title": "", "subtitle_style": "none"})
    assert status == "sliced"
    worker.render_pool.shutdown(wait=True)
    got = worker.store.get_clip(clip.id)
    assert (got.status, got.start, got.end, got.layout, got.hook_title) == ("review", 1.0, 55.0, "crop", "")
    assert (stages.render_req.trim_start, stages.render_req.trim_end) == (1.0, 55.0)   # clamped to the cut
    assert stages.render_req.subtitles is None                 # no captions and no title: nothing burned in
    assert got.render_file != clip.render_file and not old_file.exists()   # new name, old file gone


def test_edit_validation_and_state(worker, stages):
    _, clip = reviewed(worker, stages)
    with pytest.raises(ValueError, match="at least 3 seconds"):
        worker.edit_clip(clip.id, {"start": 10.0, "end": 11.0})
    with pytest.raises(ValueError, match="layout must be one of"):
        worker.edit_clip(clip.id, {"layout": "zoom"})
    with pytest.raises(ValueError, match="cannot change"):
        worker.edit_clip(clip.id, {"status": "scheduled"})
    assert worker.store.get_clip(clip.id).status == "review"
    worker.approve(clip.id, approve_opts())
    with pytest.raises(StateError):
        worker.edit_clip(clip.id, {"title": "too late"})


def test_failed_render_is_reported_and_can_be_retried(worker, stages):
    stages.fail.add("render")
    src = worker.store.create_source(url="https://youtu.be/x")
    worker.process(src)
    assert worker.store.get_source(src.id).status == "ready"   # one bad clip does not fail the source
    failed = worker.store.list_clips(source_id=src.id, status="failed")
    assert len(failed) == 1 and "Render failed: render blew up" in failed[0].error
    assert worker.retry_clip(failed[0].id) == "sliced"         # the cut is fine, only render again
    worker.render_pool.shutdown(wait=True)
    assert worker.store.get_clip(failed[0].id).status == "review" and stages.calls["slice"] == 2
    with pytest.raises(StateError):
        worker.retry_clip(failed[0].id)


def test_failed_source_can_be_retried(worker, stages):
    stages.fail.add("whisper")
    src, _ = worker.add_url("https://youtu.be/yjLcN0RFYTI")
    assert worker.wait_idle(timeout=20) and worker.store.get_source(src.id).status == "failed"
    worker.retry_source(src.id)
    assert worker.wait_idle(timeout=20)
    got = worker.store.get_source(src.id)
    assert got.status == "ready" and got.error is None and stages.calls["analyze"] == 1
    with pytest.raises(StateError):
        worker.retry_source(src.id)


def test_only_clips_in_review_can_be_approved_or_rejected(worker, stages):
    src, clip = reviewed(worker, stages)
    low = next(c for c in worker.store.list_clips(source_id=src.id) if c.status == "below_threshold")
    assert not worker.reject(low.id)
    with pytest.raises(StateError):
        worker.approve(low.id, approve_opts())
    sdir = worker.store.source_dir(src.id)
    assert worker.reject(clip.id) and not worker.reject(clip.id)
    assert not (sdir / "renders" / "c00").exists() and not (sdir / "slices" / "c00.mp4").exists()
    with pytest.raises(StateError):
        worker.approve(clip.id, approve_opts())
    assert worker.queue.list_posts() == []


def test_cancel_before_anything_posted_returns_the_clip_to_review(worker, stages):
    _, clip = reviewed(worker, stages)
    post_id = worker.approve(clip.id, approve_opts())
    result = worker.cancel_post(post_id)
    assert result["deleted"] and sorted(result["cancelled"]) == ["tiktok", "youtube"] and result["kept"] == []
    got = worker.store.get_clip(clip.id)
    assert (got.status, got.post_id) == ("review", None) and worker.queue.get_post(post_id) is None
    assert worker.approve(clip.id, approve_opts())             # and it can be scheduled again
    with pytest.raises(KeyError):
        worker.cancel_post(post_id)


def test_cancel_keeps_what_was_already_posted_and_waits_for_running_uploads(worker, stages):
    _, clip = reviewed(worker, stages)
    post_id = worker.approve(clip.id, approve_opts(scheduled_at=datetime(2020, 1, 1, tzinfo=timezone.utc)))
    yt, tt = worker.queue.claim_due()                          # both uploads start
    with pytest.raises(PostBusy):
        worker.cancel_post(post_id)
    assert worker.store.get_clip(clip.id).status == "scheduled"            # nothing changed
    worker.queue.mark_completed(yt["target_id"], "vid", "https://youtube.com/shorts/vid")
    worker.queue.mark_failed(tt["target_id"], "captcha", retryable=False, max_attempts=3, backoff_seconds=60)
    result = worker.cancel_post(post_id)
    assert not result["deleted"] and result["cancelled"] == ["tiktok"] and result["kept"] == ["youtube"]
    assert worker.queue.get_post(post_id)["status"] == "completed"
    assert worker.store.get_clip(clip.id).status == "scheduled"            # it did go out on YouTube


def test_preferences_are_validated_and_used_for_new_sources(worker, stages):
    assert worker.prefs()["review_min_score"] == 70
    for bad in ({"review_min_score": 101}, {"review_min_score": "high"}, {"default_aspect": "4:3"}, {"volume": 11}):
        with pytest.raises(ValueError):
            worker.set_prefs(bad)
    assert worker.set_prefs({"review_min_score": 95})["review_min_score"] == 95
    src = worker.store.create_source(url="https://youtu.be/x")
    worker.process(src)
    assert set(statuses(worker, src.id).values()) == {"below_threshold"} and stages.calls["render"] == 0


def test_wait_idle_waits_for_the_bookkeeping_after_a_source_is_marked_ready(worker, stages, monkeypatch):
    import time as real_time

    real_add = worker.store.add_event

    def slow_event(*a, **kw):
        real_time.sleep(0.6)                                   # the source is already "ready" at this point
        return real_add(*a, **kw)

    monkeypatch.setattr(worker.store, "add_event", slow_event)
    worker.add_url("https://youtu.be/yjLcN0RFYTI")
    assert worker.wait_idle(timeout=20)
    assert [e["kind"] for e in worker.store.list_events()] == ["clips_ready"]   # not returned early


def test_candidate_count_scales_with_duration():
    assert candidate_count_for(58 * 60, 25) == 14
    assert candidate_count_for(5 * 3600, 25) == 25
    assert candidate_count_for(300, 25) == 4
    assert candidate_count_for(None, 25) == 25
