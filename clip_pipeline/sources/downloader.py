"""yt-dlp wrapper: audio-first ingestion (Stage 1) and surgical video slices (Stage 4).

All invocations use list-form argv (never a shell) and put the URL after `--`
so a crafted URL can never be parsed as a yt-dlp option.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from clip_pipeline.config import get_settings
from clip_pipeline.hardware import resolve_binary

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

_PROGRESS_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")
CHAT_SUB_LANGS = "live_chat,rechat"  # YouTube live chat replay, Twitch VOD chat
AUDIO_EXTS = (".opus", ".webm", ".m4a", ".ogg", ".mp3", ".aac", ".wav")


class DownloadError(RuntimeError):
    pass


class InvalidURLError(ValueError):
    pass


def validate_url(url: str) -> str:
    """Accept only plain http(s) URLs with a host; reject anything option-like."""
    url = (url or "").strip()
    if not url or len(url) > 2048:
        raise InvalidURLError("URL is empty or too long")
    if url.startswith("-") or any(ch.isspace() for ch in url) or "\x00" in url:
        raise InvalidURLError("URL contains illegal characters")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidURLError("Only http(s) URLs are supported")
    return url


def ytdlp_base_cmd() -> list[str]:
    settings = get_settings()
    # Default to the yt-dlp installed in this environment (kept current via pip) rather than whatever
    # older yt-dlp.exe happens to be first on PATH; YTDLP_PATH overrides.
    binary = resolve_binary("yt-dlp", settings.ytdlp_path) if settings.ytdlp_path else None
    base = [binary] if binary else [sys.executable, "-m", "yt_dlp"]
    base += ["--no-playlist", "--newline", "--no-colors"]
    ffmpeg = resolve_binary("ffmpeg", settings.ffmpeg_path)
    if ffmpeg:
        base += ["--ffmpeg-location", ffmpeg]
    # YouTube extraction needs a JS runtime; yt-dlp only auto-enables deno, so opt into node when that's what exists.
    if not resolve_binary("deno"):
        node = resolve_binary("node")
        if node:
            base += ["--js-runtimes", f"node:{node}"]
    return base


def build_audio_cmd(url: str, out_dir: Path, *, with_ffmpeg: bool, with_chat: bool = True) -> list[str]:
    url = validate_url(url)
    cmd = ytdlp_base_cmd() + [
        "-f", "ba[acodec=opus]/ba",
        "--write-info-json",
        "-o", str(out_dir / "audio.%(ext)s"),
    ]
    if with_ffmpeg:
        # Extraction needs ffmpeg; without it we keep the native (usually opus-in-webm) stream.
        cmd += ["-x", "--audio-format", "opus"]
    if with_chat:
        cmd += ["--write-subs", "--sub-langs", CHAT_SUB_LANGS]
    return cmd + ["--", url]


def format_section_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def build_section_cmd(url: str, start: float, end: float, out_path: Path, *, max_height: int = 1080) -> list[str]:
    url = validate_url(url)
    if end <= start:
        raise ValueError("Section end must be after start")
    fmt = (
        f"bv*[height<={max_height}][vcodec^=avc1]+ba[ext=m4a]/"
        f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
    )
    return ytdlp_base_cmd() + [
        "-f", fmt,
        "--download-sections", f"*{format_section_time(start)}-{format_section_time(end)}",
        "--force-keyframes-at-cuts",  # frame-accurate start => subtitles stay in sync
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        "--", url,
    ]


def _run_streaming(cmd: list[str], progress: ProgressFn | None, label: str, timeout: float) -> str:
    """Run a command, forwarding yt-dlp percentage lines to `progress`."""
    log.debug("exec: %s", cmd)
    lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as exc:
        raise DownloadError(f"Could not start yt-dlp: {exc}") from exc
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line.rstrip())
        if progress:
            m = _PROGRESS_RE.search(line)
            if m:
                progress(min(float(m.group(1)), 100.0) / 100.0, label)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise DownloadError(f"{label} timed out")
    output = "\n".join(lines)
    if proc.returncode != 0:
        tail = "\n".join(lines[-15:])
        raise DownloadError(f"{label} failed (exit {proc.returncode}):\n{tail}")
    return output


@dataclass
class AudioDownload:
    audio_path: Path
    chat_path: Path | None
    info: dict = field(default_factory=dict)

    @property
    def title(self) -> str:
        return str(self.info.get("title") or "")

    @property
    def duration(self) -> float | None:
        d = self.info.get("duration")
        return float(d) if d else None


def find_outputs(out_dir: Path) -> AudioDownload:
    audio = next((p for p in sorted(out_dir.glob("audio.*")) if p.suffix.lower() in AUDIO_EXTS), None)
    if audio is None:
        raise DownloadError("yt-dlp finished but no audio file was produced")
    chat = next(
        (p for lang in CHAT_SUB_LANGS.split(",") for p in out_dir.glob(f"audio.{lang}.*")),
        None,
    )
    info: dict = {}
    info_path = out_dir / "audio.info.json"
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
    return AudioDownload(audio_path=audio, chat_path=chat, info=info)


def download_audio(url: str, out_dir: Path, progress: ProgressFn | None = None, *, timeout: float = 3 * 3600) -> AudioDownload:
    """Stage 1: audio-only + live chat replay. ~150 MB for a 5 h stream."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with_ffmpeg = resolve_binary("ffmpeg", get_settings().ffmpeg_path) is not None
    cmd = build_audio_cmd(url, out_dir, with_ffmpeg=with_ffmpeg)
    try:
        _run_streaming(cmd, progress, "audio download", timeout)
    except DownloadError:
        # Chat replay endpoints fail fairly often; never let chat sink the audio.
        if any(out_dir.glob("audio.*")):
            log.warning("yt-dlp reported an error but audio exists; continuing")
        else:
            log.warning("Retrying audio download without chat replay")
            _run_streaming(build_audio_cmd(url, out_dir, with_ffmpeg=with_ffmpeg, with_chat=False),
                           progress, "audio download", timeout)
    return find_outputs(out_dir)


def download_section(url: str, start: float, end: float, out_path: Path,
                     progress: ProgressFn | None = None, *, timeout: float = 900) -> Path:
    """Stage 4: fetch only [start, end] of the source at <=1080p."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _run_streaming(build_section_cmd(url, start, end, out_path), progress, "video slice", timeout)
    if not out_path.exists():
        # yt-dlp may append the container extension itself.
        alt = next(iter(out_path.parent.glob(out_path.stem + ".*")), None)
        if alt is None:
            raise DownloadError("yt-dlp finished but the slice file is missing")
        return alt
    return out_path
