"""Does the work: source -> scored clips -> slices for review -> render -> post.

Every stage leaves its result on disk or in the database before the next one starts, so after a
restart a source carries on from the stage it had reached instead of starting over:

    audio      source/ + audio.done      (skipped for local files: they are read in place)
    analysis   analysis.json
    whisper    transcripts.json
    llm        rows in `clips`
    slices     slice_file on each clip that reached the review threshold
    render     render_file on each of those: review shows the clip exactly as it will be posted
"""
from __future__ import annotations

import json
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any

from clip_pipeline.config import Settings, get_settings
from clip_pipeline.db import iso, utcnow
from clip_pipeline.publish.accounts import Accounts
from clip_pipeline.publish.queue import PostQueue
from clip_pipeline.store import Clip, Source, Store

log = logging.getLogger(__name__)

AUDIO_DONE = "audio.done"
MIN_CLIP_SECONDS = 3.0

ASPECTS = ("9:16", "1:1", "16:9")
LAYOUTS = ("auto", "crop", "fit")
SUBTITLE_STYLES = ("karaoke", "static", "none")
TEXT_FIELDS = ("title", "description", "tags")
VIDEO_FIELDS = ("start", "end", "aspect", "layout", "subtitle_style", "hook_title")


class StateError(RuntimeError):
    """The clip or post is not in a state that allows this (e.g. approving a clip still rendering)."""


def candidate_count_for(duration: float | None, cap: int) -> int:
    """~1 candidate per 4 source minutes, so the funnel narrows on short videos too (58 min -> 15, 5 h -> cap)."""
    if not duration:
        return cap
    return max(4, min(cap, round(duration / 240)))


class Worker:
    def __init__(self, store: Store, queue: PostQueue, settings: Settings | None = None,
                 accounts: Accounts | None = None):
        self.store = store
        self.queue = queue
        self.s = settings or get_settings()
        self.accounts = accounts or Accounts(store.db, self.s)
        # One source at a time; slices and renders have their own lanes so review stays responsive.
        self.source_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="source")
        self.render_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render")
        self.io_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="slice")
        self._stop = threading.Event()
        self._drains = 0                      # drain runs queued or in progress (see wait_idle)
        self._drains_lock = threading.Lock()
        from clip_pipeline.sources.inbox import InboxWatcher

        self.inbox = InboxWatcher(self.s.inbox_dir, self.s.inbox_settle_seconds)

    # ---------------------------------------------------------------- lifecycle
    def start(self, poll_seconds: float | None = None) -> dict[str, int]:
        """Call once at boot: re-queue whatever a previous run left half-done, then get going.
        With `poll_seconds`, also keep watching the inbox folder and the database for new sources."""
        recovered = self.store.recover_interrupted()
        if any(recovered.values()):
            log.info("Recovered after restart: %s", recovered)
        # Clips whose source already finished but whose own cut or render was interrupted.
        for clip in self.store.list_clips(status=("candidate", "sliced")):
            if self.store.get_source(clip.source_id).status == "ready":
                (self.submit_slice if clip.status == "candidate" else self.submit_render)(clip.id)
        self.kick()
        if poll_seconds:
            threading.Thread(target=self._poll, args=(poll_seconds,), name="source-poll", daemon=True).start()
        return recovered

    def _poll(self, seconds: float):
        while not self._stop.wait(seconds):
            try:
                self.scan_inbox()
                if self.store.next_queued_source() is not None:
                    self.kick()
            except Exception:  # noqa: BLE001 - the watcher must outlive a bad file or a locked database
                log.exception("Polling for new sources failed; trying again")

    def shutdown(self):
        self._stop.set()
        for pool in (self.source_pool, self.render_pool, self.io_pool):
            pool.shutdown(wait=False, cancel_futures=True)

    def finish_background(self):
        """Let queued cuts and renders finish, then stop taking more (for the CLI and tests)."""
        self.io_pool.shutdown(wait=True)       # a finished cut queues its render, so this order matters
        self.render_pool.shutdown(wait=True)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no source is queued or running (for the CLI and tests). False on timeout.

        A source is marked ready a moment before its last bookkeeping (the "clips ready" event, cleaning
        up) is done, so the queue being empty is not enough: the drain itself must have returned."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._drains > 0 or self.store.next_queued_source() is not None:
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.2)
        return True

    def kick(self):
        """Make sure queued sources are being worked through."""
        with self._drains_lock:
            self._drains += 1
        try:
            self.source_pool.submit(self._drain)
        except RuntimeError:                  # shutting down: nothing will run, so nothing is pending
            with self._drains_lock:
                self._drains -= 1

    def _drain(self):
        try:
            self._drain_queue()
        finally:
            with self._drains_lock:
                self._drains -= 1

    def _drain_queue(self):
        while (src := self.store.next_queued_source()) is not None:
            try:
                self.process(src)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded, the queue moves on
                log.exception("Source %s failed", src.id)
                self.store.update_source(src.id, status="failed", error=str(exc)[:2000])
                failed = self.store.get_source(src.id)
                self.store.add_event("source_failed", f"Could not process {failed.title or 'a video'}",
                                     str(exc).strip().splitlines()[0][:300] if str(exc).strip() else "",
                                     {"source_id": src.id, "stage": failed.stage})

    # ---------------------------------------------------------------- adding work
    def add_url(self, url: str, content_hint: str | None = None) -> tuple[Source, bool]:
        """Queue a link. Returns (source, is_new): a video we already have is not processed again."""
        from clip_pipeline.sources.downloader import validate_url
        from clip_pipeline.sources.inbox import url_fingerprint

        url = validate_url(url)
        fingerprint = url_fingerprint(url)
        existing = self.store.find_by_fingerprint(fingerprint)
        if existing is not None:
            return existing, False
        src = self.store.create_source(url=url, content_hint=content_hint, fingerprint=fingerprint)
        self.kick()
        return src, True

    def add_file(self, path: Path, content_hint: str | None = None, *, take: bool = False) -> tuple[Source, bool]:
        """Queue a local video. Returns (source, is_new).

        `take=True` (inbox files, uploads) moves the file into the source's own folder: the inbox empties
        as a sign it was picked up, and the file becomes ours to delete once its clips are decided. With
        `take=False` the file is read where it is and never touched."""
        from clip_pipeline.sources import inbox, local

        path = path.resolve()
        fingerprint = inbox.file_fingerprint(path)
        existing = self.store.find_by_fingerprint(fingerprint)
        if existing is not None:
            if take:
                parked = inbox.move_aside_duplicate(path, self.s.inbox_dir)
                log.info("%s was already processed as %s; moved to %s", path.name, existing.id[:8], parked)
            return existing, False
        source_id, title, final = self.store.new_id(), local.title_from_filename(path.name), path
        if take:
            # Move first, record second: a source row never points at a file that is still being moved.
            sdir = self.store.source_dir(source_id)
            sdir.mkdir(parents=True, exist_ok=True)
            final = sdir / f"original{path.suffix.lower()}"
            try:
                shutil.move(str(path), str(final))
            except OSError:
                shutil.rmtree(sdir, ignore_errors=True)   # still locked by whoever is copying it: try later
                raise
        src = self.store.create_source(source_id=source_id, file_path=final, title=title, content_hint=content_hint,
                                       fingerprint=fingerprint, owns_file=take)
        self.kick()
        return src, True

    def scan_inbox(self) -> list[Source]:
        """Take every inbox video whose copy has finished. Called on a timer; safe to call any time."""
        taken = []
        for path in self.inbox.ready_files():
            try:
                src, is_new = self.add_file(path, take=True)
            except OSError as exc:
                log.info("Inbox file %s is not ready yet (%s)", path.name, exc)
                continue
            self.inbox.forget(path)
            if is_new:
                log.info("Picked up %s from the inbox as source %s", path.name, src.id[:8])
                taken.append(src)
        return taken

    # ---------------------------------------------------------------- source -> clips
    def _progress(self, source_id: str, stage: str):
        last = [0.0]

        def cb(frac: float, msg: str = ""):
            now = time.monotonic()
            if frac < 1.0 and now - last[0] < 0.5:
                return
            last[0] = now
            self.store.update_source(source_id, stage=stage, stage_progress=round(frac, 3), message=msg or stage)
        return cb

    def process(self, src: Source):
        from clip_pipeline.analysis import audio_analyzer, llm_scorer, transcriber

        sdir = self.store.source_dir(src.id)
        sdir.mkdir(parents=True, exist_ok=True)
        update = lambda **kw: self.store.update_source(src.id, **kw)  # noqa: E731
        update(status="running", error=None)

        # ---- audio
        audio = self._audio(src, sdir)
        update(title=audio.title or src.title, duration=audio.duration)

        # ---- analysis
        analysis_file = sdir / "analysis.json"
        if analysis_file.exists():
            summary = json.loads(analysis_file.read_text(encoding="utf-8"))
        else:
            update(stage="analysis", stage_progress=0.0, message="Computing loudness & chat velocity")
            result = audio_analyzer.analyze(
                audio.audio_path, audio.chat_path,
                count=candidate_count_for(audio.duration, self.s.candidate_count),
                pre_roll=self.s.candidate_pre_roll, post_roll=self.s.candidate_post_roll,
                chat_lag=self.s.chat_lag_seconds, progress=self._progress(src.id, "analysis"))
            summary = result.summary()
            _write_json(analysis_file, summary)
        names = {f.name for f in fields(audio_analyzer.Candidate)}
        candidates = [audio_analyzer.Candidate(**{k: v for k, v in c.items() if k in names})
                      for c in summary["candidates"]]
        duration = audio.duration or summary.get("duration")
        update(duration=duration, candidate_minutes=summary.get("candidate_minutes"))

        # ---- whisper
        transcripts_file = sdir / "transcripts.json"
        if transcripts_file.exists():
            transcripts = [transcriber.Transcript.from_dict(t)
                           for t in json.loads(transcripts_file.read_text(encoding="utf-8"))]
        else:
            update(stage="whisper", stage_progress=0.0, message=f"Transcribing {len(candidates)} candidates")
            transcripts = transcriber.transcribe_candidates(audio.audio_path, candidates,
                                                            self._progress(src.id, "whisper"))
            _write_json(transcripts_file, [t.to_dict() for t in transcripts])

        # ---- scoring
        if not self.store.list_clips(source_id=src.id, limit=1):
            stream_type = src.content_hint or transcriber.classify_stream(transcripts, audio.info)
            update(content_type=stream_type, stage="llm", stage_progress=0.0, message="Scoring hooks")
            notes: list[str] = []
            prefs = self.prefs()
            scored = llm_scorer.score_candidates(
                candidates, transcripts, stream_title=audio.title, stream_type=stream_type, notes=notes,
                categories=[str(c) for c in audio.info.get("categories") or []],
                status=lambda msg: self.store.update_source(src.id, message=msg),
                progress=self._progress(src.id, "llm"))
            # The LLM read the transcripts, so its verdict beats the metadata guess; clips it never saw
            # follow it. A type the owner picked themselves beats both.
            stream_type = src.content_hint or llm_scorer.dominant_content_type(scored) or stream_type
            rows = [dict(
                candidate_id=c.candidate_id, title=c.title, description=c.description, tags=c.tags,
                hook_title=c.hook_title, hook_reason=c.hook_reason, virality_score=c.virality_score,
                hook_score=c.hook_score, engagement_score=c.engagement_score, value_score=c.value_score,
                shareability_score=c.shareability_score, scorer=c.source, start=c.start, end=c.end,
                content_type=c.content_type if c.source == "gemini" and not src.content_hint else stream_type,
                status="candidate" if c.virality_score >= prefs["review_min_score"] else "below_threshold",
                aspect=prefs["default_aspect"], layout=prefs["default_layout"],
                subtitle_style=prefs["default_subtitle_style"],
            ) for c in scored]
            mark_overlaps(rows)
            fallback = sum(c.source == "heuristic" for c in scored)
            note = f"{notes[0]} - {fallback} of {len(scored)} clips scored by the offline heuristic" if notes else ""
            self.store.add_clips(src.id, rows)
            update(content_type=stream_type, scoring_note=note,
                   scoring_model=", ".join(llm_scorer.models_used(scored)))

        # ---- slices for everything that reached the review threshold
        todo = self.store.list_clips(source_id=src.id, status="candidate")
        update(stage="slices", stage_progress=0.0, message=f"Cutting {len(todo)} clips")
        done = [0]
        lock = threading.Lock()

        def cut(clip: Clip):
            self.slice_clip(clip.id, duration=duration, then_render=False)
            with lock:
                done[0] += 1
                update(stage_progress=round(done[0] / len(todo), 3))

        list(self.io_pool.map(cut, todo))

        # ---- render each one the way it will be posted (best first), so review shows the finished clip
        todo = self.store.list_clips(source_id=src.id, status="sliced")
        update(stage="render", stage_progress=0.0, message=f"Rendering {len(todo)} clips for review")
        for i, clip in enumerate(todo, 1):
            self.render_for_review(clip.id)
            update(stage_progress=round(i / len(todo), 3))

        waiting = len(self.store.list_clips(source_id=src.id, status="review"))
        update(status="ready", stage="render", stage_progress=1.0,
               message=f"{waiting} clips ready for review" if waiting else "No clip reached the review score")
        if waiting:
            title = self.store.get_source(src.id).title
            self.store.add_event("clips_ready", f"{waiting} clip{'s' if waiting != 1 else ''} ready to review",
                                 title, {"source_id": src.id, "count": waiting})
        self.cleanup_source(src.id)   # nothing to review => nothing more is needed from the source

    def _audio(self, src: Source, sdir: Path):
        from clip_pipeline.sources import downloader, local

        if src.kind == "file":
            # Nothing to download: every later stage reads the audio track straight out of the video file.
            path = Path(src.file_path or "")
            if not path.is_file():
                raise local.LocalSourceError(f"source file is gone: {path}")
            return downloader.AudioDownload(audio_path=path, chat_path=None, info=local.probe(path))
        marker = sdir / AUDIO_DONE
        if not marker.exists():
            self.store.update_source(src.id, stage="audio", stage_progress=0.0, message="Downloading audio")
            downloader.download_audio(src.url, sdir / "source", self._progress(src.id, "audio"))
            marker.write_text("ok", encoding="utf-8")
        return downloader.find_outputs(sdir / "source")

    # ---------------------------------------------------------------- preferences
    def prefs(self) -> dict[str, Any]:
        """Defaults from the environment, overridden by whatever the owner saved from the app."""
        return {"review_min_score": self.s.review_min_score, "default_aspect": "9:16", "default_layout": "auto",
                "default_subtitle_style": "karaoke", **self.store.get_prefs()}

    def set_prefs(self, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"default_aspect": ASPECTS, "default_layout": LAYOUTS, "default_subtitle_style": SUBTITLE_STYLES}
        clean: dict[str, Any] = {}
        for key, value in changes.items():
            if key == "review_min_score":
                if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                    raise ValueError("review_min_score must be a whole number from 0 to 100")
            elif key in allowed:
                if value not in allowed[key]:
                    raise ValueError(f"{key} must be one of {', '.join(allowed[key])}")
            else:
                raise ValueError(f"unknown setting {key!r}")
            clean[key] = value
        self.store.set_prefs(clean)
        return self.prefs()

    # ---------------------------------------------------------------- cut and render
    def submit_slice(self, clip_id: str):
        self.io_pool.submit(self.slice_clip, clip_id)

    def submit_render(self, clip_id: str):
        self.render_pool.submit(self.render_for_review, clip_id)

    def slice_clip(self, clip_id: str, duration: float | None = None, then_render: bool = True):
        """Cut the clip (plus padding for re-trimming) out of the source: candidate -> sliced."""
        from clip_pipeline.sources import downloader, local

        if not self.store.claim_clip(clip_id, ("candidate",), "slicing"):
            return
        clip = self.store.get_clip(clip_id)
        src = self.store.get_source(clip.source_id)
        sdir = self.store.source_dir(src.id)
        duration = duration or src.duration
        start = max(0.0, clip.start - self.s.slice_padding)
        end = clip.end + self.s.slice_padding
        if duration:
            end = min(end, duration)
        try:
            out = sdir / "slices" / f"{clip.candidate_id}.mp4"
            if src.kind == "file":
                path = local.cut_section(Path(src.file_path or ""), start, end, out)
            else:
                path = downloader.download_section(src.url, start, end, out)
            self.store.update_clip(clip.id, status="sliced", slice_file=path.relative_to(sdir).as_posix(),
                                   slice_start=start, slice_end=end)
        except Exception as exc:  # noqa: BLE001
            log.exception("Slice %s failed", clip.id)
            self.store.update_clip(clip.id, status="failed", error=f"Slice failed: {exc}"[:1000])
            return
        if then_render:
            self.submit_render(clip.id)

    def render_for_review(self, clip_id: str) -> bool:
        """Render the clip with its saved settings: sliced -> review. False if it wasn't waiting for that."""
        from clip_pipeline.render import video_renderer

        if not self.store.claim_clip(clip_id, ("sliced",), "rendering"):
            return False
        clip = self.store.get_clip(clip_id)
        sdir = self.store.source_dir(clip.source_id)
        try:
            path, encoder = self.render_clip(clip)
            thumb = video_renderer.extract_thumbnail(path, path.parent / "thumb.jpg", at=1.0)
            self.store.update_clip(clip.id, status="review", render_file=path.relative_to(sdir).as_posix(),
                                   encoder=encoder, thumb_file=thumb.relative_to(sdir).as_posix() if thumb else None)
            return True
        except Exception as exc:  # noqa: BLE001
            log.exception("Render %s failed", clip.id)
            self.store.update_clip(clip.id, status="failed", error=f"Render failed: {exc}"[:1000])
            return False

    def retry_clip(self, clip_id: str) -> str:
        """Try a failed clip again from the step that failed. Returns the state it was put back into."""
        clip = self.store.get_clip(clip_id)
        back_to = "sliced" if clip.slice_file and (self.store.source_dir(clip.source_id) / clip.slice_file).exists() \
            else "candidate"
        if not self.store.claim_clip(clip_id, ("failed",), back_to):
            raise StateError(f"clip is {clip.status}, not failed")
        (self.submit_render if back_to == "sliced" else self.submit_slice)(clip_id)
        return back_to

    def retry_source(self, source_id: str) -> None:
        """Queue a failed source again; it carries on from the stage that failed."""
        src = self.store.get_source(source_id)
        if src.status != "failed":
            raise StateError(f"source is {src.status}, not failed")
        self.store.update_source(source_id, status="queued", error=None, message="Queued again")
        self.kick()

    # ---------------------------------------------------------------- review
    def edit_clip(self, clip_id: str, changes: dict[str, Any]) -> str:
        """Change a clip that is waiting for review. Post text is just saved; anything that changes the
        picture (trim, framing, captions, hook title) sends it back to be rendered again.
        Returns the clip's new status: "review" or "sliced" (= queued for rendering)."""
        clip = self.store.get_clip(clip_id)
        unknown = set(changes) - set(TEXT_FIELDS) - set(VIDEO_FIELDS)
        if unknown:
            raise ValueError(f"cannot change {sorted(unknown)}")
        if clip.status != "review":
            raise StateError(f"clip is {clip.status}; only clips waiting for review can be changed")
        new = dict(changes)
        for key, allowed in (("aspect", ASPECTS), ("layout", LAYOUTS), ("subtitle_style", SUBTITLE_STYLES)):
            if key in new and new[key] not in allowed:
                raise ValueError(f"{key} must be one of {', '.join(allowed)}")
        if "start" in new or "end" in new:
            start = max(float(new.get("start", clip.start)), clip.slice_start or 0.0)
            end = min(float(new.get("end", clip.end)), clip.slice_end or float(new.get("end", clip.end)))
            if end - start < MIN_CLIP_SECONDS:
                raise ValueError(f"a clip must be at least {MIN_CLIP_SECONDS:g} seconds and inside its cut "
                                 f"({clip.slice_start:.1f} to {clip.slice_end:.1f})")
            new["start"], new["end"] = round(start, 2), round(end, 2)
        if "hook_title" in new:
            new["hook_title"] = str(new["hook_title"] or "").strip()[:80]
        rerender = any(k in new and new[k] != getattr(clip, k) for k in VIDEO_FIELDS)
        if rerender and not self.store.claim_clip(clip_id, ("review",), "sliced"):
            raise StateError("clip changed state while it was being edited; try again")
        if new:
            self.store.update_clip(clip_id, **new)
        if rerender:
            self.submit_render(clip_id)
        return "sliced" if rerender else "review"

    def reject(self, clip_id: str) -> bool:
        if not self.store.claim_clip(clip_id, ("review",), "rejected"):
            return False
        clip = self.store.get_clip(clip_id)
        sdir = self.store.source_dir(clip.source_id)
        if clip.slice_file:
            (sdir / clip.slice_file).unlink(missing_ok=True)
        shutil.rmtree(sdir / "renders" / clip.candidate_id, ignore_errors=True)
        self.store.update_clip(clip_id, slice_file=None, thumb_file=None, render_file=None)
        self.cleanup_source(clip.source_id)
        return True

    def resolve_targets(self, specs: list[dict[str, Any]], keep_time_for: set[tuple[str, str]] = frozenset()
                        ) -> list[dict[str, Any]]:
        """Turn what the owner picked into concrete targets. Each spec names an `account_id` (or just a
        `platform`, meaning that platform's first account) and optionally a `scheduled_at`; without a
        time the account's next free posting slot is used. Specs whose (platform, account) is in
        `keep_time_for` already exist on the post and keep their time. Raises ValueError."""
        out: list[dict[str, Any]] = []
        handed_out: dict[str, set[str]] = {}
        for spec in specs:
            if spec.get("account_id"):
                try:
                    acct = self.accounts.get(spec["account_id"])
                except KeyError:
                    raise ValueError(f"unknown account {spec['account_id']!r}") from None
                if not acct["enabled"]:
                    raise ValueError(f"account {acct['name']!r} is switched off")
            elif spec.get("platform"):
                acct = self.accounts.default_for(spec["platform"])
            else:
                raise ValueError("each target needs an account_id or a platform")
            platform = acct["platform"] if acct else spec["platform"]
            account_id = acct["id"] if acct else None
            when = spec.get("scheduled_at")
            if when is None and (platform, account_id or "") not in keep_time_for:
                if acct is None:
                    raise ValueError(f"there is no {platform} account yet, so its next slot is unknown; give a time")
                taken = handed_out.setdefault(acct["id"], set())
                when = self.accounts.next_slot(acct["id"], taken=taken)
                taken.add(iso(when))
            out.append({"platform": platform, "account_id": account_id, "scheduled_at": when})
        return out

    def approve(self, clip_id: str, opts: dict[str, Any]) -> str:
        """Schedule the clip that was reviewed: its finished render becomes a post. The clip and its
        post change together or not at all.

        `opts["targets"]` says where it goes (see resolve_targets); the short form `platforms` +
        `scheduled_at` is also accepted. Optional: edited title / description / tags, visibility.
        Raises KeyError / StateError / ValueError."""
        clip = self.store.get_clip(clip_id)
        src = self.store.get_source(clip.source_id)
        sdir = self.store.source_dir(src.id)
        text = {k: opts[k] for k in TEXT_FIELDS if opts.get(k) is not None}
        specs = opts.get("targets") or [{"platform": p, "scheduled_at": opts.get("scheduled_at")}
                                        for p in opts.get("platforms") or []]
        if not specs:
            raise ValueError("choose at least one account to post to")
        targets = self.resolve_targets(specs)
        with self.store.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status, render_file FROM clips WHERE id = ?", (clip_id,)).fetchone()
            if row["status"] != "review" or not row["render_file"]:
                conn.execute("ROLLBACK")
                raise StateError(f"clip is {row['status']}; only clips waiting for review can be approved")
            post_id = self.queue.create_post(
                title=text.get("title", clip.title), description=text.get("description", clip.description),
                tags=text.get("tags", clip.tags), targets=targets, file_path=sdir / row["render_file"],
                clip_id=clip.id, conn=conn,
                extra={"source_url": src.url or None, "virality_score": clip.virality_score,
                       "visibility": opts.get("visibility")})
            self.store.update_clip(clip.id, conn=conn, status="scheduled", error=None, post_id=post_id, **text)
            conn.execute("COMMIT")
        self.cleanup_source(src.id)
        return post_id

    def edit_post(self, post_id: str, changes: dict[str, Any]) -> None:
        """Change a scheduled post: text, one time for everything still waiting, or the set of targets
        (new accounts get their next free slot unless a time is given). Raises KeyError / PostBusy / ValueError."""
        post = self.queue.get_post(post_id)
        if post is None:
            raise KeyError(post_id)
        changes = dict(changes)
        if changes.get("targets") is not None:
            existing = {(t["platform"], t["account_id"] or "") for t in post["targets"]}
            changes["targets"] = self.resolve_targets(changes["targets"], keep_time_for=existing)
        self.queue.update_post(post_id, **changes)

    def after_upload(self, row: dict[str, Any], outcome: str, detail: str) -> None:
        """Called by the scheduler after every upload attempt: tell the owner, and once a clip is out
        everywhere remove the video files it no longer needs."""
        acct = None
        if row.get("account_id"):
            try:
                acct = self.accounts.get(row["account_id"])
            except KeyError:
                pass
        where = f"{row['platform'].capitalize()}" + (f" ({acct['name']})" if acct else "")
        data = {"post_id": row["id"], "clip_id": row.get("clip_id"), "platform": row["platform"],
                "account_id": row.get("account_id")}
        if outcome == "completed":
            self.store.add_event("upload_done", f"Posted to {where}", row["title"], {**data, "url": detail or None})
            post = self.queue.get_post(row["id"])
            if post and post["status"] == "completed":
                self._cleanup_posted(post)
        else:
            final = outcome == "failed"
            self.store.add_event("upload_failed",
                                 f"Upload to {where} failed" + ("" if final else "; it will be tried again"),
                                 f"{row['title']}: {detail}"[:1000], {**data, "final": final})

    def _cleanup_posted(self, post: dict[str, Any]) -> None:
        """The clip is out on every platform it was meant for: drop its video files, keep its thumbnail."""
        Path(post["file_path"]).unlink(missing_ok=True)
        if not post.get("clip_id"):
            return
        try:
            clip = self.store.get_clip(post["clip_id"])
        except KeyError:
            return
        sdir = self.store.source_dir(clip.source_id)
        if clip.slice_file:
            (sdir / clip.slice_file).unlink(missing_ok=True)
        rdir = sdir / "renders" / clip.candidate_id
        if rdir.is_dir():
            for f in rdir.iterdir():
                if f.name != "thumb.jpg":
                    f.unlink(missing_ok=True)
        self.store.update_clip(clip.id, status="posted", slice_file=None, render_file=None)
        log.info("Clip %s is posted everywhere; removed its video files", clip.id[:8])

    def cancel_post(self, post_id: str) -> dict[str, Any]:
        """Stop a scheduled post. Uploads that have not happened are dropped; if nothing was posted yet
        the clip goes back to review. Raises KeyError / PostBusy."""
        with self.store.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self.queue.cancel_post(post_id, conn)
                if result["deleted"] and result["clip_id"]:
                    self.store.update_clip(result["clip_id"], conn=conn, status="review", post_id=None)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return result

    def cleanup_source(self, source_id: str) -> bool:
        """Once every clip of a finished source is decided, remove what only undecided clips needed:
        the downloaded audio and, if the file came through the inbox, the original video. Slices of
        scheduled clips stay until they are posted. Returns whether anything was cleaned now."""
        src = self.store.get_source(source_id)
        if src.status != "ready" or src.cleaned_at or self.store.undecided_clips(source_id):
            return False
        sdir = self.store.source_dir(source_id)
        shutil.rmtree(sdir / "source", ignore_errors=True)
        (sdir / AUDIO_DONE).unlink(missing_ok=True)
        if src.owns_file and src.file_path:
            Path(src.file_path).unlink(missing_ok=True)
        self.store.update_source(source_id, cleaned_at=iso(utcnow()))
        log.info("Source %s is fully decided; removed its source files", source_id[:8])
        return True

    def render_clip(self, clip: Clip) -> tuple[Path, str]:
        """Render `clip` exactly as its saved settings say. Returns (file, encoder used)."""
        from clip_pipeline.analysis.transcriber import Transcript
        from clip_pipeline.render import face_tracker, subtitle_gen, video_renderer

        sdir = self.store.source_dir(clip.source_id)
        if not clip.slice_file or clip.slice_start is None or clip.slice_end is None:
            raise RuntimeError("Clip has no video slice yet")
        slice_path = sdir / clip.slice_file
        start, end = max(clip.start, clip.slice_start), min(clip.end, clip.slice_end)
        if end - start < MIN_CLIP_SECONDS:
            raise ValueError(f"clip must be at least {MIN_CLIP_SECONDS:g} seconds and inside the slice")
        rdir = sdir / "renders" / clip.candidate_id
        rdir.mkdir(parents=True, exist_ok=True)

        plan = face_tracker.analyze_video(slice_path, aspect=clip.aspect, content_type=clip.content_type,
                                          layout=clip.layout)
        _write_json(rdir / "layout.json", plan.to_dict())

        subs = None
        if clip.subtitle_style != "none" or clip.hook_title:
            words = []
            if clip.subtitle_style != "none":
                transcripts = json.loads((sdir / "transcripts.json").read_text(encoding="utf-8"))
                words = [w for t in map(Transcript.from_dict, transcripts) if t.candidate_id == clip.candidate_id
                         for w in t.words]
            out_w, out_h = face_tracker.OUTPUT_SIZES[clip.aspect]
            subs = subtitle_gen.write_ass(rdir / video_renderer.SUBTITLE_FILENAME, words, start, end,
                                          width=out_w, height=out_h, style=clip.subtitle_style,
                                          hook_title=clip.hook_title or None, font=self.s.caption_font)

        # A new file name per render: the app never plays a stale cached copy after a re-trim.
        for old in rdir.glob("*.mp4"):
            old.unlink(missing_ok=True)
        out = rdir / f"{clip.candidate_id}_{clip.aspect.replace(':', 'x')}_{int(time.time() * 1000) % 10 ** 9}.mp4"
        req = video_renderer.RenderRequest(
            source=slice_path, output=out,
            trim_start=start - clip.slice_start, trim_end=end - clip.slice_start,
            aspect=clip.aspect, plan=plan, subtitles=subs,
        )
        return video_renderer.render(req)


OVERLAP_LIMIT = 0.5


def mark_overlaps(rows: list[dict[str, Any]]) -> None:
    """Two clips that share more than half of the shorter one are the same moment: keep the higher
    scored one for review and mark the other `duplicate`."""
    kept: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: -r["virality_score"]):
        if row["status"] != "candidate":
            continue
        length = row["end"] - row["start"]
        for other in kept:
            shared = min(row["end"], other["end"]) - max(row["start"], other["start"])
            if shared > OVERLAP_LIMIT * min(length, other["end"] - other["start"]):
                row["status"] = "duplicate"
                break
        else:
            kept.append(row)


def _write_json(path: Path, data: Any) -> None:
    """Write-then-rename, so a crash mid-write can never leave a half file that a resume would trust."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
