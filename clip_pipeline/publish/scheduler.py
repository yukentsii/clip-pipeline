"""APScheduler daemon: publishes targets whose time has come, and reads reach numbers on a timer."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Mapping

from apscheduler.schedulers.background import BackgroundScheduler

from clip_pipeline.config import Settings, get_settings
from clip_pipeline.publish.queue import PostQueue, utcnow
from clip_pipeline.publish.uploaders.base import BaseUploader, UploadError, UploadJob

log = logging.getLogger(__name__)

# (platform, account_id) -> the uploader to use, or None when there is none
Resolver = Callable[[str, "str | None"], "BaseUploader | None"]
# called after every attempt: (claimed row, "completed" | "retrying" | "failed", remote url or error text)
ResultHook = Callable[[dict[str, Any], str, str], None]


def process_due(queue: PostQueue, uploaders: Mapping[str, BaseUploader] | Resolver, *,
                settings: Settings | None = None, now: datetime | None = None,
                on_result: ResultHook | None = None) -> dict[str, int]:
    """One scheduler tick. `uploaders` is either one uploader per platform or a function that picks
    the uploader for a target's account. Returns counts of completed / retrying / failed targets."""
    s = settings or get_settings()
    resolve: Resolver = (lambda platform, account_id: uploaders.get(platform)) \
        if isinstance(uploaders, Mapping) else uploaders
    stats = {"completed": 0, "retrying": 0, "failed": 0}

    def report(row: dict[str, Any], outcome: str, detail: str):
        stats[outcome] += 1
        if on_result is not None:
            try:
                on_result(row, outcome, detail)
            except Exception:  # noqa: BLE001 - bookkeeping after an upload must never undo the upload
                log.exception("Result hook failed for %s/%s", row["id"], row["platform"])

    for row in queue.claim_due(now=now):
        job = UploadJob.from_claim(row)
        try:
            uploader = resolve(job.platform, row.get("account_id"))
            if uploader is None:
                raise UploadError(f"no usable {job.platform} account for this post", retryable=False)
            log.info("Uploading %s to %s", job.upload_id, job.platform)
            result = uploader.run(job)
        except UploadError as exc:
            status = queue.mark_failed(row["target_id"], str(exc), retryable=exc.retryable,
                                       max_attempts=s.max_attempts, backoff_seconds=s.retry_backoff_seconds, now=now)
            log.warning("%s/%s -> %s: %s", job.upload_id, job.platform, status, exc)
            report(row, "retrying" if status == "pending" else "failed", str(exc))
        except Exception as exc:  # noqa: BLE001 - unexpected bugs still must not kill the daemon
            status = queue.mark_failed(row["target_id"], f"unexpected: {exc!r}", retryable=True,
                                       max_attempts=s.max_attempts, backoff_seconds=s.retry_backoff_seconds, now=now)
            log.exception("Unexpected uploader error for %s/%s", job.upload_id, job.platform)
            report(row, "retrying" if status == "pending" else "failed", f"unexpected: {exc!r}")
        else:
            queue.mark_completed(row["target_id"], result.remote_id, result.url)
            log.info("Published %s to %s: %s", job.upload_id, job.platform, result.url or result.remote_id)
            report(row, "completed", result.url or "")
    return stats


def start_scheduler(queue: PostQueue, uploaders: Mapping[str, BaseUploader] | Resolver,
                    settings: Settings | None = None, *, on_result: ResultHook | None = None,
                    collect_stats: Callable[[], Any] | None = None) -> BackgroundScheduler:
    s = settings or get_settings()
    recovered = queue.recover_stale()
    if recovered:
        log.info("Re-queued %d targets left in 'processing' by a previous run", recovered)
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(process_due, "interval", seconds=s.interval_seconds, args=[queue, uploaders],
                      kwargs={"settings": s, "on_result": on_result}, id="publish-due", max_instances=1,
                      coalesce=True, next_run_time=utcnow())
    if collect_stats is not None and s.stats_interval_minutes > 0:
        scheduler.add_job(collect_stats, "interval", minutes=s.stats_interval_minutes, id="collect-stats",
                          max_instances=1, coalesce=True)
    scheduler.start()
    return scheduler
