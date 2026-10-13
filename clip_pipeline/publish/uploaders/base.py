"""BaseUploader contract shared by every platform."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


@dataclass
class UploadJob:
    upload_id: str
    platform: str
    file_path: Path
    title: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_claim(cls, row: dict) -> "UploadJob":
        return cls(upload_id=row["id"], platform=row["platform"], file_path=Path(row["file_path"]),
                   title=row["title"], description=row.get("description") or "", tags=row.get("tags") or [],
                   extra=row.get("extra") or {})


@dataclass
class UploadResult:
    remote_id: str
    url: str | None = None


class UploadError(Exception):
    """`retryable=False` for problems a retry cannot fix (bad credentials, rejected media, CAPTCHA)."""

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class BaseUploader(ABC):
    platform: ClassVar[str]

    @abstractmethod
    def is_configured(self) -> bool:
        """True when credentials/profile needed for uploads are present."""

    @abstractmethod
    def upload(self, job: UploadJob) -> UploadResult:
        """Publish the video. Raise UploadError on failure."""

    def preflight(self, job: UploadJob) -> str:
        """Exercise as much of the upload path as possible WITHOUT publishing; return a summary."""
        raise UploadError(f"{self.platform} has no preflight check", retryable=False)

    def run(self, job: UploadJob) -> UploadResult:
        if not self.is_configured():
            raise UploadError(f"{self.platform} uploader is not configured", retryable=False)
        if not job.file_path.exists():
            raise UploadError(f"video file missing: {job.file_path}", retryable=False)
        return self.upload(job)


def hashtags(tags: list[str], limit: int = 8) -> str:
    out = []
    for t in tags[:limit]:
        t = "".join(ch for ch in t if ch.isalnum() or ch == "_")
        if t:
            out.append("#" + t)
    return " ".join(out)


def build_caption(title: str, description: str, tags: list[str], *, max_len: int, extra_tags: list[str] = ()) -> str:
    parts = [title.strip()]
    if description.strip() and description.strip() != title.strip():
        parts.append(description.strip())
    tag_line = hashtags(list(extra_tags) + [t for t in tags if t.lower() not in {e.lower() for e in extra_tags}])
    body = "\n\n".join(parts)
    room = max_len - (len(tag_line) + 2 if tag_line else 0)
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    return f"{body}\n\n{tag_line}" if tag_line else body
