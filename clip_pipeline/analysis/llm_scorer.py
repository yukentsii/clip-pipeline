"""Stage 3b: semantic scoring with the Gemini free tier (google-genai SDK).

The model receives candidate transcripts + energy markers and must answer with
a strict JSON schema. Every response is re-validated with pydantic, clamped
to the candidate window and snapped to word boundaries. Without an API key
(or after repeated failures) we fall back to a deterministic heuristic so the
pipeline still produces clips at $0 and offline.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from clip_pipeline.config import get_settings
from clip_pipeline.analysis.audio_analyzer import Candidate
from clip_pipeline.analysis.transcriber import CONTENT_TYPES, ContentType, Transcript

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]

MIN_CLIP = 15.0
TARGET_MIN = 30.0
TARGET_MAX = 60.0
MAX_CLIP = 90.0
BATCH_SIZE = 13             # free-tier models allow ~20 requests/day each, so spend few requests per job
MAX_RETRIES = 3             # per model, for server hiccups / malformed JSON only (rate limits move on at once)
MAX_BAD_JSON_MODELS = 2     # models allowed to return unusable JSON for one batch before scoring it locally
MINUTE_LIMIT_MAX_DELAY = 120.0   # a 429 asking for longer than this is the per-day quota
MAX_QUOTA_WAIT = 65.0       # longest we sit out per-minute limits when no other model is left
MAX_TIMEOUTS_PER_MODEL = 2  # unanswered requests to one model before moving to the next
TIMED_OUT_BENCH_SECONDS = 300.0   # how long a model that never answered is left alone

_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")


def parse_timestamp(value: str | float | int) -> float:
    """'HH:MM:SS', 'MM:SS', 'H:MM:SS.ms' or plain seconds -> seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = _TS_RE.match(s)
    if not m:
        raise ValueError(f"Bad timestamp: {value!r}")
    h, mnt, sec = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + float(sec)


def format_timestamp(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ------------------------------------------------------------------ wire schema (sent to Gemini)
# Kept to primitive types so the SDK -> OpenAPI schema conversion is lossless.

class LLMClip(BaseModel):
    candidate_id: str
    title: str
    hook_title: str
    description: str
    tags: list[str]
    hook_score: int
    engagement_score: int
    value_score: int
    shareability_score: int
    content_type: str
    start_time: str
    end_time: str
    hook_reason: str


class LLMClipBatch(BaseModel):
    clips: list[LLMClip]


class GeminiUnavailable(RuntimeError):
    """The API kept refusing (quota / outage) after every retry: later batches would fail the same way."""
    timed_out = False   # True when the model simply never answered


# ------------------------------------------------------------------ validated domain model

SCORE_FIELDS = ("hook_score", "engagement_score", "value_score", "shareability_score")
SCORE_PART_MAX = 25
HOOK_TITLE_MAX_WORDS = 9
HOOK_TITLE_MAX_CHARS = 60

_TYPE_ALIASES = {
    "discussion": "podcast", "talk": "podcast", "irl_talk": "podcast", "interview": "podcast",
    "movie": "film", "movies": "film", "tv": "film", "series": "film", "show": "film", "scripted": "film",
    "game": "gaming", "gameplay": "gaming", "esports": "gaming",
    "sport": "sports", "irl": "vlog", "tutorial": "education", "lecture": "education",
}


def sanitize_hook_title(raw) -> str:
    """One short plain-text headline: no hashtags, wrapping quotes, line breaks or trailing punctuation."""
    words = [w for w in re.sub(r"\s+", " ", str(raw or "")).split(" ") if w and not w.startswith("#")]
    text = " ".join(words).strip("\"“”‘’ ").strip()
    words = text.split(" ")[:HOOK_TITLE_MAX_WORDS] if text else []
    while words and len(" ".join(words)) > HOOK_TITLE_MAX_CHARS:
        words.pop()
    return " ".join(words).rstrip(" ,;:-")


class ScoredClip(BaseModel):
    candidate_id: str
    title: str = Field(min_length=1, max_length=100)
    hook_title: str = ""
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list)
    virality_score: int = Field(default=0, ge=0, le=100)
    # Four 0-25 parts that sum to virality_score. None for heuristic clips: there is nothing to break down.
    hook_score: int | None = None
    engagement_score: int | None = None
    value_score: int | None = None
    shareability_score: int | None = None
    content_type: ContentType
    start_time: str
    end_time: str
    hook_reason: str = ""
    start: float = 0.0
    end: float = 0.0
    source: Literal["gemini", "heuristic"] = "gemini"
    model: str = ""  # which Gemini model scored it

    @field_validator("title", "description", "hook_reason", mode="before")
    @classmethod
    def _strip(cls, v):
        return str(v or "").strip()

    @field_validator("hook_title", mode="before")
    @classmethod
    def _hook_title(cls, v):
        return sanitize_hook_title(v)

    @field_validator(*SCORE_FIELDS, mode="before")
    @classmethod
    def _part(cls, v):
        if v is None:
            return None
        try:
            return max(0, min(SCORE_PART_MAX, int(round(float(v)))))
        except (TypeError, ValueError):
            return None

    @model_validator(mode="after")
    def _sum_parts(self):
        parts = [getattr(self, f) for f in SCORE_FIELDS]
        if all(p is not None for p in parts):
            self.virality_score = sum(parts)
        else:
            for f in SCORE_FIELDS:
                setattr(self, f, None)
        return self

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        if isinstance(v, str):
            v = re.split(r"[,\s]+", v)
        tags = []
        for t in v or []:
            t = re.sub(r"[^\w\-]", "", str(t).lstrip("#"))
            if t and t.lower() not in {x.lower() for x in tags}:
                tags.append(t)
        return tags[:15]

    @field_validator("virality_score", mode="before")
    @classmethod
    def _score(cls, v):
        try:
            return max(0, min(100, int(round(float(v)))))
        except (TypeError, ValueError):
            return 0

    @field_validator("content_type", mode="before")
    @classmethod
    def _ctype(cls, v):
        v = str(v or "").strip().lower()
        v = _TYPE_ALIASES.get(v, v)
        return v if v in CONTENT_TYPES else "other"

    @property
    def duration(self) -> float:
        return self.end - self.start


# ------------------------------------------------------------------ prompt

SYSTEM_INSTRUCTION = """You are a senior short-form video editor who picks viral clips from long videos and livestreams.
You get candidate windows found by audio-loudness and chat-reaction peaks, with transcripts.
For EACH candidate decide whether it contains a clip worth posting on TikTok/Reels/Shorts.
Rules:
- Pick ONE contiguous sub-window per candidate, 30-60 seconds, fully inside that candidate's window.
- The first 3 seconds must hook the viewer (a question, a bold claim, a reaction, a punchline setup).
- End right after the payoff - never mid-sentence.
- content_type is what the source actually is, judged from its title, category and the transcript (never
  from the energy markers alone). Use exactly one of:
  podcast (conversation, interview, commentary), gaming (gameplay, game streams), film (movies, TV, scripted
  scenes, animation), music (performances, music videos), sports, vlog (IRL, travel, challenges, daily life),
  education (tutorials, lectures, explainers), other.
- podcast / education: prioritise semantic hooks, hot takes, stories with a payoff.
- gaming / sports: prioritise energy bursts, clutch plays, fails, big reactions; chat reactions are strong evidence.
- film: prioritise self-contained scenes - a confrontation, a twist, a memorable line - that work without context.
- Score four parts, each an integer 0-25. Their sum is the clip's 0-100 virality score, so be harsh:
  a sum below 40 means skip-worthy, above 80 means exceptional.
  hook_score: do the first 3 seconds stop the scroll?
  engagement_score: does tension, emotion or pace hold attention to the end?
  value_score: how strong is the payoff (funny, insightful, impressive, satisfying)?
  shareability_score: would someone send it to a friend or comment on it?
- title <= 70 chars, punchy, no clickbait lies. description 1-2 sentences. 3-8 tags without '#'.
- hook_title is a 3-8 word headline burned onto the top of the video for its first seconds. It must create
  curiosity without spoiling the payoff and stay true to the clip. Plain words only: no hashtags, emoji or quotes.
- start_time/end_time are absolute stream timestamps formatted HH:MM:SS.
- Return clips only for candidates provided; echo candidate_id exactly."""


def _transcript_lines(t: Transcript | None, every: float = 8.0) -> str:
    if t is None or not t.words:
        return "(no speech detected)"
    lines, bucket, bucket_start = [], [], t.words[0].start
    for w in t.words:
        if bucket and w.start - bucket_start >= every:
            lines.append(f"[{format_timestamp(bucket_start)}] {' '.join(bucket)}")
            bucket, bucket_start = [], w.start
        bucket.append(w.text)
    if bucket:
        lines.append(f"[{format_timestamp(bucket_start)}] {' '.join(bucket)}")
    return "\n".join(lines)


def build_prompt(candidates: list[Candidate], transcripts: dict[str, Transcript],
                 stream_title: str = "", stream_type: ContentType = "other", categories: list[str] | None = None) -> str:
    parts = [f"Source title: {stream_title or 'unknown'}",
             f"Uploader's category: {', '.join(categories) if categories else 'unknown'}",
             f"Our guess at the content type (correct it if wrong): {stream_type}", ""]
    for c in candidates:
        t = transcripts.get(c.id)
        markers = {
            "audio_energy_z": c.audio_score, "chat_reaction_z": c.chat_score,
            "peak_loudness_lufs": c.peak_lufs, "chat_messages": c.chat_messages,
            "top_chat_reactions": c.top_reactions,
            "words_per_minute": round(t.wpm, 1) if t else 0,
        }
        parts += [
            f"=== candidate_id: {c.id} ===",
            f"window: {format_timestamp(c.start)} -> {format_timestamp(c.end)} "
            f"(peak at {format_timestamp(c.peak_time)})",
            f"energy markers: {json.dumps(markers)}",
            "transcript:", _transcript_lines(t), "",
        ]
    return "\n".join(parts)


# ------------------------------------------------------------------ post-processing

def finalize_clip(raw: LLMClip | dict, candidate: Candidate, transcript: Transcript | None,
                  source: str = "gemini", model: str = "") -> ScoredClip:
    data = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw)
    try:
        start = parse_timestamp(data.get("start_time", candidate.start))
        end = parse_timestamp(data.get("end_time", candidate.end))
    except ValueError:
        start, end = candidate.start, candidate.end
    start, end = clamp_window(start, end, candidate.start, candidate.end)
    if transcript is not None:
        start, end = snap_to_words(start, end, transcript, candidate.start, candidate.end)
    data.update(start=round(start, 2), end=round(end, 2), candidate_id=candidate.id,
                start_time=format_timestamp(start), end_time=format_timestamp(end), source=source, model=model)
    return ScoredClip.model_validate(data)


def clamp_window(start: float, end: float, lo: float, hi: float) -> tuple[float, float]:
    """Force [start, end] inside [lo, hi] with MIN_CLIP <= length <= MAX_CLIP."""
    if end < start:
        start, end = end, start
    start, end = max(lo, start), min(hi, end)
    avail = hi - lo
    if end - start < MIN_CLIP:
        want = min(max(TARGET_MIN, MIN_CLIP), avail)
        mid = (start + end) / 2
        start = max(lo, min(mid - want / 2, hi - want))
        end = start + want
    if end - start > MAX_CLIP:
        end = start + MAX_CLIP
    return start, end


def snap_to_words(start: float, end: float, transcript: Transcript, lo: float, hi: float,
                  tolerance: float = 1.5) -> tuple[float, float]:
    """Don't cut mid-word: pull start back to a word start, push end to a word end."""
    for w in transcript.words:
        if w.start < start < w.end and start - w.start <= tolerance:
            start = max(lo, w.start - 0.1)
            break
    for w in transcript.words:
        if w.start < end < w.end and w.end - end <= tolerance:
            end = min(hi, w.end + 0.15)
            break
    return start, end


def heuristic_clip(candidate: Candidate, transcript: Transcript | None, rank_pct: float,
                   stream_type: ContentType) -> ScoredClip:
    """Deterministic fallback: ~45 s window with the peak at ~65 %, score from rank."""
    length = min(45.0, candidate.duration)
    start = candidate.peak_time - 0.65 * length
    start = max(candidate.start, min(start, candidate.end - length))
    end = start + length
    text = transcript.text if transcript else ""
    words = text.split()
    title = " ".join(words[:9]).strip(" ,.") or f"Highlight at {format_timestamp(candidate.peak_time)}"
    ctype = stream_type
    tags = ["shorts", "clips", ctype] + candidate.top_reactions[:3]
    raw = {
        "title": title[:70], "description": (" ".join(words[:30]) + ("…" if len(words) > 30 else "")) or title,
        "tags": tags, "virality_score": int(round(40 + 55 * rank_pct)), "content_type": ctype,
        # plain seconds keep sub-second precision; finalize_clip reformats to HH:MM:SS
        "start_time": f"{start:.2f}", "end_time": f"{end:.2f}",
        "hook_reason": "Loudness/chat peak (heuristic: no LLM available)",
    }
    return finalize_clip(raw, candidate, transcript, source="heuristic")


def heuristic_scores(candidates: list[Candidate], transcripts: dict[str, Transcript],
                     stream_type: ContentType) -> list[ScoredClip]:
    ranked = sorted(candidates, key=lambda c: c.score)
    pct = {c.id: (i / max(len(ranked) - 1, 1)) for i, c in enumerate(ranked)}
    return [heuristic_clip(c, transcripts.get(c.id), pct[c.id], stream_type) for c in candidates]


# ------------------------------------------------------------------ Gemini call

def _code(exc: Exception):
    return getattr(exc, "code", None) or getattr(exc, "status_code", None)


def _is_quota(exc: Exception) -> bool:
    return _code(exc) == 429 or "resource_exhausted" in str(exc).lower()


def _is_timeout(exc: Exception) -> bool:
    """The request got no answer in time. Recognised by type first: httpx words it "timed out", which a
    search for "timeout" in the message would miss."""
    import httpx

    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return True
    msg = str(exc).lower()
    return "timed out" in msg or "timeout" in msg or "deadline" in msg


def _is_transient(exc: Exception) -> bool:
    import httpx

    if _is_timeout(exc) or isinstance(exc, httpx.TransportError):   # no answer / connection dropped
        return True
    if _code(exc) in (408, 500, 502, 503, 504):
        return True
    msg = str(exc).lower()
    return any(s in msg for s in ("unavailable", "503"))


def _is_missing_model(exc: Exception) -> bool:
    """This key can't use that model (retired, renamed, not enabled): another model may still work."""
    msg = str(exc).lower()
    return _code(exc) in (403, 404) or "not_found" in msg or "permission_denied" in msg


_RETRY_DELAY_RE = re.compile(r"retryDelay'?\"?:\s*['\"]?(\d+(?:\.\d+)?)s")
_RETRY_IN_RE = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", re.I)


def retry_delay(exc: Exception) -> float | None:
    """Seconds the API asks us to wait, from a 429's RetryInfo ("retryDelay": "33s" / "retry in 20h52m10s")."""
    msg = str(exc)
    m = _RETRY_DELAY_RE.search(msg)
    if m:
        return float(m.group(1))
    m = _RETRY_IN_RE.search(msg)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))
    return None


class QuotaExceeded(RuntimeError):
    """One model's rate limit. `daily` = the requests-per-day quota, which won't come back during this job."""

    def __init__(self, model: str, exc: Exception):
        super().__init__(f"{model}: {exc}")
        delay = retry_delay(exc)
        # Per-minute limits ask for seconds; the per-day one asks for hours (it resets at midnight Pacific).
        self.daily = "perday" in str(exc).lower() or delay is None or delay > MINUTE_LIMIT_MAX_DELAY
        self.delay = delay if delay is not None else 60.0


class ModelPool:
    """The ordered Gemini models still worth calling during one scoring run."""

    def __init__(self, models, now=time.monotonic):
        self.models = [m for m in dict.fromkeys(models) if m]
        self._now = now
        self.dropped: dict[str, str] = {}      # model -> why it is out for the rest of the run
        self._benched: dict[str, float] = {}   # model -> when its per-minute limit clears

    def available(self) -> list[str]:
        now = self._now()
        return [m for m in self.models if m not in self.dropped and self._benched.get(m, 0.0) <= now]

    def drop(self, model: str, reason: str):
        self.dropped[model] = reason
        self._benched.pop(model, None)

    def bench(self, model: str, seconds: float):
        self._benched[model] = self._now() + max(1.0, seconds)

    def wait_needed(self) -> float | None:
        """Seconds until a benched model is back, or None when every model is out for good."""
        if not self._benched:
            return None
        return max(0.0, min(self._benched.values()) - self._now())

    def release(self):
        self._benched.clear()

    def summary(self) -> str:
        if not self.models:
            return "no Gemini models configured"
        reasons = set(self.dropped.values())
        if len(self.dropped) == len(self.models) and reasons == {"daily quota used up"}:
            return f"daily quota used up on all {len(self.models)} Gemini models (resets at midnight Pacific)"
        parts = [f"{m}: {self.dropped.get(m, 'rate limited')}" for m in self.models]
        return "no Gemini model could answer (" + "; ".join(parts) + ")"


StatusFn = Callable[[str], None]


def call_gemini(prompt: str, *, client=None, model: str | None = None, sleep=time.sleep,
                status: StatusFn | None = None, deadline: float | None = None,
                now: Callable[[], float] = time.monotonic) -> LLMClipBatch:
    """One model. A rate limit raises QuotaExceeded straight away (the caller has other models to try);
    only server hiccups, unanswered requests and malformed JSON are retried here.

    Every request has a time limit (GEMINI_TIMEOUT_SECONDS): without one, a reply that never comes would
    hold the worker - and every source queued behind it - forever. `deadline` (on the `now` clock) is
    when the caller stops waiting altogether; `status` is told what is being tried."""
    from google.genai import types

    settings = get_settings()
    model = model or settings.gemini_models[0]
    if client is None:
        from google import genai
        client = genai.Client(api_key=settings.gemini_api_key,
                              http_options=types.HttpOptions(timeout=int(settings.gemini_timeout_seconds * 1000)))
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=LLMClipBatch,
        temperature=0.4,
    )
    last_exc: Exception | None = None
    timeouts = 0
    for attempt in range(MAX_RETRIES):
        if deadline is not None and now() >= deadline:
            break
        if status:
            status(f"Scoring with {model}" + (f", attempt {attempt + 1}" if attempt else ""))
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=config)
            parsed = getattr(resp, "parsed", None)
            if isinstance(parsed, LLMClipBatch):
                return parsed
            return LLMClipBatch.model_validate_json(resp.text or "")
        except ValidationError as exc:  # malformed JSON: retry, then give up on this model
            last_exc = exc
            log.warning("%s returned invalid JSON (attempt %d): %s", model, attempt + 1, exc)
        except Exception as exc:  # noqa: BLE001 - SDK raises several error families
            last_exc = exc
            if _is_quota(exc):
                raise QuotaExceeded(model, exc) from exc
            if not _is_transient(exc):
                raise
            if _is_timeout(exc):
                timeouts += 1
                log.warning("%s did not answer within %.0f s (attempt %d)", model,
                            settings.gemini_timeout_seconds, attempt + 1)
                if status:
                    status(f"{model} did not answer; trying again")
                if timeouts >= MAX_TIMEOUTS_PER_MODEL:   # a model that is hanging rarely recovers in a minute
                    break
            else:
                log.warning("%s transient error (attempt %d): %s", model, attempt + 1, exc)
        if attempt + 1 < MAX_RETRIES:
            sleep(min(30.0, (2 ** attempt) * 4 + random.uniform(0, 2)))
    if isinstance(last_exc, ValidationError):
        raise RuntimeError(f"{model} returned unusable JSON {MAX_RETRIES} times: {last_exc}")
    unavailable = GeminiUnavailable(
        f"{model} did not answer in time ({timeouts} request(s) of {settings.gemini_timeout_seconds:.0f} s)"
        if timeouts else f"{model} failed after {MAX_RETRIES} attempts: {last_exc}")
    unavailable.timed_out = bool(timeouts)
    raise unavailable


def call_with_fallback(prompt: str, pool: ModelPool, *, client=None, sleep=time.sleep,
                       status: StatusFn | None = None, budget: float | None = None,
                       now: Callable[[], float] = time.monotonic) -> tuple[LLMClipBatch, str]:
    """Try each model in order until one answers; returns (reply, model that produced it).

    A model out of daily quota (or unknown to this key) is dropped for the rest of the run, so later
    batches go straight to the next one. A model that does not answer is set aside for a while and the
    next one is tried. If every model that is left is only per-minute limited we wait once for the limit
    to clear. The whole attempt is limited to `budget` seconds. Raises GeminiUnavailable when nothing is
    left or the time is up: the caller scores locally, so the queue always moves on."""
    waited = False
    bad_json = 0
    json_error: RuntimeError | None = None
    budget = get_settings().gemini_batch_budget_seconds if budget is None else budget
    deadline = now() + budget

    def out_of_time() -> GeminiUnavailable:
        return GeminiUnavailable(f"Gemini did not answer within {budget:.0f} s")

    while True:
        for model in pool.available():
            if now() >= deadline:
                raise out_of_time()
            try:
                return call_gemini(prompt, client=client, model=model, sleep=sleep, status=status,
                                   deadline=deadline, now=now), model
            except QuotaExceeded as exc:
                if exc.daily:
                    log.warning("%s: daily quota used up, trying the next model", model)
                    pool.drop(model, "daily quota used up")
                else:
                    log.warning("%s: per-minute limit hit, trying the next model", model)
                    pool.bench(model, exc.delay)
            except GeminiUnavailable as exc:
                log.warning("%s", exc)
                # Hanging models are left alone for longer, so later batches don't wait on them again.
                pool.bench(model, TIMED_OUT_BENCH_SECONDS if exc.timed_out else 60.0)
                if status and exc.timed_out:
                    status(f"{model} did not answer; trying the next model")
            except RuntimeError as exc:  # unusable JSON: the prompt itself may be the problem, so don't try them all
                bad_json += 1
                json_error = exc
                if bad_json >= MAX_BAD_JSON_MODELS:
                    raise
            except Exception as exc:  # noqa: BLE001
                if not _is_missing_model(exc):
                    raise
                log.warning("%s is not available to this API key, trying the next model", model)
                pool.drop(model, "not available to this API key")
        wait = pool.wait_needed()
        if now() >= deadline:
            raise out_of_time()
        if waited or wait is None or wait > MAX_QUOTA_WAIT:
            if json_error is not None:
                raise json_error
            raise GeminiUnavailable(pool.summary())
        if status:
            status("Waiting for Gemini's rate limit to clear")
        log.info("All remaining Gemini models are per-minute limited; waiting %.0f s", wait)
        sleep(wait + 1.0)
        pool.release()
        waited = True


def describe_failure(exc: Exception) -> str:
    """One short line for the UI explaining why clips were scored without the LLM."""
    msg = re.sub(r"\s+", " ", str(exc))
    if isinstance(exc, GeminiUnavailable):
        return "Gemini unavailable: " + msg[:300]
    return "Gemini error: " + msg[:160]


def split_batches(candidates: list[Candidate], size: int = BATCH_SIZE) -> list[list[Candidate]]:
    """As few requests as `size` allows, evenly filled (25 -> 13 + 12, not 13 + 12 leftovers of 1)."""
    if not candidates:
        return []
    n = -(-len(candidates) // size)
    per = -(-len(candidates) // n)
    return [candidates[i:i + per] for i in range(0, len(candidates), per)]


def models_used(clips: list[ScoredClip]) -> list[str]:
    return list(dict.fromkeys(c.model for c in clips if c.source == "gemini" and c.model))


def dominant_content_type(clips: list[ScoredClip]) -> ContentType | None:
    """What the LLM thinks the source is: majority vote over the clips it actually scored."""
    votes: dict[str, int] = {}
    for c in clips:
        if c.source == "gemini":
            votes[c.content_type] = votes.get(c.content_type, 0) + 1
    return max(votes, key=votes.get) if votes else None  # type: ignore[return-value]


def score_candidates(candidates: list[Candidate], transcripts: list[Transcript], *,
                     stream_title: str = "", stream_type: ContentType = "other",
                     categories: list[str] | None = None, notes: list[str] | None = None,
                     models: list[str] | None = None, status: StatusFn | None = None,
                     progress: ProgressFn | None = None, client=None, sleep=time.sleep) -> list[ScoredClip]:
    """Score with the first Gemini model in `models` (default: settings) that still has quota, moving
    down the list as models run out; whatever no model could score falls back to the local heuristic.
    `notes`, when given, collects human-readable reasons for any heuristic fallback; `status` is told
    which model is being tried, so whoever is watching can see why scoring is taking a while."""
    by_id = {t.candidate_id: t for t in transcripts}
    cand_by_id = {c.id: c for c in candidates}
    settings = get_settings()
    if not settings.gemini_api_key and client is None:
        log.info("GEMINI_API_KEY not set: using heuristic scoring")
        return sorted(heuristic_scores(candidates, by_id, stream_type), key=lambda c: -c.virality_score)

    results: dict[str, ScoredClip] = {}
    batches = split_batches(candidates)
    pool = ModelPool(models or settings.gemini_models)
    noted = False
    for bi, batch in enumerate(batches):
        try:
            reply, model = call_with_fallback(build_prompt(batch, by_id, stream_title, stream_type, categories),
                                              pool, client=client, sleep=sleep, status=status)
            for raw in reply.clips:
                cand = cand_by_id.get(raw.candidate_id)
                if cand is None or cand.id in results:
                    continue
                try:
                    results[cand.id] = finalize_clip(raw, cand, by_id.get(cand.id), model=model)
                except ValidationError as exc:
                    log.warning("Dropping invalid clip for %s: %s", raw.candidate_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.error("Gemini batch %d failed, using heuristics for it: %s", bi, exc)
            if not noted and notes is not None:
                notes.append(describe_failure(exc))
            noted = True
        # Any candidate the model skipped or that failed gets a heuristic score, flagged as such.
        ranked = sorted(candidates, key=lambda c: c.score)
        pct = {c.id: i / max(len(ranked) - 1, 1) for i, c in enumerate(ranked)}
        for c in batch:
            if c.id not in results:
                results[c.id] = heuristic_clip(c, by_id.get(c.id), pct[c.id] * 0.6, stream_type)
        if progress:
            progress((bi + 1) / len(batches), "llm scoring")
    return sorted(results.values(), key=lambda c: -c.virality_score)
