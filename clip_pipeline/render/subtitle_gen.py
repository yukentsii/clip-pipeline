"""Stage 5b: word-level .ass subtitles from Whisper timestamps.

Captions are one line, centred horizontally, and vertically centred at 70 % of
the frame height so the whole text block stays inside the 65-75 % social safe
band (clear of TikTok/Reels/Shorts UI at the top, right rail and bottom).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Protocol

SubtitleStyle = Literal["karaoke", "static", "none"]   # "none" = hook title only, no captions

SAFE_TOP = 0.65
SAFE_BOTTOM = 0.75
SAFE_CENTER = 0.70
LINE_HEIGHT = 1.2       # rendered line box ≈ font size × 1.2
CHAR_WIDTH = 0.6        # average glyph advance for bold sans ≈ font size × 0.6

# Hook title: a headline card at the top of the frame for the opening seconds.
HOOK_SECONDS = 4.0
HOOK_TOP_VERTICAL = 0.12   # below the TikTok/Reels/Shorts top bar on 9:16
HOOK_TOP_OTHER = 0.07
HOOK_MAX_LINES = 3


class WordLike(Protocol):
    start: float
    end: float
    text: str


@dataclass
class CaptionWord:
    start: float
    end: float
    text: str


@dataclass
class Phrase:
    words: list[CaptionWord]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class CaptionTheme:
    font: str = "Arial"
    primary: str = "&H00FFFFFF"     # white   (ASS colours are &HAABBGGRR)
    highlight: str = "&H0000E5FF"   # amber-yellow active word
    outline: str = "&H00000000"
    back: str = "&H80000000"
    outline_px: float = 6.0
    shadow_px: float = 2.0
    uppercase: bool = True
    max_words: int = 4


# ------------------------------------------------------------------ helpers

def ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape_ass(text: str) -> str:
    """Neutralise override blocks and escape sequences in user/LLM/ASR text."""
    return (text.replace("\\", "/").replace("{", "(").replace("}", ")")
            .replace("\r", " ").replace("\n", " ").strip())


def font_size_for(width: int, height: int) -> int:
    return int(round(min(width, height) * 0.075))


def max_chars_for(width: int, font_size: int, usable: float = 0.86) -> int:
    return max(8, int((width * usable) / (font_size * CHAR_WIDTH)))


def caption_center_y(height: int) -> int:
    return int(round(height * SAFE_CENTER))


def caption_bounds(height: int, font_size: int) -> tuple[float, float]:
    """(top, bottom) of the caption line box as fractions of frame height."""
    cy = caption_center_y(height)
    half = font_size * LINE_HEIGHT / 2
    return (cy - half) / height, (cy + half) / height


_ATTACH_LEFT = tuple(",.!?;:%)]}'’…") + ("n't", "'s", "'re", "'ll", "'ve", "'m", "'d")


def relative_words(words: Iterable[WordLike], clip_start: float, clip_end: float) -> list[CaptionWord]:
    out: list[CaptionWord] = []
    for w in words:
        if w.end <= clip_start or w.start >= clip_end:
            continue
        text = escape_ass(w.text)
        if not text:
            continue
        s = max(0.0, w.start - clip_start)
        e = min(clip_end - clip_start, w.end - clip_start)
        # Whisper emits some tokens as separate "words" (",768", "%", "'s"): glue them to the previous word
        # so captions read "1,768" / "50%" / "it's" instead of "1 ,768".
        if out and text.lower().startswith(_ATTACH_LEFT):
            prev = out[-1]
            out[-1] = CaptionWord(prev.start, round(max(e, prev.end), 3), prev.text + text)
            continue
        out.append(CaptionWord(round(s, 3), round(max(e, s + 0.05), 3), text))
    return out


def chunk_phrases(words: list[CaptionWord], *, max_chars: int, max_words: int = 4,
                  max_gap: float = 0.6) -> list[Phrase]:
    phrases: list[Phrase] = []
    cur: list[CaptionWord] = []
    for w in words:
        if cur:
            length = len(" ".join(x.text for x in cur)) + 1 + len(w.text)
            gap = w.start - cur[-1].end
            ends_sentence = cur[-1].text[-1:] in ".?!"
            if length > max_chars or len(cur) >= max_words or gap > max_gap or ends_sentence:
                phrases.append(Phrase(cur))
                cur = []
        cur.append(w)
    if cur:
        phrases.append(Phrase(cur))
    return phrases


def hook_font_size(width: int, height: int) -> int:
    return int(round(min(width, height) * 0.06))


def wrap_hook_title(text: str, max_chars: int, max_lines: int = HOOK_MAX_LINES) -> list[str]:
    """Split a headline into the fewest lines that fit, keeping them about the same length."""
    words = text.split()
    if not words:
        return []
    for n in range(1, max_lines + 1):
        target = len(text) / n
        lines, cur = [], ""
        for w in words:
            trial = f"{cur} {w}".strip()
            # break once the line is past its share, unless this is the last allowed line
            if cur and len(lines) < n - 1 and len(trial) > target and len(cur) >= target * 0.6:
                lines.append(cur)
                cur = w
            else:
                cur = trial
        lines.append(cur)
        if all(len(line) <= max_chars for line in lines) or n == max_lines:
            return lines
    return [text]


def hook_event(title: str, clip_len: float, width: int, height: int) -> str | None:
    text = escape_ass(title)
    if not text:
        return None
    fs = hook_font_size(width, height)
    lines = wrap_hook_title(text, max_chars_for(width, fs, usable=0.80))
    top = int(round(height * (HOOK_TOP_VERTICAL if height > width else HOOK_TOP_OTHER)))
    body = f"{{\\an8\\pos({width // 2},{top})\\fad(120,250)}}" + "\\N".join(lines)
    return _dialogue(0.0, min(HOOK_SECONDS, clip_len), body, style="Hook", layer=1)


# ------------------------------------------------------------------ document

def build_ass(words: Iterable[WordLike], clip_start: float, clip_end: float, *, width: int, height: int,
              style: SubtitleStyle = "karaoke", theme: CaptionTheme | None = None,
              hook_title: str | None = None) -> str:
    theme = theme or CaptionTheme()
    fs = font_size_for(width, height)
    rel = relative_words(words, clip_start, clip_end) if style != "none" else []
    if theme.uppercase:
        rel = [CaptionWord(w.start, w.end, w.text.upper()) for w in rel]
    phrases = chunk_phrases(rel, max_chars=max_chars_for(width, fs), max_words=theme.max_words)
    pos = f"{{\\an5\\pos({width // 2},{caption_center_y(height)})}}"
    clip_len = clip_end - clip_start

    events: list[str] = []
    hook = hook_event(hook_title, clip_len, width, height) if hook_title else None
    if hook:
        events.append(hook)
    for i, ph in enumerate(phrases):
        nxt = phrases[i + 1].start if i + 1 < len(phrases) else clip_len
        # hold the phrase on screen across short pauses so captions don't flicker
        ph_end = min(nxt, ph.end + 0.3) if nxt - ph.end < 0.6 else ph.end + 0.15
        ph_end = min(ph_end, clip_len)
        if style == "static":
            events.append(_dialogue(ph.start, ph_end, pos + ph.text))
            continue
        for j, w in enumerate(ph.words):
            w_end = ph.words[j + 1].start if j + 1 < len(ph.words) else ph_end
            parts = []
            for k, other in enumerate(ph.words):
                if k == j:
                    parts.append(f"{{\\c{theme.highlight}\\fscx112\\fscy112}}{other.text}{{\\r}}")
                else:
                    parts.append(other.text)
            events.append(_dialogue(w.start, max(w_end, w.start + 0.05), pos + " ".join(parts)))

    header = "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Caption,{theme.font},{fs},{theme.primary},{theme.highlight},{theme.outline},{theme.back},"
        f"-1,0,0,0,100,100,0,0,1,{theme.outline_px:g},{theme.shadow_px:g},5,40,40,0,1",
        # BorderStyle 3 = opaque box in OutlineColour; Outline is the box padding. Dark text on a white card.
        f"Style: Hook,{theme.font},{hook_font_size(width, height)},&H00141414,&H00141414,&H00FFFFFF,&H00000000,"
        f"-1,0,0,0,100,100,0,0,3,{hook_font_size(width, height) * 0.28:.0f},0,8,40,40,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])
    return header + "\n" + "\n".join(events) + "\n"


def _dialogue(start: float, end: float, text: str, *, style: str = "Caption", layer: int = 0) -> str:
    return f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}"


def write_ass(path: Path, words: Iterable[WordLike], clip_start: float, clip_end: float, *,
              width: int, height: int, style: SubtitleStyle = "karaoke", hook_title: str | None = None,
              font: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    theme = CaptionTheme(font=font) if font else None
    path.write_text(build_ass(words, clip_start, clip_end, width=width, height=height, style=style,
                              theme=theme, hook_title=hook_title), encoding="utf-8")
    return path
