import re

import pytest

from clip_pipeline.render import subtitle_gen as sg
from clip_pipeline.analysis.transcriber import Word


def words(texts, start=100.0, step=0.4):
    return [Word(start + i * step, start + i * step + 0.35, t) for i, t in enumerate(texts)]


def test_ass_time_format():
    assert sg.ass_time(0) == "0:00:00.00"
    assert sg.ass_time(3723.456) == "1:02:03.46"
    assert sg.ass_time(-1) == "0:00:00.00"


def test_escape_neutralises_override_tags():
    assert sg.escape_ass(r"{\pos(0,0)}hi\N") == "(/pos(0,0))hi/N"


@pytest.mark.parametrize("w,h", [(1080, 1920), (1080, 1080), (1920, 1080)])
def test_caption_box_inside_safe_zone(w, h):
    top, bottom = sg.caption_bounds(h, sg.font_size_for(w, h))
    assert sg.SAFE_TOP <= top < bottom <= sg.SAFE_BOTTOM


def test_relative_words_clip_and_shift():
    ws = [Word(95, 96, "before"), Word(99.8, 100.3, "edge"), Word(101, 101.4, "in"), Word(131, 132, "after")]
    rel = sg.relative_words(ws, 100.0, 130.0)
    assert [(w.text, w.start) for w in rel] == [("edge", 0.0), ("in", 1.0)]


def test_whisper_split_tokens_are_glued():
    ws = [Word(100.0, 100.3, "200"), Word(100.3, 100.5, "and"), Word(100.5, 100.7, "1"),
          Word(100.7, 101.0, ",768"), Word(101.0, 101.2, "it"), Word(101.2, 101.3, "'s"), Word(101.3, 101.5, "50"),
          Word(101.5, 101.6, "%")]
    rel = sg.relative_words(ws, 100.0, 110.0)
    assert [w.text for w in rel] == ["200", "and", "1,768", "it's", "50%"]
    assert rel[2].end == pytest.approx(1.0)


def test_chunking_respects_limits():
    ws = sg.relative_words(words(["one", "two", "three.", "four", "five", "six", "seven", "eight"]), 100, 200)
    phrases = sg.chunk_phrases(ws, max_chars=14, max_words=4)
    assert phrases[0].text == "one two three."      # sentence end breaks
    assert all(len(p.text) <= 14 for p in phrases)
    assert all(len(p.words) <= 4 for p in phrases)


def test_karaoke_emits_one_event_per_word_with_highlight():
    doc = sg.build_ass(words(["hello", "world"]), 100.0, 110.0, width=1080, height=1920, style="karaoke")
    events = [l for l in doc.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 2
    assert "PlayResX: 1080" in doc and "PlayResY: 1920" in doc
    assert "\\pos(540,1344)" in events[0] and "\\an5" in events[0]
    assert re.search(r"\\c&H0000E5FF&?\\fscx112\\fscy112\}HELLO", events[0])
    assert "{\\c" not in events[1].split("HELLO")[0].split("}", 1)[1]  # first word not highlighted in 2nd event
    assert events[0].startswith("Dialogue: 0,0:00:00.00,0:00:00.40")


def test_static_style_one_event_per_phrase():
    doc = sg.build_ass(words(["a", "b", "c"]), 100.0, 110.0, width=1080, height=1920, style="static")
    events = [l for l in doc.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 1 and events[0].endswith("A B C")


def test_hook_title_is_a_top_card_for_the_opening_seconds():
    doc = sg.build_ass(words(["hello"]), 100.0, 130.0, width=1080, height=1920,
                       hook_title=r"He {\b1}did not see this coming at all tonight")
    hook = [l for l in doc.splitlines() if l.startswith("Dialogue:") and ",Hook," in l]
    assert len(hook) == 1 and "Style: Hook," in doc
    assert hook[0].startswith("Dialogue: 1,0:00:00.00,0:00:04.00,Hook")
    assert "\\an8\\pos(540,230)" in hook[0]                 # 12 % down: clear of the platform top bar
    text = hook[0].split("}", 1)[1]
    assert "\\N" in text and "{" not in text                # wrapped by us; override tags neutralised
    assert all(len(line) <= sg.max_chars_for(1080, sg.hook_font_size(1080, 1920), usable=0.80)
               for line in text.split("\\N"))


def test_hook_title_without_captions_and_short_clip():
    doc = sg.build_ass(words(["hello", "world"]), 100.0, 102.5, width=1920, height=1080, style="none",
                       hook_title="Watch this")
    events = [l for l in doc.splitlines() if l.startswith("Dialogue:")]
    assert len(events) == 1 and ",Hook," in events[0]
    assert "0:00:02.50" in events[0] and "\\pos(960,76)" in events[0]
    assert not [l for l in sg.build_ass(words(["a"]), 100, 110, width=1080, height=1920).splitlines()
                if ",Hook,," in l]                          # no title -> no card


def test_wrap_hook_title_balances_lines():
    assert sg.wrap_hook_title("short one", 22) == ["short one"]
    lines = sg.wrap_hook_title("nobody expected the ending of this scene", 22)
    assert len(lines) == 2 and abs(len(lines[0]) - len(lines[1])) <= 8


def test_write_ass(tmp_path):
    p = sg.write_ass(tmp_path / "captions.ass", words(["x"]), 100, 105, width=1080, height=1080)
    assert p.read_text(encoding="utf-8").startswith("[Script Info]")
    assert "Style: Caption,Arial," in p.read_text(encoding="utf-8")
    q = sg.write_ass(tmp_path / "b.ass", words(["x"]), 100, 105, width=1080, height=1080, font="Liberation Sans",
                     hook_title="Hi")
    text = q.read_text(encoding="utf-8")
    assert "Style: Caption,Liberation Sans," in text and "Style: Hook,Liberation Sans," in text
