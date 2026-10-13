"""Stage 3a: faster-whisper over *only* the flagged candidate windows.

Runs on CPU with int8 quantisation: CTranslate2 uses AVX-512 kernels on Zen 5,
which beats any DirectML path for Whisper (CTranslate2 has no DirectML backend).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Literal

import numpy as np

from clip_pipeline.config import get_settings
from clip_pipeline.analysis.audio_analyzer import ANALYSIS_SR, Candidate, load_audio_segment

log = logging.getLogger(__name__)

ContentType = Literal["podcast", "gaming", "film", "music", "sports", "vlog", "education", "other"]
CONTENT_TYPES: tuple[str, ...] = ("podcast", "gaming", "film", "music", "sports", "vlog", "education", "other")
ProgressFn = Callable[[float, str], None]

# The uploader's own category is the best free signal for what a video is (yt-dlp info.json "categories").
# Catch-all categories (Entertainment, Comedy, News) are left out so they fall through to the speech vote.
_CATEGORY_TYPES: dict[str, ContentType] = {
    "gaming": "gaming",
    "film & animation": "film", "movies": "film", "shows": "film", "trailers": "film",
    "music": "music",
    "sports": "sports",
    "education": "education", "howto & style": "education", "science & technology": "education",
    "people & blogs": "vlog", "travel & events": "vlog", "autos & vehicles": "vlog", "pets & animals": "vlog",
}

# Conversational speech runs ~130-170 WPM; gameplay commentary is bursty and sparse.
PODCAST_MIN_WPM = 110.0
PODCAST_MIN_SPEECH_RATIO = 0.55


@dataclass
class Word:
    start: float  # absolute seconds in the source stream
    end: float
    text: str
    probability: float = 1.0


@dataclass
class Transcript:
    candidate_id: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)
    language: str | None = None

    @property
    def text(self) -> str:
        return " ".join(w.text.strip() for w in self.words).strip()

    @property
    def window(self) -> float:
        return max(self.end - self.start, 1e-6)

    @property
    def wpm(self) -> float:
        return len(self.words) / (self.window / 60.0)

    @property
    def speech_ratio(self) -> float:
        """Fraction of the window covered by words (overlaps merged)."""
        covered, cursor = 0.0, self.start
        for w in sorted(self.words, key=lambda w: w.start):
            s, e = max(w.start, cursor), max(w.end, cursor)
            covered += max(0.0, e - s)
            cursor = max(cursor, w.end)
        return min(covered / self.window, 1.0)

    @property
    def content_type(self) -> ContentType:
        """Speech-density guess for this window only (podcast = dense talk, gaming = sparse)."""
        return classify_window(self.wpm, self.speech_ratio)

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words if w.end > start and w.start < end]

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(text=self.text, wpm=round(self.wpm, 1), speech_ratio=round(self.speech_ratio, 3),
                 content_type=self.content_type)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        return cls(candidate_id=d["candidate_id"], start=d["start"], end=d["end"],
                   words=[Word(**{k: w[k] for k in ("start", "end", "text", "probability")}) for w in d["words"]],
                   language=d.get("language"))


def classify_window(wpm: float, speech_ratio: float) -> ContentType:
    if wpm >= PODCAST_MIN_WPM and speech_ratio >= PODCAST_MIN_SPEECH_RATIO:
        return "podcast"
    return "gaming"


def content_type_from_metadata(info: dict | None) -> ContentType | None:
    """Source metadata -> content type, or None when it says nothing useful."""
    info = info or {}
    for cat in info.get("categories") or []:
        hit = _CATEGORY_TYPES.get(str(cat).strip().lower())
        if hit:
            return hit
    if "twitch" in str(info.get("extractor") or "").lower():
        return "gaming"
    return None


def classify_stream(transcripts: Iterable[Transcript], info: dict | None = None) -> ContentType:
    """Source metadata first; otherwise a duration-weighted speech-density vote across candidate
    windows. Sparse speech alone doesn't prove gameplay, so without metadata it is just "other"."""
    from_meta = content_type_from_metadata(info)
    if from_meta:
        return from_meta
    weights = {"podcast": 0.0, "gaming": 0.0}
    for t in transcripts:
        weights[t.content_type] += t.window
    return "podcast" if weights["podcast"] > weights["gaming"] else "other"


_model = None
_model_lock = threading.Lock()


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            s = get_settings()
            log.info("Loading faster-whisper %s (%s/%s, %d threads)", s.whisper_model, s.whisper_device,
                     s.whisper_compute_type, s.whisper_cpu_threads)
            _model = WhisperModel(s.whisper_model, device=s.whisper_device,
                                  compute_type=s.whisper_compute_type, cpu_threads=s.whisper_cpu_threads)
        return _model


def transcribe_array(model, audio: np.ndarray, offset: float, candidate_id: str = "",
                     language: str | None = None) -> Transcript:
    segments, info = model.transcribe(
        audio, language=language, word_timestamps=True, vad_filter=True,
        beam_size=1, condition_on_previous_text=False,
    )
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append(Word(round(offset + w.start, 3), round(offset + w.end, 3), text,
                                  round(float(w.probability), 3)))
    return Transcript(candidate_id, offset, offset + audio.size / ANALYSIS_SR, words,
                      getattr(info, "language", None))


# ---------------------------------------------------------------------------- which language is spoken
#
# Whisper guesses one language per stretch of audio, from whatever it hears first - so a song, or a
# line in another language, can flip the captions of a whole clip. Instead we first find the languages
# that carry a real share of the video's speech, and then only ever choose among those:
#
#   * one such language (the usual case): everything is transcribed in it;
#   * several (e.g. half Turkish, half English): each stretch of speech between pauses takes the
#     likeliest of them, so captions follow the speaker sentence by sentence.

MIN_STRETCH_SECONDS = 4.0     # shorter than this is too little to judge: it takes its neighbour's language
MAX_STRETCH_SECONDS = 28.0    # Whisper listens in 30 s pieces
DETECT_SECONDS = 30.0


def speech_stretches(audio: np.ndarray) -> list[tuple[float, float]]:
    """(start, end) in seconds of each stretch of speech between pauses, found with Whisper's own VAD."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    opts = VadOptions(min_silence_duration_ms=500, max_speech_duration_s=MAX_STRETCH_SECONDS)
    return [(s["start"] / ANALYSIS_SR, s["end"] / ANALYSIS_SR) for s in get_speech_timestamps(audio, opts)]


def language_probabilities(model, audio: np.ndarray) -> dict[str, float]:
    """How likely each language is for (the first 30 s of) this audio."""
    _, _, probs = model.detect_language(audio[: int(DETECT_SECONDS * ANALYSIS_SR)])
    return {lang: float(p) for lang, p in probs}


def language_shortlist(weights: dict[str, float], min_share: float) -> list[str]:
    """Languages that carry at least `min_share` of the speech, heaviest first. Never empty unless
    there was no speech at all."""
    total = sum(weights.values())
    if total <= 0:
        return []
    ranked = sorted(weights, key=lambda k: -weights[k])
    return [ranked[0]] + [k for k in ranked[1:] if weights[k] / total >= min_share]


def pick_language(probs: dict[str, float], shortlist: list[str]) -> str:
    """The likeliest language for this audio among the ones the video really uses."""
    return max(shortlist, key=lambda lang: probs.get(lang, 0.0))


def plan_language_runs(stretches: list[tuple[float, float]], languages: list[str | None],
                       fallback: str) -> list[tuple[float, float, str]]:
    """Give every stretch a language (`None` = too short to judge: use the stretch before it, else the
    one after) and join neighbours that share one into runs of (start, end, language)."""
    filled: list[str | None] = list(languages)
    for i, lang in enumerate(filled):
        if lang is None and i and filled[i - 1]:
            filled[i] = filled[i - 1]
    for i in range(len(filled) - 2, -1, -1):
        if filled[i] is None and filled[i + 1]:
            filled[i] = filled[i + 1]
    runs: list[tuple[float, float, str]] = []
    for (a, b), lang in zip(stretches, filled):
        lang = lang or fallback
        if runs and runs[-1][2] == lang:
            runs[-1] = (runs[-1][0], b, lang)
        else:
            runs.append((a, b, lang))
    return runs


def _speech_only(audio: np.ndarray, stretches: list[tuple[float, float]]) -> np.ndarray:
    parts = [audio[int(a * ANALYSIS_SR): int(b * ANALYSIS_SR)] for a, b in stretches]
    return np.concatenate(parts) if parts else audio[:0]


def transcribe_candidates(audio_path: Path | str, candidates: list[Candidate],
                          progress: ProgressFn | None = None, model=None) -> list[Transcript]:
    model = model or get_model()
    s = get_settings()
    n = max(len(candidates), 1)

    def window(cand: Candidate, audio: np.ndarray, language: str | None) -> Transcript:
        t = transcribe_array(model, audio, cand.start, cand.id, language)
        t.end = cand.end  # window bounds are the candidate's, not the decoded length
        return t

    if s.whisper_language or not hasattr(model, "detect_language"):
        # A language was forced in the settings: no guessing at all.
        out = []
        for i, cand in enumerate(candidates):
            out.append(window(cand, load_audio_segment(audio_path, cand.start, cand.end), s.whisper_language))
            if progress:
                progress((i + 1) / n, "whisper")
        return out

    # ---- pass 1: how much of the video's speech is in each language
    audios: list[np.ndarray] = []
    all_stretches: list[list[tuple[float, float]]] = []
    weights: dict[str, float] = {}
    for i, cand in enumerate(candidates):
        audio = load_audio_segment(audio_path, cand.start, cand.end)
        stretches = speech_stretches(audio)
        audios.append(audio)
        all_stretches.append(stretches)
        seconds = sum(b - a for a, b in stretches)
        if seconds >= 1.0:
            for lang, p in language_probabilities(model, _speech_only(audio, stretches)).items():
                weights[lang] = weights.get(lang, 0.0) + p * seconds
        if progress:
            progress(0.25 * (i + 1) / n, "listening for the language")
    shortlist = language_shortlist(weights, s.language_min_share)
    total = sum(weights.values()) or 1.0
    log.info("Languages spoken: %s", ", ".join(f"{k} {100 * weights[k] / total:.0f}%" for k in shortlist) or "none")

    # ---- pass 2: transcribe, never leaving the shortlist
    out = []
    for i, (cand, audio, stretches) in enumerate(zip(candidates, audios, all_stretches)):
        if len(shortlist) <= 1 or not stretches:
            out.append(window(cand, audio, shortlist[0] if shortlist else None))
        else:
            judged = [pick_language(language_probabilities(model, audio[int(a * ANALYSIS_SR): int(b * ANALYSIS_SR)]),
                                    shortlist) if b - a >= MIN_STRETCH_SECONDS else None
                      for a, b in stretches]
            words: list[Word] = []
            seconds_by_language: dict[str, float] = {}
            for a, b, lang in plan_language_runs(stretches, judged, shortlist[0]):
                piece = audio[int(a * ANALYSIS_SR): int(b * ANALYSIS_SR)]
                words += transcribe_array(model, piece, cand.start + a, cand.id, lang).words
                seconds_by_language[lang] = seconds_by_language.get(lang, 0.0) + (b - a)
            out.append(Transcript(cand.id, cand.start, cand.end, sorted(words, key=lambda w: w.start),
                                  max(seconds_by_language, key=seconds_by_language.get)))
        if progress:
            progress(0.25 + 0.75 * (i + 1) / n, "whisper")
    return out
