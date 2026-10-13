"""Instagram Reels via the Graph API (Creator/Business accounts).

Flow: create a REELS container with upload_type=resumable -> push bytes to
rupload.facebook.com -> poll container status_code until FINISHED ->
media_publish. The resumable path means the video never needs a public URL.
"""
from __future__ import annotations

import logging
import time

import httpx

from clip_pipeline.config import get_settings
from clip_pipeline.publish.uploaders.base import BaseUploader, UploadError, UploadJob, UploadResult, build_caption

log = logging.getLogger(__name__)

POLL_INTERVAL = 5.0
POLL_TIMEOUT = 600.0


class InstagramUploader(BaseUploader):
    platform = "instagram"

    def __init__(self, settings=None, client: httpx.Client | None = None, sleep=time.sleep):
        self.s = settings or get_settings()
        self._client = client
        self._sleep = sleep

    def is_configured(self) -> bool:
        return bool(self.s.instagram_user_id and self.s.instagram_access_token)

    @property
    def graph(self) -> str:
        return f"https://graph.facebook.com/{self.s.instagram_graph_version}"

    def _headers(self) -> dict:
        # token in a header, never in the URL (keeps it out of proxy/access logs)
        return {"Authorization": f"OAuth {self.s.instagram_access_token}"}

    def _check(self, resp: httpx.Response, step: str) -> dict:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or "error" in data:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            retryable = resp.status_code >= 500 or resp.status_code == 429 or bool(err.get("is_transient"))
            raise UploadError(f"Instagram {step} failed ({resp.status_code}): {err.get('message') or resp.text[:300]}",
                              retryable=retryable)
        return data

    def upload(self, job: UploadJob) -> UploadResult:
        return self._with_client(job, publish=True)

    def preflight(self, job: UploadJob) -> str:
        """Create + upload + process a Reels container but never publish it (unpublished containers expire in 24 h)."""
        res = self._with_client(job, publish=False)
        return f"container {res.remote_id} uploaded and FINISHED processing (not published; expires in 24 h)"

    def _with_client(self, job: UploadJob, *, publish: bool) -> UploadResult:
        client = self._client or httpx.Client(timeout=httpx.Timeout(60.0, write=900.0))
        try:
            return self._upload(client, job, publish=publish)
        except httpx.HTTPError as exc:
            raise UploadError(f"Instagram network error: {exc}", retryable=True) from exc
        finally:
            if self._client is None:
                client.close()

    def _upload(self, client: httpx.Client, job: UploadJob, *, publish: bool = True) -> UploadResult:
        caption = build_caption(job.title, job.description, job.tags, max_len=2200)
        data = self._check(client.post(
            f"{self.graph}/{self.s.instagram_user_id}/media", headers=self._headers(),
            data={"media_type": "REELS", "upload_type": "resumable", "caption": caption, "share_to_feed": "true"},
        ), "container")
        container_id = data["id"]
        upload_uri = data.get("uri") or (
            f"https://rupload.facebook.com/ig-api-upload/{self.s.instagram_graph_version}/{container_id}")

        size = job.file_path.stat().st_size
        with job.file_path.open("rb") as fh:
            # Explicit Content-Length: without it httpx streams the file chunked, which rupload rejects
            # with a generic 500 ProcessingFailedError.
            self._check(client.post(upload_uri, headers={**self._headers(), "offset": "0", "file_size": str(size),
                                                         "Content-Length": str(size),
                                                         "Content-Type": "application/octet-stream"},
                                    content=fh), "upload")

        deadline = time.monotonic() + POLL_TIMEOUT
        while True:
            st = self._check(client.get(f"{self.graph}/{container_id}", headers=self._headers(),
                                        params={"fields": "status_code,status"}), "status")
            code = st.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise UploadError(f"Instagram processing {code}: {st.get('status')}", retryable=code == "EXPIRED")
            if time.monotonic() > deadline:
                raise UploadError("Instagram processing timed out", retryable=True)
            self._sleep(POLL_INTERVAL)

        if not publish:
            return UploadResult(container_id)
        pub = self._check(client.post(f"{self.graph}/{self.s.instagram_user_id}/media_publish",
                                      headers=self._headers(), data={"creation_id": container_id}), "publish")
        media_id = pub["id"]
        permalink = None
        try:
            permalink = self._check(client.get(f"{self.graph}/{media_id}", headers=self._headers(),
                                               params={"fields": "permalink"}), "permalink").get("permalink")
        except UploadError:
            pass
        return UploadResult(media_id, permalink)
