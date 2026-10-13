from pathlib import Path
from types import SimpleNamespace

import pytest

from clip_pipeline.render import video_renderer as vr
from clip_pipeline.render.face_tracker import CropSegment, LayoutPlan, Region


def track_plan():
    return LayoutPlan("track", 1920, 1080, 80.0,
                      [CropSegment(0, 20, 0.3), CropSegment(20, 45, 0.7), CropSegment(45, 80, 0.5)])


def test_piecewise_expr_escapes_commas():
    assert vr.piecewise_expr([5.0], [100, 200]) == "if(lt(t\\,5.000)\\,100\\,200)"
    assert vr.piecewise_expr([], [42]) == "42"


def test_track_filter_offsets_segments_by_trim():
    f = vr.track_crop_filter(track_plan().segments, 1920, 1080, "9:16", offset=10.0, duration=40.0)
    assert f.startswith("crop=w=606:h=1080:x=if(lt(t\\,10.000)")   # 20 s boundary - 10 s trim
    assert "\\,35.000)" in f                                       # 45 s boundary - 10 s trim
    assert ":y=0" in f


def test_crop_x_clamped_inside_frame():
    plan = LayoutPlan("track", 1920, 1080, 10.0, [CropSegment(0, 10, 0.99)])
    f = vr.track_crop_filter(plan.segments, 1920, 1080, "9:16", 0, 10)
    assert ":x=1314:" in f                                         # 1920 - 606


def test_split_graph_stacks_two_panels():
    plan = LayoutPlan("split", 1920, 1080, 30, top=Region(0, 0, 0.3, 1), bottom=Region(0.6, 0, 0.3, 1))
    g = vr.build_filtergraph(plan, "9:16", offset=0, duration=30, subtitles=True)
    assert "split=2[a][b]" in g and "scale=1080:960" in g
    assert g.endswith("vstack=inputs=2,subtitles=captions.ass[vout]")


def test_facecam_graph_heights_sum_to_1920():
    plan = LayoutPlan("facecam", 1920, 1080, 30, top=Region(0.7, 0.6, 0.3, 0.3), bottom=Region(0.2, 0, 0.6, 1))
    g = vr.build_filtergraph(plan, "9:16", offset=0, duration=30, subtitles=False)
    assert "scale=1080:672" in g and "scale=1080:1248" in g and "subtitles" not in g


def test_landscape_passthrough_scales_and_pads():
    plan = LayoutPlan("center", 1920, 1080, 30, [CropSegment(0, 30, 0.5)])
    g = vr.build_filtergraph(plan, "16:9", offset=0, duration=30, subtitles=False)
    assert "pad=1920:1080" in g


def test_fit_graph_puts_whole_frame_over_blurred_copy():
    plan = LayoutPlan("fit", 1920, 1080, 30, [CropSegment(0, 30, 0.5, 0.5, "fit")])
    g = vr.build_filtergraph(plan, "9:16", offset=0, duration=30, subtitles=True)
    assert g.startswith("[0:v]split=2[fg0][bg0];") and "boxblur" in g
    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in g and "crop=w=" not in g
    assert g.endswith("overlay=(W-w)/2:(H-h)/2,subtitles=captions.ass[vout]")


def test_mixed_graph_enables_crop_only_for_face_shots():
    plan = LayoutPlan("track", 1920, 1080, 40, [CropSegment(0, 12, 0.3), CropSegment(12, 25, 0.5, 0.5, "fit"),
                                                CropSegment(25, 40, 0.7)])
    g = vr.build_filtergraph(plan, "9:16", offset=5.0, duration=30.0, subtitles=False)
    assert "split=3[fg0][bg0][c0]" in g and "[fitted][cropped]overlay=0:0:enable=" in g
    assert "gte(t\\,0.000)*lt(t\\,7.000)+gte(t\\,20.000)*lt(t\\,30.000)" in g   # shifted by trim, clamped
    # a trim that only covers face shots needs no fit branch at all
    assert "boxblur" not in vr.build_filtergraph(plan, "9:16", offset=0, duration=10, subtitles=False)


def test_black_bars_are_cropped_before_any_layout():
    bars = "[0:v]crop=w=1920:h=800:x=0:y=140[pic];[pic]"
    track = LayoutPlan("track", 1920, 800, 30, [CropSegment(0, 30, 0.5)], src_crop=(0, 140, 1920, 800))
    g = vr.build_filtergraph(track, "9:16", offset=0, duration=30, subtitles=False)
    assert g.startswith(bars + "crop=w=450:h=800:") and g.count("[0:v]") == 1   # 9:16 of the picture, not the frame
    fit = LayoutPlan("fit", 1920, 800, 30, [CropSegment(0, 30, 0.5, 0.5, "fit")], src_crop=(0, 140, 1920, 800))
    assert vr.build_filtergraph(fit, "9:16", offset=0, duration=30, subtitles=False).startswith(bars + "split=2")
    wide = LayoutPlan("center", 1920, 800, 30, [CropSegment(0, 30, 0.5)], src_crop=(0, 140, 1920, 800))
    assert vr.build_filtergraph(wide, "16:9", offset=0, duration=30, subtitles=False).startswith(bars + "scale=1920:1080")


def test_audio_is_loudness_normalised_unless_disabled(tmp_path):
    req = vr.RenderRequest(tmp_path / "s.mp4", tmp_path / "o.mp4", 0, 30, "9:16", track_plan())
    cmd = vr.build_render_cmd(req, "libx264", "ffmpeg")
    assert cmd[cmd.index("-af") + 1] == "loudnorm=I=-14:TP=-1.5:LRA=11"
    assert cmd.index("-af") < cmd.index("-c:a")
    req.loudnorm = False
    assert "-af" not in vr.build_render_cmd(req, "libx264", "ffmpeg")


def test_render_cmd_is_safe_list(tmp_path):
    req = vr.RenderRequest(source=tmp_path / "slice.mp4", output=tmp_path / "out.mp4", trim_start=10, trim_end=55,
                           aspect="1:1", plan=track_plan(), subtitles=tmp_path / "captions.ass")
    cmd = vr.build_render_cmd(req, "h264_amf", "ffmpeg")
    assert cmd[cmd.index("-ss") + 1] == "10.000" and cmd[cmd.index("-t") + 1] == "45.000"
    assert cmd[cmd.index("-c:v") + 1] == "h264_amf"
    assert "scale=1080:1080" in cmd[cmd.index("-filter_complex") + 1]
    assert all(isinstance(a, str) for a in cmd)


def test_vaapi_uploads_frames_to_the_gpu_as_the_last_step(tmp_path):
    req = vr.RenderRequest(tmp_path / "s.mp4", tmp_path / "o.mp4", 5, 35, "9:16", track_plan(), tmp_path / "captions.ass")
    cmd = vr.build_render_cmd(req, "h264_vaapi", "ffmpeg")
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.endswith("subtitles=captions.ass[vout];[vout]format=nv12,hwupload[venc]")   # captions burned first
    assert cmd[cmd.index("-map") + 1] == "[venc]" and cmd[cmd.index("-c:v") + 1] == "h264_vaapi"
    assert cmd.index("-vaapi_device") < cmd.index("-i")                                        # device before input
    plain = vr.build_render_cmd(req, "libx264", "ffmpeg")
    assert plain[plain.index("-map") + 1] == "[vout]" and "-vaapi_device" not in plain

def test_render_falls_back_to_libx264(monkeypatch, tmp_path):
    out = tmp_path / "out.mp4"
    subs = tmp_path / "captions.ass"
    subs.write_text("x")
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        enc = cmd[cmd.index("-c:v") + 1]
        if enc == "libx264":
            out.write_bytes(b"video")
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=1, stderr="AMF init failed")

    monkeypatch.setattr(vr, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(vr, "preferred_encoders", lambda: ["h264_amf", "libx264"])
    monkeypatch.setattr(vr.subprocess, "run", fake_run)
    req = vr.RenderRequest(tmp_path / "s.mp4", out, 0, 30, "9:16", track_plan(), subs)
    path, encoder = vr.render(req)
    assert encoder == "libx264" and path == out
    assert len(calls) == 2
    assert all(kw["cwd"] == str(tmp_path) and not kw.get("shell") for _, kw in calls)


def test_render_raises_when_all_encoders_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(vr, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(vr, "preferred_encoders", lambda: ["libx264"])
    monkeypatch.setattr(vr.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1, stderr="boom"))
    with pytest.raises(vr.RenderError, match="boom"):
        vr.render(vr.RenderRequest(tmp_path / "s.mp4", tmp_path / "o.mp4", 0, 5, "9:16", track_plan()))


def test_subtitle_name_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(vr, "ffmpeg_binary", lambda: "ffmpeg")
    with pytest.raises(ValueError):
        vr.render(vr.RenderRequest(tmp_path / "s.mp4", tmp_path / "o.mp4", 0, 5, "9:16", track_plan(),
                                   tmp_path / "evil'name.ass"))


def test_thumbnail_and_probe_with_opencv(tmp_path):
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    path = tmp_path / "v.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 180))
    if not writer.isOpened():
        pytest.skip("no mp4 writer available")
    for i in range(30):
        writer.write(np.full((180, 320, 3), i * 8, dtype=np.uint8))
    writer.release()
    w, h, fps, dur = vr.probe_video(path)
    assert (w, h) == (320, 180) and dur == pytest.approx(3.0, abs=0.2)
    thumb = vr.extract_thumbnail(path, tmp_path / "t.jpg", at=1.0)
    assert thumb and Path(thumb).stat().st_size > 0
