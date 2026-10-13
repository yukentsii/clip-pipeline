from clip_pipeline import hardware

ENCODERS_SAMPLE = """Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D h264_amf             AMD AMF H.264 Encoder (codec h264)
 V....D hevc_amf             AMD AMF HEVC encoder (codec hevc)
 A....D aac                  AAC (Advanced Audio Coding)
"""


def test_parse_encoders_finds_amf_and_x264():
    names = hardware.parse_encoders(ENCODERS_SAMPLE)
    assert {"libx264", "h264_amf", "hevc_amf", "aac"} <= names
    assert "Video" not in names and "=" not in names


def test_amf_args_use_hardware_encoder_and_bitrate():
    args = hardware.encoder_args("h264_amf", "8M")
    assert args[:2] == ["-c:v", "h264_amf"]
    assert args[args.index("-b:v") + 1] == "8M"
    assert args[args.index("-maxrate") + 1] == "12M"


def test_x264_fallback_args():
    args = hardware.encoder_args("libx264", "8M")
    assert args[:2] == ["-c:v", "libx264"]
    assert "-preset" in args and args[args.index("-bufsize") + 1] == "16M"


def test_scale_bitrate_handles_units():
    assert hardware._scale_bitrate("8M", 1.5) == "12M"
    assert hardware._scale_bitrate("2500k", 2) == "5000k"
    assert hardware._scale_bitrate("weird", 2) == "weird"


def pick(monkeypatch, working, mode="auto"):
    from dataclasses import replace

    settings = replace(hardware.get_settings(), video_encoder=mode)
    monkeypatch.setattr(hardware, "get_settings", lambda: settings)
    monkeypatch.setattr(hardware, "encoder_works", lambda e: e in working)
    hardware.working_hardware_encoder.cache_clear()
    try:
        return hardware.preferred_encoders()
    finally:
        hardware.working_hardware_encoder.cache_clear()


def test_auto_picks_the_first_hardware_encoder_that_really_works(monkeypatch):
    assert pick(monkeypatch, set()) == ["libx264"]
    assert pick(monkeypatch, {"h264_amf"}) == ["h264_amf", "libx264"]
    assert pick(monkeypatch, {"h264_vaapi"}) == ["h264_vaapi", "libx264"]               # e.g. Intel on Linux
    assert pick(monkeypatch, {"h264_vaapi", "h264_qsv", "h264_nvenc"}) == ["h264_nvenc", "libx264"]


def test_encoder_can_be_named_and_software_is_always_the_fallback(monkeypatch):
    assert pick(monkeypatch, {"h264_amf"}, mode="x264") == ["libx264"]
    assert pick(monkeypatch, set(), mode="qsv") == ["h264_qsv", "libx264"]              # trusted, falls back at render
    assert pick(monkeypatch, {"h264_amf"}, mode="vaapi") == ["h264_vaapi", "libx264"]
    assert pick(monkeypatch, {"h264_amf"}, mode="nonsense") == ["h264_amf", "libx264"]  # unknown name = auto


def test_each_hardware_encoder_gets_its_own_arguments():
    for name in ("h264_nvenc", "h264_qsv", "h264_vaapi"):
        args = hardware.encoder_args(name, "8M")
        assert args[:2] == ["-c:v", name] and args[args.index("-maxrate") + 1] == "12M"
    assert "-pix_fmt" not in hardware.encoder_args("h264_vaapi")                         # frames are GPU surfaces
    assert hardware.encoder_filter_suffix("h264_vaapi") == "format=nv12,hwupload"
    assert hardware.encoder_input_args("h264_vaapi") == ["-vaapi_device", "/dev/dri/renderD128"]
    for name in ("h264_amf", "h264_nvenc", "h264_qsv", "libx264"):
        assert hardware.encoder_filter_suffix(name) == "" and hardware.encoder_input_args(name) == []


def test_listed_encoder_that_fails_its_test_encode_is_not_used(monkeypatch):
    from types import SimpleNamespace

    hardware.encoder_works.cache_clear()
    monkeypatch.setattr(hardware, "available_encoders", lambda: frozenset({"h264_nvenc", "libx264"}))
    monkeypatch.setattr(hardware, "ffmpeg_binary", lambda: "ffmpeg")
    ran = []

    def fake_run(cmd, **kw):
        ran.append(cmd)
        return SimpleNamespace(returncode=1, stderr="Cannot load nvcuda.dll")

    monkeypatch.setattr(hardware.subprocess, "run", fake_run)
    try:
        assert hardware.encoder_works("h264_nvenc") is False and "h264_nvenc" in ran[0]
        assert hardware.encoder_works("h264_qsv") is False and len(ran) == 1            # not listed: no probe at all
        assert hardware.encoder_works("libx264") is True
    finally:
        hardware.encoder_works.cache_clear()

def test_avx512_probe_returns_bool():
    assert isinstance(hardware.cpu_has_avx512(), bool)
