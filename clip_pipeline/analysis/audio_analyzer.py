"""Stage 2: math-first peak detection.

Audio is streamed through PyAV in small chunks (a 5 h stream never sits in RAM
as one array) and reduced to 100 ms blocks of plain and K-weighted (ITU-R
BS.1770) mean square. From those we derive 1 Hz series of short-term LUFS and
RMS energy, combine them with chat velocity / emote bursts, and pick the top-N
non-overlapping candidate windows.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

import numpy as np
from scipy import signal as sps

log = logging.getLogger(__name__)

ANALYSIS_SR = 16_000
BLOCK_SECONDS = 0.1
SILENCE_LUFS = -70.0

ProgressFn = Callable[[float, str], None]


# ============================================================================ audio I/O

def iter_audio_chunks(path: Path | str, sr: int = ANALYSIS_SR) -> Iterator[tuple[np.ndarray, float | None]]:
    """Yield (mono float32 chunk, source duration seconds) decoded with PyAV."""
    import av

    with av.open(str(path)) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        duration = None
        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        elif container.duration:
            duration = container.duration / 1_000_000
        resampler = av.AudioResampler(format="flt", layout="mono", rate=sr)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                yield out.to_ndarray().reshape(-1).astype(np.float32, copy=False), duration
        for out in resampler.resample(None):
            yield out.to_ndarray().reshape(-1).astype(np.float32, copy=False), duration


def load_audio_segment(path: Path | str, start: float, end: float, sr: int = ANALYSIS_SR) -> np.ndarray:
    """Decode only [start, end] seconds as mono float32 at `sr` (seek, don't scan)."""
    import av

    start = max(0.0, start)
    pieces: list[np.ndarray] = []
    with av.open(str(path)) as container:
        stream = next(s for s in container.streams if s.type == "audio")
        container.seek(int(start * av.time_base), any_frame=False, backward=True)
        resampler = av.AudioResampler(format="flt", layout="mono", rate=sr)
        t0: float | None = None
        collected = 0
        needed = None
        for frame in container.decode(stream):
            if t0 is None:
                t0 = float(frame.time) if frame.time is not None else start
                needed = int((end - t0) * sr) + sr
            for out in resampler.resample(frame):
                arr = out.to_ndarray().reshape(-1)
                pieces.append(arr)
                collected += arr.size
            if needed is not None and collected >= needed:
                break
        for out in resampler.resample(None):
            pieces.append(out.to_ndarray().reshape(-1))
    if not pieces:
        return np.zeros(0, dtype=np.float32)
    audio = np.concatenate(pieces).astype(np.float32, copy=False)
    offset = int(max(0.0, start - (t0 or start)) * sr)
    return audio[offset: offset + int((end - start) * sr)]


# ============================================================================ loudness

def k_weighting_sos(fs: int) -> np.ndarray:
    """BS.1770 K-weighting (pre-filter shelf + RLB high-pass) for any sample rate.
    Coefficient derivation follows libebur128, so 48 kHz matches the spec tables."""
    # Stage 1: high-shelf
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = math.tan(math.pi * f0 / fs)
    Vh = 10 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416
    a0 = 1.0 + K / Q + K * K
    b1 = [(Vh + Vb * K / Q + K * K) / a0, 2.0 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0]
    a1 = [1.0, 2.0 * (K * K - 1.0) / a0, (1.0 - K / Q + K * K) / a0]
    # Stage 2: high-pass
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = math.tan(math.pi * f0 / fs)
    a0 = 1.0 + K / Q + K * K
    b2 = [1.0, -2.0, 1.0]
    a2 = [1.0, 2.0 * (K * K - 1.0) / a0, (1.0 - K / Q + K * K) / a0]
    return np.array([b1 + a1, b2 + a2], dtype=np.float64)


def ms_to_lufs(ms: np.ndarray | float) -> np.ndarray:
    ms = np.asarray(ms, dtype=np.float64)
    with np.errstate(divide="ignore"):
        out = -0.691 + 10.0 * np.log10(ms)
    return np.maximum(out, SILENCE_LUFS)


def ms_to_db(ms: np.ndarray | float) -> np.ndarray:
    ms = np.asarray(ms, dtype=np.float64)
    with np.errstate(divide="ignore"):
        out = 10.0 * np.log10(ms)
    return np.maximum(out, -100.0)


@dataclass
class AudioFeatures:
    duration: float
    block_seconds: float
    block_ms: np.ndarray        # plain mean square per block
    block_ms_k: np.ndarray      # K-weighted mean square per block

    def per_second(self) -> tuple[np.ndarray, np.ndarray]:
        """(short-term LUFS over 3 s, RMS dBFS over 1 s), one value per second."""
        bps = int(round(1.0 / self.block_seconds))
        n_sec = int(math.ceil(self.duration)) or 1
        ms_k = _pad_to(self.block_ms_k, n_sec * bps)
        ms = _pad_to(self.block_ms, n_sec * bps)
        sec_k = ms_k.reshape(n_sec, bps).mean(axis=1)
        sec = ms.reshape(n_sec, bps).mean(axis=1)
        # short-term loudness = 3 s sliding window, centred on each second
        st = np.convolve(sec_k, np.ones(3) / 3.0, mode="same")
        return ms_to_lufs(st), ms_to_db(sec)


def _pad_to(arr: np.ndarray, n: int) -> np.ndarray:
    if arr.size >= n:
        return arr[:n]
    return np.concatenate([arr, np.zeros(n - arr.size, dtype=arr.dtype)])


def compute_features(chunks: Iterable[tuple[np.ndarray, float | None]], sr: int = ANALYSIS_SR,
                     progress: ProgressFn | None = None) -> AudioFeatures:
    """Reduce a stream of audio chunks to per-block mean squares (streaming, O(blocks) memory)."""
    sos = k_weighting_sos(sr)
    zi = np.zeros((sos.shape[0], 2))
    block = int(sr * BLOCK_SECONDS)
    carry_raw = np.zeros(0, dtype=np.float64)
    carry_k = np.zeros(0, dtype=np.float64)
    ms_list: list[np.ndarray] = []
    msk_list: list[np.ndarray] = []
    total = 0
    last_report = 0.0
    for chunk, src_duration in chunks:
        if chunk.size == 0:
            continue
        x = chunk.astype(np.float64)
        y, zi = sps.sosfilt(sos, x, zi=zi)
        total += x.size
        raw = np.concatenate([carry_raw, x])
        kw = np.concatenate([carry_k, y])
        n_full = raw.size // block
        if n_full:
            ms_list.append(np.mean(raw[: n_full * block].reshape(n_full, block) ** 2, axis=1))
            msk_list.append(np.mean(kw[: n_full * block].reshape(n_full, block) ** 2, axis=1))
        carry_raw = raw[n_full * block:]
        carry_k = kw[n_full * block:]
        if progress and src_duration:
            frac = min(total / sr / src_duration, 1.0)
            if frac - last_report >= 0.01:
                last_report = frac
                progress(frac, "math analysis")
    if carry_raw.size:
        ms_list.append(np.array([np.mean(carry_raw ** 2)]))
        msk_list.append(np.array([np.mean(carry_k ** 2)]))
    block_ms = np.concatenate(ms_list) if ms_list else np.zeros(0)
    block_ms_k = np.concatenate(msk_list) if msk_list else np.zeros(0)
    return AudioFeatures(duration=total / sr, block_seconds=BLOCK_SECONDS,
                         block_ms=block_ms, block_ms_k=block_ms_k)


# ============================================================================ chat

# token -> weight. Matched against whitespace tokens (case-insensitive) after
# stripping punctuation; phrases are matched as substrings.
EMOTE_WEIGHTS: dict[str, float] = {
    "omegalul": 3.0, "lulw": 2.5, "kekw": 2.5, "lul": 1.5, "kek": 1.5, "icant": 2.0,
    "lmao": 2.0, "lmfao": 2.5, "lol": 1.0, "xd": 1.0, "😂": 1.5, "🤣": 2.0, "💀": 2.0,
    ":face-with-tears-of-joy:": 1.5, ":rolling-on-the-floor-laughing:": 2.0,
    "pog": 2.0, "pogu": 2.5, "poggers": 2.5, "pogchamp": 2.5, "poggies": 2.0,
    "w": 1.5, "ww": 1.5, "www": 2.0, "l": 1.0, "ratio": 1.0,
    "monkas": 1.5, "pausechamp": 1.5, "aware": 1.0, "wtf": 1.5, "holy": 1.5, "sheesh": 1.5,
    "?": 0.5, "??": 1.0, "???": 1.5,
}
PHRASE_WEIGHTS: dict[str, float] = {
    "clip it": 4.0, "clip that": 4.0, "clipped": 2.5, "no way": 1.5, "oh my god": 1.5, "let's go": 1.5,
    "lets go": 1.5,
}
_STRIP_RE = re.compile(r"^[^\w?:😂🤣💀-]+|[^\w?:😂🤣💀-]+$")


@dataclass
class ChatMessage:
    t: float
    text: str


def _yt_runs_to_text(runs: list[dict]) -> str:
    parts = []
    for run in runs:
        if "text" in run:
            parts.append(run["text"])
        elif "emoji" in run:
            emoji = run["emoji"]
            shortcuts = emoji.get("shortcuts") or []
            label = shortcuts[0] if shortcuts else emoji.get("emojiId", "")
            parts.append(f" {label} ")
    return "".join(parts)


def _parse_youtube_line(obj: dict) -> list[ChatMessage]:
    action = obj.get("replayChatItemAction")
    if not action:
        return []
    try:
        t = int(action.get("videoOffsetTimeMsec") or obj.get("videoOffsetTimeMsec") or 0) / 1000.0
    except (TypeError, ValueError):
        return []
    out = []
    for act in action.get("actions", []):
        item = (act.get("addChatItemAction") or {}).get("item") or {}
        for key in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
            renderer = item.get(key)
            if renderer and "message" in renderer:
                out.append(ChatMessage(t, _yt_runs_to_text(renderer["message"].get("runs", []))))
    return out


def _parse_generic(obj: object) -> list[ChatMessage]:
    """Twitch rechat ({"comments": [...]}) or a plain list of {time, text}."""
    items = obj.get("comments") if isinstance(obj, dict) else obj
    out = []
    if not isinstance(items, list):
        return out
    for c in items:
        if not isinstance(c, dict):
            continue
        t = c.get("content_offset_seconds", c.get("time", c.get("t", c.get("offset"))))
        msg = c.get("message", c.get("text", ""))
        if isinstance(msg, dict):
            msg = msg.get("body", "")
        try:
            out.append(ChatMessage(float(t), str(msg)))
        except (TypeError, ValueError):
            continue
    return out


def parse_chat_file(path: Path | str | None) -> list[ChatMessage]:
    if not path:
        return []
    path = Path(path)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    messages: list[ChatMessage] = []
    stripped = text.lstrip()
    if stripped.startswith("[") or (stripped.startswith("{") and '"comments"' in stripped[:2000]):
        try:
            messages = _parse_generic(json.loads(text))
        except json.JSONDecodeError:
            messages = []
    if not messages:  # JSON-lines (yt-dlp live_chat.json)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                messages.extend(_parse_youtube_line(obj))
    messages.sort(key=lambda m: m.t)
    return messages


def score_message(text: str) -> tuple[float, list[str]]:
    lowered = text.lower()
    weight = 0.0
    hits: list[str] = []
    for phrase, w in PHRASE_WEIGHTS.items():
        if phrase in lowered:
            weight += w
            hits.append(phrase)
    seen: set[str] = set()
    for raw in lowered.split():
        tok = _STRIP_RE.sub("", raw)
        if tok.startswith("?") and set(tok) == {"?"}:
            tok = tok[:3]
        if tok in EMOTE_WEIGHTS and tok not in seen:  # spam of one emote counts once per message
            seen.add(tok)
            weight += EMOTE_WEIGHTS[tok]
            hits.append(tok)
    return weight, hits


@dataclass
class ChatSeries:
    counts: np.ndarray          # messages per second
    emote_weight: np.ndarray    # weighted reaction hits per second
    tokens: list[Counter]       # per-second hit tokens (for explaining a peak)


def chat_series(messages: list[ChatMessage], n_seconds: int) -> ChatSeries:
    counts = np.zeros(n_seconds)
    emotes = np.zeros(n_seconds)
    tokens: list[Counter] = [Counter() for _ in range(n_seconds)]
    for m in messages:
        i = int(m.t)
        if 0 <= i < n_seconds:
            counts[i] += 1
            w, hits = score_message(m.text)
            emotes[i] += w
            if hits:
                tokens[i].update(hits)
    return ChatSeries(counts, emotes, tokens)


# ============================================================================ scoring

def robust_z(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return x
    med = np.median(x)
    mad = np.median(np.abs(x - med)) * 1.4826
    scale = mad if mad > 1e-9 else x.std()
    if scale <= 1e-9:
        return np.zeros_like(x)
    return (x - med) / scale


def moving_sum(x: np.ndarray, width: int) -> np.ndarray:
    return np.convolve(x, np.ones(width), mode="same")


def moving_median(x: np.ndarray, width: int) -> np.ndarray:
    from scipy.ndimage import median_filter
    return median_filter(x, size=width, mode="nearest")


def shift_back(x: np.ndarray, seconds: int) -> np.ndarray:
    """Chat reacts after the moment: value at t+lag describes the moment at t."""
    if seconds <= 0:
        return x
    out = np.zeros_like(x)
    out[:-seconds] = x[seconds:]
    return out


@dataclass
class Candidate:
    id: str
    start: float
    end: float
    peak_time: float
    score: float
    audio_score: float
    chat_score: float
    chat_messages: int = 0
    top_reactions: list[str] = field(default_factory=list)
    peak_lufs: float = SILENCE_LUFS

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        return asdict(self)


def combined_score(loudness: np.ndarray, rms_db: np.ndarray, chat: ChatSeries | None,
                   chat_lag: int = 6) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (combined, audio_component, chat_component) per-second scores."""
    voiced = loudness > SILENCE_LUFS + 1
    loud_z = robust_z(np.where(voiced, loudness, np.median(loudness[voiced]) if voiced.any() else SILENCE_LUFS))
    baseline = moving_median(rms_db, 31)
    jump_z = robust_z(np.clip(rms_db - baseline, 0, None))
    audio = np.clip(0.7 * loud_z + 0.3 * jump_z, -3, 8)

    if chat is not None and chat.counts.sum() > 0:
        rate_z = robust_z(shift_back(moving_sum(chat.counts, 5), chat_lag))
        emote_z = robust_z(shift_back(moving_sum(chat.emote_weight, 5), chat_lag))
        chat_comp = np.clip(0.45 * rate_z + 0.55 * emote_z, -3, 10)
        combined = 0.5 * audio + 0.5 * chat_comp
    else:
        chat_comp = np.zeros_like(audio)
        combined = audio
    smooth = np.convolve(combined, np.ones(5) / 5.0, mode="same")
    return smooth, audio, chat_comp


def pick_candidates(score: np.ndarray, *, count: int, pre_roll: float, post_roll: float,
                    duration: float, audio: np.ndarray | None = None, chat_comp: np.ndarray | None = None,
                    chat: ChatSeries | None = None, loudness: np.ndarray | None = None,
                    chat_lag: int = 6) -> list[Candidate]:
    """Greedy non-maximum suppression: highest peaks first, windows never overlap."""
    min_gap = pre_roll + post_roll
    order = np.argsort(score)[::-1]
    picked: list[int] = []
    for idx in order:
        if len(picked) >= count:
            break
        if any(abs(int(idx) - p) < min_gap for p in picked):
            continue
        picked.append(int(idx))

    out: list[Candidate] = []
    for rank, peak in enumerate(sorted(picked)):
        start = max(0.0, peak - pre_roll)
        end = min(duration, peak + post_roll)
        if end - start < 5:
            continue
        msgs, reactions = 0, []
        if chat is not None:
            lo, hi = int(start), int(min(len(chat.counts), end + chat_lag))
            msgs = int(chat.counts[lo:hi].sum())
            merged: Counter = Counter()
            for c in chat.tokens[lo:hi]:
                merged.update(c)
            reactions = [tok for tok, _ in merged.most_common(5)]
        out.append(Candidate(
            id=f"c{rank:02d}",
            start=round(start, 2), end=round(end, 2), peak_time=float(peak),
            score=round(float(score[peak]), 3),
            audio_score=round(float(audio[peak]) if audio is not None else 0.0, 3),
            chat_score=round(float(chat_comp[peak]) if chat_comp is not None else 0.0, 3),
            chat_messages=msgs, top_reactions=reactions,
            peak_lufs=round(float(loudness[peak]), 1) if loudness is not None else SILENCE_LUFS,
        ))
    return out


@dataclass
class AnalysisResult:
    duration: float
    candidates: list[Candidate]
    loudness: np.ndarray
    score: np.ndarray
    chat_message_count: int

    def summary(self) -> dict:
        return {
            "duration": self.duration,
            "chat_messages": self.chat_message_count,
            "candidates": [c.to_dict() for c in self.candidates],
            "candidate_minutes": round(sum(c.duration for c in self.candidates) / 60, 1),
        }


def analyze_features(features: AudioFeatures, messages: list[ChatMessage], *, count: int = 25,
                     pre_roll: float = 35.0, post_roll: float = 25.0, chat_lag: float = 6.0) -> AnalysisResult:
    loudness, rms_db = features.per_second()
    n = loudness.size
    chat = chat_series(messages, n) if messages else None
    score, audio, chat_comp = combined_score(loudness, rms_db, chat, int(chat_lag))
    candidates = pick_candidates(score, count=count, pre_roll=pre_roll, post_roll=post_roll,
                                 duration=features.duration, audio=audio, chat_comp=chat_comp,
                                 chat=chat, loudness=loudness, chat_lag=int(chat_lag))
    return AnalysisResult(features.duration, candidates, loudness, score, len(messages))


def analyze(audio_path: Path | str, chat_path: Path | str | None = None, *, count: int = 25,
            pre_roll: float = 35.0, post_roll: float = 25.0, chat_lag: float = 6.0,
            progress: ProgressFn | None = None) -> AnalysisResult:
    features = compute_features(iter_audio_chunks(audio_path), progress=progress)
    messages = parse_chat_file(chat_path)
    log.info("Analyzed %.0f s of audio, %d chat messages", features.duration, len(messages))
    return analyze_features(features, messages, count=count, pre_roll=pre_roll,
                            post_roll=post_roll, chat_lag=chat_lag)
