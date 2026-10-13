"""Local video files as a source: the inbox folder, uploads into it, and slices cut with ffmpeg.

A file already on disk needs no download: the analyser and Whisper read its audio track in place
(PyAV decodes straight from the container), so only the per-clip slices are ever written.
"""
from __future__ import annotations

import logging
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from clip_pipeline.config import get_settings
from clip_pipeline.hardware import (encoder_args, encoder_filter_suffix, encoder_input_args, ffmpeg_binary,
                                    preferred_encoders)

log = logging.getLogger(__name__)

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi", ".ts", ".flv")
PARTIAL_SUFFIX = ".part"   # uploads in progress; never listed


class LocalSourceError(RuntimeError):
    pass


@dataclass
class InboxFile:
    name: str
    size: int
    modified: float

    def to_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "modified": self.modified}


def inbox_dir() -> Path:
    d = get_settings().inbox_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_inbox() -> list[InboxFile]:
    """Video files sitting in the inbox, newest first."""
    files = []
    for p in inbox_dir().iterdir():
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            st = p.stat()
            files.append(InboxFile(p.name, st.st_size, st.st_mtime))
    return sorted(files, key=lambda f: -f.modified)


def resolve_inbox_file(name: str) -> Path:
    """Map a bare file name to a video inside the inbox. Anything path-like is rejected, so a request
    can never reach a file outside that folder."""
    name = (name or "").strip()
    if not name or "/" in name or "\\" in name or name in (".", "..") or "\x00" in name:
        raise LocalSourceError("not a plain file name")   # a backslash is a separator too, whatever the OS
    path = inbox_dir() / name
    if path.suffix.lower() not in VIDEO_EXTS:
        raise LocalSourceError(f"unsupported file type {path.suffix or '(none)'}")
    if not path.is_file():
        raise LocalSourceError("no such file in the inbox")
    return path


def safe_filename(name: str) -> str:
    """A browser-supplied file name reduced to something safe to create on any OS."""
    name = unicodedata.normalize("NFC", re.split(r"[\\/]", name or "")[-1])   # either slash, on any OS
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    if ext not in VIDEO_EXTS:
        raise LocalSourceError(f"unsupported file type {ext or '(none)'}; use one of {', '.join(VIDEO_EXTS)}")
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem).strip(" .")[:120] or "video"
    return stem + ext


def unused_inbox_path(name: str, folder: Path | None = None) -> Path:
    """`name` in the inbox (`folder`, default: the configured one), or `name (2)`, `name (3)`… when taken."""
    folder = folder or inbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / safe_filename(name)
    path, n = base, 1
    while path.exists() or path.with_name(path.name + PARTIAL_SUFFIX).exists():
        n += 1
        path = base.with_name(f"{base.stem} ({n}){base.suffix}")
    return path


def probe(path: Path) -> dict:
    """Duration, and the title the video itself carries, in the shape yt-dlp's info.json has.

    The title is only what is written in the file's own metadata ("" when there is none). It is never
    made up from the file name here: a file taken from the inbox has been renamed to `original.<ext>`
    by then, and the name it arrived with is already stored on the source."""
    import av

    try:
        with av.open(str(path)) as container:
            if not any(s.type == "audio" for s in container.streams):
                raise LocalSourceError("this video has no audio track; clips are found from the audio")
            duration = container.duration / 1_000_000 if container.duration else None
            title = re.sub(r"\s+", " ", str(container.metadata.get("title") or "")).strip()
    except LocalSourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - PyAV raises many error types for unreadable files
        raise LocalSourceError(f"could not read {path.name}: {exc}") from exc
    return {"title": title[:200], "duration": duration, "extractor": "local", "categories": []}


def title_from_filename(name: str) -> str:
    stem = re.sub(r"[._]+", " ", Path(name).stem)
    return re.sub(r"\s+", " ", stem).strip() or name


def build_cut_cmd(src: Path, start: float, end: float, out: Path, encoder: str, ffmpeg: str = "ffmpeg",
                  max_height: int = 1080) -> list[str]:
    """Frame-accurate [start, end] of `src` as H.264/AAC mp4 (first video + first audio track).

    Re-encoding rather than stream-copying keeps the cut exact (captions are timed from the slice start)
    and makes any source - HEVC, 10-bit, MKV - play in the browser preview."""
    if end <= start:
        raise ValueError("Section end must be after start")
    suffix = encoder_filter_suffix(encoder)
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *encoder_input_args(encoder),
        "-ss", f"{start:.3f}", "-i", str(src.resolve()),
        "-t", f"{end - start:.3f}",
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", f"scale=-2:'min({max_height},ih)'" + (f",{suffix}" if suffix else ""),
        *encoder_args(encoder),
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart",
        str(out.resolve()),
    ]


def cut_section(src: Path, start: float, end: float, out: Path, *, timeout: float = 900) -> Path:
    """The local-file counterpart of downloader.download_section."""
    if not src.is_file():
        raise LocalSourceError(f"source file is gone: {src}")
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = ffmpeg_binary()
    errors = []
    for encoder in preferred_encoders():
        try:
            proc = subprocess.run(build_cut_cmd(src, start, end, out, encoder, ffmpeg), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            errors.append(f"{encoder}: timeout")
            continue
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return out
        errors.append(f"{encoder}: {proc.stderr.strip()[-400:]}")
        log.warning("Cutting with %s failed, trying fallback", encoder)
    raise LocalSourceError("could not cut the slice:\n" + "\n".join(errors))
