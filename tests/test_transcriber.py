from types import SimpleNamespace

import numpy as np
import pytest

from clip_pipeline.analysis import transcriber as tr
from clip_pipeline.analysis.transcriber import Transcript, Word


def words_every(start, end, step, dur):
    t, out = start, []
    while t + dur <= end:
        out.append(Word(t, t + dur, "word"))
        t += step
    return out


def test_dense_dialogue_is_podcast():
    t = Transcript("c00", 0, 60, words_every(0, 60, 0.4, 0.35))  # 150 WPM, ~88 % coverage
    assert t.wpm == pytest.approx(150, abs=1)
    assert t.speech_ratio > 0.8
    assert t.content_type == "podcast"


def test_sparse_shouting_is_gaming():
    t = Transcript("c01", 0, 60, words_every(10, 20, 0.5, 0.3) + words_every(40, 45, 0.5, 0.3))
    assert t.content_type == "gaming"


def test_speech_ratio_merges_overlaps():
    t = Transcript("c", 0, 10, [Word(0, 5, "a"), Word(2, 6, "b")])
    assert t.speech_ratio == pytest.approx(0.6)


def test_classify_stream_is_duration_weighted():
    pod = Transcript("a", 0, 120, words_every(0, 120, 0.4, 0.35))
    game = Transcript("b", 0, 30, [])
    assert tr.classify_stream([pod, game]) == "podcast"
    assert tr.classify_stream([Transcript("c", 0, 10, []), game]) == "other"   # quiet != gameplay


def test_source_metadata_beats_speech_density():
    quiet = [Transcript("a", 0, 60, [])]
    movie = {"extractor": "youtube", "categories": ["Film & Animation"]}
    assert tr.classify_stream(quiet, movie) == "film"
    assert tr.classify_stream(quiet, {"extractor": "twitch:vod"}) == "gaming"
    assert tr.classify_stream(quiet, {"categories": ["Gaming"]}) == "gaming"
    # catch-all categories say nothing, so the speech vote decides
    chatty = [Transcript("b", 0, 120, words_every(0, 120, 0.4, 0.35))]
    assert tr.classify_stream(chatty, {"categories": ["Entertainment"]}) == "podcast"
    assert tr.content_type_from_metadata({"categories": ["Entertainment"]}) is None


def test_words_between_and_roundtrip():
    t = Transcript("c02", 100, 160, [Word(101, 101.5, "hi"), Word(150, 150.4, "there")], "en")
    assert [w.text for w in t.words_between(140, 160)] == ["there"]
    back = Transcript.from_dict(t.to_dict())
    assert back.words == t.words and back.text == "hi there"


def test_transcribe_array_offsets_word_times_to_stream_time():
    class FakeModel:
        def transcribe(self, audio, **kw):
            assert kw["word_timestamps"] and kw["vad_filter"]
            seg = SimpleNamespace(words=[SimpleNamespace(start=1.0, end=1.4, word=" Hello", probability=0.9),
                                         SimpleNamespace(start=1.5, end=1.9, word=" ", probability=0.1)])
            return iter([seg]), SimpleNamespace(language="en")

    t = tr.transcribe_array(FakeModel(), np.zeros(16000 * 30, dtype=np.float32), offset=2535.0, candidate_id="c03")
    assert [(w.start, w.end, w.text) for w in t.words] == [(2536.0, 2536.4, "Hello")]
    assert t.start == 2535.0 and t.end == pytest.approx(2565.0) and t.language == "en"


# ---------------------------------------------------------------------------- which language is spoken

SR = tr.ANALYSIS_SR
CODE = {1: "tr", 2: "en", 3: "sv"}          # test audio is filled with a number that stands for a language


class LangModel:
    """Hears the language the audio is "filled" with; records what it was asked to transcribe."""

    def __init__(self):
        self.detected, self.transcribed = 0, []

    def detect_language(self, audio):
        self.detected += 1
        codes = np.rint(audio[audio > 0]).astype(int)
        top = CODE[int(np.bincount(codes).argmax())]
        return top, 0.9, [(top, 0.9)] + [(l, 0.05) for l in ("tr", "en", "sv") if l != top]

    def transcribe(self, audio, language=None, **kw):
        self.transcribed.append((language, round(audio.size / SR, 1)))
        seg = SimpleNamespace(words=[SimpleNamespace(start=0.5, end=0.9, word=f" {language}-word", probability=0.9)])
        return iter([seg]), SimpleNamespace(language=language)


def setup_windows(monkeypatch, windows):
    """`windows`: one list per candidate of (start, end, code) stretches of speech inside a 60 s window."""
    from clip_pipeline.analysis.audio_analyzer import Candidate

    audios = {}
    cands = []
    for i, stretches in enumerate(windows):
        audio = np.zeros(60 * SR, dtype=np.float32)
        for a, b, code in stretches:
            audio[int(a * SR): int(b * SR)] = code
        cand = Candidate(id=f"c{i:02d}", start=1000.0 * i, end=1000.0 * i + 60, peak_time=1000.0 * i + 30, score=1.0,
                         audio_score=1.0, chat_score=0.0)
        audios[cand.start] = (audio, [(a, b) for a, b, _ in stretches])
        cands.append(cand)
    monkeypatch.setattr(tr, "load_audio_segment", lambda path, start, end: audios[start][0])
    monkeypatch.setattr(tr, "speech_stretches", lambda audio: next(s for a, s in audios.values() if a is audio))
    return cands


def test_a_song_in_another_language_cannot_flip_the_captions(monkeypatch):
    talk = [(0, 40, 1)]                                              # 40 s of Turkish
    cands = setup_windows(monkeypatch, [talk, talk, [(5, 15, 3)], talk])   # window 3: a 10 s Swedish song
    model = LangModel()
    out = tr.transcribe_candidates("x", cands, model=model)
    assert [lang for lang, _ in model.transcribed] == ["tr"] * 4     # Swedish is 8% of the speech: not used
    assert [t.language for t in out] == ["tr"] * 4 and model.detected == 4   # one listen per window, no more
    assert out[2].start == 2000.0 and out[2].end == 2060.0


def test_two_real_languages_are_followed_stretch_by_stretch(monkeypatch):
    mixed = [(0, 10, 1), (11, 13, 3), (14, 24, 2), (25, 27, 1), (30, 42, 2), (44, 54, 3)]
    cands = setup_windows(monkeypatch, [[(0, 40, 1)], [(0, 40, 2)], mixed])
    model = LangModel()
    out = tr.transcribe_candidates("x", cands, model=model)
    # windows 1 and 2 are one stretch each; in the mixed one: tr 0-13 (the 2 s Swedish-sounding bit is too
    # short to judge and follows what came before), en 14-42 (same for the 2 s Turkish bit), and the long
    # Swedish stretch is not on the shortlist, so it takes the likelier of tr/en, never "sv".
    langs = [lang for lang, _ in model.transcribed]
    assert langs[:2] == ["tr", "en"] and "sv" not in langs
    assert model.transcribed[2:] == [("tr", 13.0), ("en", 40.0)]     # the Swedish stretch joined the English run
    words = out[2].words
    assert [w.text for w in words][:2] == ["tr-word", "en-word"] and words == sorted(words, key=lambda w: w.start)
    assert words[1].start == pytest.approx(2000.0 + 14 + 0.5)        # times stay on the video's clock
    assert out[2].language == "en"                                   # the window's main language


def test_forced_language_skips_all_guessing(monkeypatch):
    from dataclasses import replace

    cands = setup_windows(monkeypatch, [[(0, 40, 2)], [(0, 40, 3)]])
    forced = replace(tr.get_settings(), whisper_language="tr")
    monkeypatch.setattr(tr, "get_settings", lambda: forced)
    model = LangModel()
    tr.transcribe_candidates("x", cands, model=model)
    assert model.detected == 0 and [lang for lang, _ in model.transcribed] == ["tr", "tr"]


def test_shortlist_and_runs():
    assert tr.language_shortlist({"tr": 80, "en": 15, "sv": 5}, 0.15) == ["tr", "en"]
    assert tr.language_shortlist({"sv": 3}, 0.15) == ["sv"] and tr.language_shortlist({}, 0.15) == []
    assert tr.pick_language({"sv": 0.9, "en": 0.06, "tr": 0.04}, ["tr", "en"]) == "en"
    runs = tr.plan_language_runs([(0, 2), (3, 9), (10, 12), (13, 20)], [None, "tr", None, "en"], "tr")
    assert runs == [(0, 12, "tr"), (13, 20, "en")]                   # leading gap looks ahead, later ones behind
    assert tr.plan_language_runs([(0, 2)], [None], "tr") == [(0, 2, "tr")]
