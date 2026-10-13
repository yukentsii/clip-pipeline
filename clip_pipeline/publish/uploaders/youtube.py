"""YouTube Shorts via YouTube Data API v3 (videos.insert, resumable, stored refresh token)."""
from __future__ import annotations

import logging

from clip_pipeline.config import get_settings
from clip_pipeline.publish.uploaders.base import BaseUploader, UploadError, UploadJob, UploadResult, build_caption

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_URI = "https://oauth2.googleapis.com/token"
CHUNK = 8 * 1024 * 1024


class YouTubeUploader(BaseUploader):
    platform = "youtube"

    def __init__(self, settings=None, service_factory=None):
        self.s = settings or get_settings()
        self._factory = service_factory

    def is_configured(self) -> bool:
        return bool(self.s.youtube_client_id and self.s.youtube_client_secret and self.s.youtube_refresh_token)

    def _service(self):
        if self._factory:
            return self._factory()
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(token=None, refresh_token=self.s.youtube_refresh_token, token_uri=TOKEN_URI,
                            client_id=self.s.youtube_client_id, client_secret=self.s.youtube_client_secret,
                            scopes=SCOPES)
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def build_body(self, job: UploadJob) -> dict:
        title = job.title.strip()[:100] or "Clip"
        description = build_caption("", job.description, job.tags, max_len=4900, extra_tags=["Shorts"]).strip()
        return {
            "snippet": {
                "title": title,
                "description": description,
                "tags": [t for t in job.tags if t][:30],
                "categoryId": self.s.youtube_category_id,
            },
            "status": {"privacyStatus": job.extra.get("visibility") or self.s.youtube_privacy_status,
                       "selfDeclaredMadeForKids": False},
        }

    def preflight(self, job: UploadJob) -> str:
        """Refresh the OAuth token and build the request body. The upload-only scope can't read the
        channel, and videos.insert has no dry-run, so this is as far as YouTube can go without posting."""
        if self._factory is None:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials

            creds = Credentials(token=None, refresh_token=self.s.youtube_refresh_token, token_uri=TOKEN_URI,
                                client_id=self.s.youtube_client_id, client_secret=self.s.youtube_client_secret,
                                scopes=SCOPES)
            try:
                creds.refresh(Request())
            except Exception as exc:  # noqa: BLE001
                raise UploadError(f"YouTube token refresh failed: {exc}", retryable=False) from exc
        body = self.build_body(job)
        return (f"OAuth token refreshed; would upload as {body['status']['privacyStatus']!r} "
                f"titled {body['snippet']['title']!r}")

    def upload(self, job: UploadJob) -> UploadResult:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        try:
            youtube = self._service()
            request = youtube.videos().insert(
                part="snippet,status", body=self.build_body(job),
                media_body=MediaFileUpload(str(job.file_path), mimetype="video/mp4", chunksize=CHUNK, resumable=True),
            )
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    log.info("YouTube upload %s: %d%%", job.upload_id, int(status.progress() * 100))
        except HttpError as exc:
            code = getattr(exc.resp, "status", 0)
            content = str(exc)
            retryable = code in (429, 500, 502, 503, 504) or "quotaExceeded" in content
            raise UploadError(f"YouTube API {code}: {content[:500]}", retryable=retryable) from exc
        except Exception as exc:  # noqa: BLE001 - auth refresh / network
            msg = str(exc)
            retryable = "invalid_grant" not in msg
            raise UploadError(f"YouTube upload failed: {msg[:500]}", retryable=retryable) from exc
        video_id = response["id"]
        return UploadResult(video_id, f"https://youtube.com/shorts/{video_id}")
