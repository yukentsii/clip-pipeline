import json
from pathlib import Path

import pytest

from clip_pipeline.sources import downloader as dl


@pytest.mark.parametrize("bad", [
    "", "   ", "-o/etc/passwd", "--exec=calc.exe", "file:///C:/Windows/win.ini", "ftp://x.com/a",
    "https://", "https://exa mple.com/watch", "javascript:alert(1)", "https://x.com/\x00",
])
def test_validate_url_rejects_unsafe(bad):
    with pytest.raises(dl.InvalidURLError):
        dl.validate_url(bad)


def test_validate_url_accepts_youtube_and_twitch():
    assert dl.validate_url(" https://www.youtube.com/watch?v=abc123 ") == "https://www.youtube.com/watch?v=abc123"
    assert dl.validate_url("https://www.twitch.tv/videos/123456")


def test_audio_cmd_is_list_with_url_after_double_dash(tmp_path):
    cmd = dl.build_audio_cmd("https://youtu.be/abc", tmp_path, with_ffmpeg=True)
    assert isinstance(cmd, list) and all(isinstance(a, str) for a in cmd)
    assert cmd[-2:] == ["--", "https://youtu.be/abc"]
    assert cmd[cmd.index("-f") + 1].startswith("ba")
    assert "-x" in cmd and cmd[cmd.index("--audio-format") + 1] == "opus"
    assert cmd[cmd.index("--sub-langs") + 1] == "live_chat,rechat"


def test_audio_cmd_without_ffmpeg_skips_extraction(tmp_path):
    cmd = dl.build_audio_cmd("https://youtu.be/abc", tmp_path, with_ffmpeg=False, with_chat=False)
    assert "-x" not in cmd and "--write-subs" not in cmd


def test_section_cmd_targets_exact_window(tmp_path):
    cmd = dl.build_section_cmd("https://youtu.be/abc", 2535.5, 2585.25, tmp_path / "c00.mp4")
    assert cmd[cmd.index("--download-sections") + 1] == "*00:42:15.500-00:43:05.250"
    assert "--force-keyframes-at-cuts" in cmd
    assert "height<=1080" in cmd[cmd.index("-f") + 1]
    assert cmd[-2:] == ["--", "https://youtu.be/abc"]


def test_section_cmd_rejects_inverted_window(tmp_path):
    with pytest.raises(ValueError):
        dl.build_section_cmd("https://youtu.be/abc", 50, 40, tmp_path / "x.mp4")


def test_format_section_time():
    assert dl.format_section_time(0) == "00:00:00.000"
    assert dl.format_section_time(3723.25) == "01:02:03.250"
    assert dl.format_section_time(-5) == "00:00:00.000"


def test_find_outputs_picks_audio_chat_and_info(tmp_path):
    (tmp_path / "audio.opus").write_bytes(b"x")
    (tmp_path / "audio.live_chat.json").write_text("{}")
    (tmp_path / "audio.info.json").write_text(json.dumps({"title": "Big Stream", "duration": 18000}))
    out = dl.find_outputs(tmp_path)
    assert out.audio_path.name == "audio.opus"
    assert out.chat_path and out.chat_path.name == "audio.live_chat.json"
    assert out.title == "Big Stream" and out.duration == 18000


def test_find_outputs_without_audio_raises(tmp_path):
    with pytest.raises(dl.DownloadError):
        dl.find_outputs(tmp_path)


def test_download_audio_never_uses_shell(monkeypatch, tmp_path):
    calls = []

    class FakeProc:
        returncode = 0

        def __init__(self, cmd, **kw):
            calls.append((cmd, kw))
            Path(tmp_path / "audio.opus").write_bytes(b"x")
            self.stdout = iter(["[download]  42.0% of 150MiB\n", "[download] 100% of 150MiB\n"])

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(dl.subprocess, "Popen", FakeProc)
    seen = []
    res = dl.download_audio("https://youtu.be/abc", tmp_path, lambda f, m: seen.append(f))
    cmd, kw = calls[0]
    assert isinstance(cmd, list) and not kw.get("shell")
    assert seen == [0.42, 1.0]
    assert res.audio_path.name == "audio.opus"
