from dataclasses import replace
from types import SimpleNamespace

import pytest

from clip_pipeline.config import get_settings
from clip_pipeline.sources import local


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    s = replace(get_settings(), data_dir=tmp_path)
    monkeypatch.setattr(local, "get_settings", lambda: s)
    return tmp_path / "inbox"


def test_inbox_lists_only_finished_videos_newest_first(inbox):
    import os

    assert local.list_inbox() == [] and inbox.is_dir()         # created on first use
    for i, name in enumerate(["old.mp4", "new.MKV", "notes.txt", "half.mp4.part"]):
        (inbox / name).write_bytes(b"x" * (i + 1))
        os.utime(inbox / name, (1000 + i, 1000 + i))
    assert [(f.name, f.size) for f in local.list_inbox()] == [("new.MKV", 2), ("old.mp4", 1)]


@pytest.mark.parametrize("name", ["../secret.mp4", "sub/dir.mp4", "C:\\Windows\\x.mp4", "", "..", "a\x00.mp4"])
def test_resolve_refuses_anything_path_like(inbox, name):
    with pytest.raises(local.LocalSourceError):
        local.resolve_inbox_file(name)


def test_resolve_needs_an_existing_video(inbox):
    inbox.mkdir()
    (inbox / "clip.mp4").write_bytes(b"x")
    (inbox / "doc.pdf").write_bytes(b"x")
    assert local.resolve_inbox_file("clip.mp4") == inbox / "clip.mp4"
    for bad in ("doc.pdf", "missing.mp4"):
        with pytest.raises(local.LocalSourceError):
            local.resolve_inbox_file(bad)


def test_safe_filename_and_collision_free_path(inbox):
    assert local.safe_filename('C:\\evil\\..\\my:"movie"?.MP4') == "my__movie__.mp4"
    assert local.safe_filename("  ...  .mkv") == "video.mkv"
    assert local.safe_filename("/etc/cron.d/clip.mkv") == "clip.mkv"            # either slash, on any OS
    with pytest.raises(local.LocalSourceError):
        local.safe_filename("script.exe")
    first = local.unused_inbox_path("a.mp4")
    first.write_bytes(b"x")
    (inbox / "a (2).mp4.part").write_bytes(b"x")               # an upload in progress also holds its name
    assert first.name == "a.mp4" and local.unused_inbox_path("a.mp4").name == "a (3).mp4"


def test_title_from_filename():
    assert local.title_from_filename("My.Movie_2024.1080p.mkv") == "My Movie 2024 1080p"


def test_cut_cmd_is_frame_accurate_reencode(tmp_path):
    cmd = local.build_cut_cmd(tmp_path / "in.mkv", 12.5, 72.5, tmp_path / "out.mp4", "libx264")
    assert cmd[cmd.index("-ss") + 1] == "12.500" and cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-t") + 1] == "60.000" and cmd[cmd.index("-c:v") + 1] == "libx264"
    assert "0:a:0?" in cmd and "-sn" in cmd and all(isinstance(a, str) for a in cmd)
    with pytest.raises(ValueError):
        local.build_cut_cmd(tmp_path / "in.mkv", 10, 10, tmp_path / "o.mp4", "libx264")
    vaapi = local.build_cut_cmd(tmp_path / "in.mkv", 0, 30, tmp_path / "out.mp4", "h264_vaapi")
    assert vaapi[vaapi.index("-vf") + 1].endswith(",format=nv12,hwupload") and vaapi.index("-vaapi_device") < vaapi.index("-i")


def test_cut_falls_back_to_next_encoder_and_reports_failure(tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"x")
    out = tmp_path / "s" / "c00.mp4"
    tried = []

    def fake_run(cmd, **kw):
        enc = cmd[cmd.index("-c:v") + 1]
        tried.append(enc)
        if enc == "libx264":
            out.write_bytes(b"video")
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=1, stderr="AMF init failed")

    monkeypatch.setattr(local, "ffmpeg_binary", lambda: "ffmpeg")
    monkeypatch.setattr(local, "preferred_encoders", lambda: ["h264_amf", "libx264"])
    monkeypatch.setattr(local.subprocess, "run", fake_run)
    assert local.cut_section(src, 0, 30, out) == out and tried == ["h264_amf", "libx264"]

    monkeypatch.setattr(local, "preferred_encoders", lambda: ["h264_amf"])
    with pytest.raises(local.LocalSourceError, match="AMF init failed"):
        local.cut_section(src, 0, 30, tmp_path / "s" / "c01.mp4")
    with pytest.raises(local.LocalSourceError, match="gone"):
        local.cut_section(tmp_path / "missing.mp4", 0, 30, out)


def test_probe_reads_duration_and_requires_audio(tmp_path):
    av = pytest.importorskip("av")
    import numpy as np

    def make(path, with_audio):
        with av.open(str(path), "w") as c:
            v = c.add_stream("mpeg4", rate=10)
            v.width, v.height = 64, 48
            a = c.add_stream("aac", rate=16000) if with_audio else None
            for i in range(20):
                c.mux(v.encode(av.VideoFrame.from_ndarray(np.full((48, 64, 3), i * 10, np.uint8), format="rgb24")))
            c.mux(v.encode())
            if a is not None:
                frame = av.AudioFrame.from_ndarray(np.zeros((1, 32000), np.float32), format="fltp", layout="mono")
                frame.sample_rate = 16000
                c.mux(a.encode(frame))
                c.mux(a.encode())

    ok = tmp_path / "Good.Clip.mp4"
    make(ok, with_audio=True)
    info = local.probe(ok)
    assert info["title"] == "" and info["duration"] == pytest.approx(2.0, abs=0.3)
    silent = tmp_path / "silent.mp4"
    make(silent, with_audio=False)
    with pytest.raises(local.LocalSourceError, match="no audio"):
        local.probe(silent)
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video")
    with pytest.raises(local.LocalSourceError, match="could not read"):
        local.probe(junk)
