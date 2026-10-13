"""The inbox folder and source fingerprints.

Any video placed in the inbox is picked up by itself once its copy has finished. Fingerprints let
us recognise a video we have already processed, whether it arrives again as a file or as a link.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from clip_pipeline.sources.local import PARTIAL_SUFFIX, VIDEO_EXTS

log = logging.getLogger(__name__)

DUPLICATES_DIRNAME = "_duplicates"
SAMPLE_BYTES = 1024 * 1024

_YOUTUBE_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
_TWITCH_VOD = re.compile(r"twitch\.tv/(?:\w+/v/|videos/)(\d+)")
_TRACKING = re.compile(r"^(utm_\w+|si|feature|fbclid|gclid|ref)$", re.I)


def url_fingerprint(url: str) -> str:
    """Same video => same string, however the link was copied (short link, extra tracking parameters…)."""
    m = _YOUTUBE_ID.search(url)
    if m and re.search(r"youtu\.?be", url, re.I):
        return f"youtube:{m.group(1)}"
    m = _TWITCH_VOD.search(url)
    if m:
        return f"twitch:{m.group(1)}"
    p = urlparse(url.strip())
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query) if not _TRACKING.match(k)))
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    return f"url:{host}{p.path.rstrip('/')}" + (f"?{query}" if query else "")


def file_fingerprint(path: Path) -> str:
    """Size plus a hash of the first, middle and last megabyte: identifies a multi-gigabyte video
    without reading all of it, and survives the file being renamed."""
    size = path.stat().st_size
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for offset in sorted({0, max(0, size // 2 - SAMPLE_BYTES // 2), max(0, size - SAMPLE_BYTES)}):
            fh.seek(offset)
            h.update(fh.read(SAMPLE_BYTES))
    return f"file:{size}:{h.hexdigest()[:32]}"


class InboxWatcher:
    """Finds inbox videos whose copy has finished.

    A file counts as finished when its size and modification time were the same on two scans in a row
    and it was last written at least `settle_seconds` ago. Files still being uploaded through the API
    carry a `.part` suffix and are never looked at."""

    def __init__(self, folder: Path, settle_seconds: float = 10.0, now=time.time):
        self.folder = folder
        self.settle_seconds = settle_seconds
        self._now = now
        self._seen: dict[Path, tuple[int, float]] = {}

    def ready_files(self) -> list[Path]:
        self.folder.mkdir(parents=True, exist_ok=True)
        current: dict[Path, tuple[int, float]] = {}
        ready: list[Path] = []
        for p in sorted(self.folder.iterdir()):
            if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS or p.name.endswith(PARTIAL_SUFFIX):
                continue
            try:
                st = p.stat()
            except OSError:
                continue   # vanished between listing and stat
            state = (st.st_size, st.st_mtime)
            current[p] = state
            if st.st_size > 0 and self._seen.get(p) == state and self._now() - st.st_mtime >= self.settle_seconds:
                ready.append(p)
        self._seen = current
        return ready

    def forget(self, path: Path):
        self._seen.pop(path, None)


def move_aside_duplicate(path: Path, folder: Path) -> Path:
    """Park a video we have already processed in `<inbox>/_duplicates` rather than deleting someone's file."""
    dest_dir = folder / DUPLICATES_DIRNAME
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest, n = dest_dir / path.name, 1
    while dest.exists():
        n += 1
        dest = dest_dir / f"{path.stem} ({n}){path.suffix}"
    shutil.move(str(path), str(dest))   # also works when the inbox is on another drive
    return dest
