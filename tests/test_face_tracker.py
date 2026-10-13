import numpy as np
import pytest

from clip_pipeline.render import face_tracker as ft
from clip_pipeline.render.face_tracker import FaceBox, Sample


def face_at(cx, cy=0.4, w=0.12, h=0.2):
    return FaceBox(cx - w / 2, cy - h / 2, w, h, 0.9)


def samples(fn, seconds=20, fps=4):
    return [Sample(i / fps, fn(i / fps)) for i in range(seconds * fps)]


def test_crop_size_for_each_aspect():
    assert ft.crop_size(1920, 1080, "9:16") == (606, 1080)
    assert ft.crop_size(1920, 1080, "1:1") == (1080, 1080)
    assert ft.crop_size(1920, 1080, "16:9") == (1920, 1080)
    w, h = ft.crop_size(1080, 1920, "1:1")  # vertical source
    assert (w, h) == (1080, 1080)


def test_track_ignores_jitter_inside_deadzone():
    rng = np.random.default_rng(0)
    ss = samples(lambda t: [face_at(0.5 + rng.uniform(-0.03, 0.03))])
    segs = ft.segment_track(ss, 20.0)
    assert len(segs) == 1 and segs[0].cx == pytest.approx(0.5, abs=0.04)


def test_track_cuts_when_subject_moves_and_holds():
    ss = samples(lambda t: [face_at(0.25 if t < 10 else 0.75)])
    segs = ft.segment_track(ss, 20.0)
    assert len(segs) == 2
    assert segs[0].cx == pytest.approx(0.25) and segs[1].cx == pytest.approx(0.75)
    assert segs[1].start == pytest.approx(10.0)
    assert segs[-1].end == 20.0


def test_reframe_snaps_to_detected_scene_cut():
    # faces sampled every 0.25 s; the editor's cut is at 9.87 s, between samples 9.75 and 10.0
    ss = samples(lambda t: [face_at(0.25 if t < 9.87 else 0.75)])
    segs = ft.segment_track(ss, 20.0, cuts=[3.1, 9.87, 15.2])
    assert len(segs) == 2 and segs[1].start == pytest.approx(9.87)
    # without cut info it falls back to the first deviating sample
    assert ft.segment_track(ss, 20.0)[1].start == pytest.approx(10.0)


def test_sample_faces_detects_scene_cuts(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    path = tmp_path / "cuts.mp4"
    wr = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (160, 90))
    if not wr.isOpened():
        pytest.skip("no mp4 writer")
    for i in range(40):                          # hard cut from dark to bright at frame 20 (2.0 s)
        wr.write(np.full((90, 160, 3), 30 if i < 20 else 220, np.uint8))
    wr.release()
    monkeypatch.setattr(ft, "ensure_face_model", lambda: None)   # no detector needed for cut detection
    s, w, h, dur, cuts, box = ft.sample_faces(path)
    assert (w, h) == (160, 90) and cuts == [pytest.approx(2.0)] and box is None


def profile(n, lead, trail, bright=90.0):
    p = np.full(n, bright, np.float32)
    p[:lead] = 1.0
    p[n - trail:] = 1.0 if trail else p[n - trail:]
    return p


def test_content_box_removes_letterbox_and_pillarbox():
    flat_rows, flat_cols = profile(1080, 0, 0), profile(1920, 0, 0)
    assert ft.content_box(flat_rows, flat_cols, 1920, 1080) is None
    # 2.39:1 film in a 16:9 frame: 138 px bars, plus a 2 px shave on each edge, kept even
    assert ft.content_box(profile(1080, 138, 138), flat_cols, 1920, 1080) == (0, 140, 1920, 800)
    # 4:3 picture pillarboxed in 16:9
    assert ft.content_box(flat_rows, profile(1920, 240, 240), 1920, 1080) == (242, 0, 1436, 1080)
    assert ft.content_box(None, None, 1920, 1080) is None


def test_content_box_is_conservative():
    cols = profile(1920, 0, 0)
    # dark sky on top only: not a bar pair, leave the frame alone
    assert ft.content_box(profile(1080, 200, 0), cols, 1920, 1080) is None
    # subtitles sit in the bottom bar, so only the matching part of the top bar goes
    assert ft.content_box(profile(1080, 138, 60), cols, 1920, 1080) == (0, 62, 1920, 956)
    # a sliver is not worth a crop, and a mostly black frame is a dark scene
    assert ft.content_box(profile(1080, 8, 8), cols, 1920, 1080) is None
    assert ft.content_box(profile(1080, 400, 400), cols, 1920, 1080) is None


def test_sample_faces_finds_letterbox_bars(tmp_path, monkeypatch):
    cv2 = pytest.importorskip("cv2")
    path = tmp_path / "bars.mp4"
    wr = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 180))
    if not wr.isOpened():
        pytest.skip("no mp4 writer")
    for i in range(30):
        frame = np.zeros((180, 320, 3), np.uint8)
        frame[24:156] = 60 + i * 4                           # picture between two 24 px bars
        wr.write(frame)
    wr.release()
    monkeypatch.setattr(ft, "ensure_face_model", lambda: pytest.fail("detector must not load"))
    *_, box = ft.sample_faces(path, detect_faces=False)
    x, y, w, h = box
    assert (x, w) == (0, 320) and y == pytest.approx(26, abs=4) and y + h == pytest.approx(154, abs=4)


def test_analyze_video_plans_inside_the_bars(monkeypatch):
    faces = samples(lambda t: [FaceBox(0.45, 0.40, 0.10, 0.20, 0.9)])       # centred in the full frame
    seen = {}

    def fake_sample(path, fps, *, detect_faces=True):
        seen["detect"] = detect_faces
        return (faces if detect_faces else []), 1920, 1080, 20.0, [], (0, 140, 1920, 800)

    monkeypatch.setattr(ft, "sample_faces", fake_sample)
    plan = ft.analyze_video("x.mp4", aspect="9:16", content_type="film")
    assert plan.src_crop == (0, 140, 1920, 800) and (plan.src_w, plan.src_h) == (1920, 800)
    assert plan.mode == "track" and plan.segments[0].cx == pytest.approx(0.5)
    assert plan.segments[0].cy == pytest.approx((0.5 * 1080 - 140) / 800)   # same face, picture coordinates
    forced = ft.analyze_video("x.mp4", layout="fit")
    assert forced.mode == "fit" and seen["detect"] is False                  # no face pass for a forced fit


def test_user_framing_choice_overrides_the_planner():
    half = samples(lambda t: [face_at(0.3)] if t < 10 else [])
    assert ft.plan_layout(half, 1920, 1080, 20.0, layout="fit").mode == "fit"
    crop = ft.plan_layout(half, 1920, 1080, 20.0, layout="crop")
    assert crop.mode == "track" and all(s.kind == "crop" for s in crop.segments)
    assert ft.plan_layout(samples(lambda t: []), 1920, 1080, 20.0, layout="crop").mode == "center"
    auto = ft.plan_layout(half, 1920, 1080, 20.0)
    assert [s.kind for s in auto.segments] == ["crop", "fit"]


def test_track_ignores_brief_glances():
    ss = samples(lambda t: [face_at(0.8 if 5 <= t < 5.5 else 0.3)])
    assert len(ft.segment_track(ss, 20.0)) == 1


def test_no_faces_is_blurred_fit_plan():
    plan = ft.plan_layout(samples(lambda t: []), 1920, 1080, 20.0)
    assert plan.mode == "fit" and [s.kind for s in plan.segments] == ["fit"]
    assert (plan.segments[0].start, plan.segments[0].end) == (0.0, 20.0)


def test_no_detector_stays_center_crop():
    plan = ft.plan_layout([], 1920, 1080, 20.0)             # no samples = we never looked for faces
    assert plan.mode == "center" and plan.segments[0].kind == "crop"


def test_faceless_shot_becomes_fit_segment_snapped_to_cut():
    ss = samples(lambda t: [face_at(0.3)] if t < 10 else [])
    plan = ft.plan_layout(ss, 1920, 1080, 20.0, cuts=[10.1])
    assert plan.mode == "track"
    assert [(s.kind, s.start, s.end) for s in plan.segments] == [("crop", 0.0, 10.1), ("fit", 10.1, 20.0)]
    assert plan.segments[0].cx == pytest.approx(0.3)


def test_brief_face_dropout_does_not_switch_layout():
    ss = samples(lambda t: [] if 8 <= t < 9.5 else [face_at(0.4)])
    plan = ft.plan_layout(ss, 1920, 1080, 20.0)
    assert plan.mode == "track" and all(s.kind == "crop" for s in plan.segments)


def test_fit_skipped_when_crop_loses_little():
    plan = ft.plan_layout(samples(lambda t: []), 1080, 1920, 20.0, aspect="9:16")   # already vertical
    assert plan.mode == "center"


def test_film_never_gets_facecam_layout():
    ss = samples(lambda t: [FaceBox(0.82, 0.72, 0.08, 0.13, 0.9)])
    assert ft.plan_layout(ss, 1920, 1080, 20.0, content_type="film").mode == "track"


def test_podcast_two_speakers_gives_split():
    ss = samples(lambda t: [face_at(0.28), face_at(0.72)])
    plan = ft.plan_layout(ss, 1920, 1080, 20.0, aspect="9:16", content_type="podcast")
    assert plan.mode == "split"
    assert plan.top.x + plan.top.w / 2 == pytest.approx(0.28, abs=0.02)
    assert plan.bottom.x + plan.bottom.w / 2 == pytest.approx(0.72, abs=0.02)
    # panel aspect must equal 1080x960
    assert (plan.top.w * 1920) / (plan.top.h * 1080) == pytest.approx(1080 / 960, rel=0.01)


def test_gaming_facecam_detected_in_corner():
    ss = samples(lambda t: [FaceBox(0.82, 0.72, 0.08, 0.13, 0.9)])
    plan = ft.plan_layout(ss, 1920, 1080, 20.0, aspect="9:16", content_type="gaming")
    assert plan.mode == "facecam"
    assert plan.top.x <= 0.82 and plan.top.x + plan.top.w >= 0.90
    for r in (plan.top, plan.bottom):
        assert 0 <= r.x and r.x + r.w <= 1.0001 and 0 <= r.y and r.y + r.h <= 1.0001


def test_big_centered_face_in_gaming_is_tracked_not_facecam():
    ss = samples(lambda t: [face_at(0.5, w=0.25, h=0.4)])
    plan = ft.plan_layout(ss, 1920, 1080, 20.0, aspect="9:16", content_type="gaming")
    assert plan.mode == "track"


def test_landscape_output_from_landscape_source_is_passthrough():
    plan = ft.plan_layout(samples(lambda t: [face_at(0.2)]), 1920, 1080, 20.0, aspect="16:9")
    assert plan.mode == "center"


def test_panel_region_clamped_to_frame():
    r = ft.panel_region(0.02, 0.5, 1920, 1080, 1.125)
    assert r.x == 0 and r.x + r.w <= 1
