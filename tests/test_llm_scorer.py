import json
from types import SimpleNamespace

import pytest

from clip_pipeline.analysis import llm_scorer as ls
from clip_pipeline.analysis.audio_analyzer import Candidate
from clip_pipeline.analysis.transcriber import Transcript, Word


def cand(cid="c00", start=2500.0, end=2560.0, peak=2535.0, score=3.0):
    return Candidate(id=cid, start=start, end=end, peak_time=peak, score=score, audio_score=2.0,
                     chat_score=4.0, chat_messages=120, top_reactions=["kekw"], peak_lufs=-14.0)


def transcript(cid="c00", start=2500.0, end=2560.0):
    words = [Word(start + i * 0.5, start + i * 0.5 + 0.4, f"w{i}") for i in range(int((end - start) / 0.5))]
    return Transcript(cid, start, end, words)


@pytest.mark.parametrize("raw,expected", [
    ("00:42:15", 2535.0), ("42:15", 2535.0), ("1:02:03.5", 3723.5), ("2535.25", 2535.25), (12, 12.0),
])
def test_parse_timestamp(raw, expected):
    assert ls.parse_timestamp(raw) == expected


def test_parse_timestamp_rejects_garbage():
    with pytest.raises(ValueError):
        ls.parse_timestamp("tomorrow")


def test_format_timestamp():
    assert ls.format_timestamp(2535) == "00:42:15"


def test_clamp_window_enforces_bounds_and_lengths():
    assert ls.clamp_window(2400, 2700, 2500, 2560) == (2500, 2560)
    s, e = ls.clamp_window(2530, 2533, 2500, 2600)          # too short -> widened to 30 s
    assert e - s == pytest.approx(30) and 2500 <= s and e <= 2600
    s, e = ls.clamp_window(0, 500, 0, 1000)                  # too long -> capped
    assert e - s == ls.MAX_CLIP
    assert ls.clamp_window(2550, 2510, 2500, 2560) == (2510, 2550)  # swapped


def test_finalize_clip_validates_and_snaps_words():
    raw = ls.LLMClip(candidate_id="c00", title="  Insane clutch  ", hook_title='"He Had One Bullet Left" #fyp',
                     description="d", tags=["#Gaming", "gaming", "fps!"],
                     hook_score=40, engagement_score=20, value_score=18, shareability_score=-3,
                     content_type="Discussion", start_time="00:41:50.2",
                     end_time="00:42:30.7", hook_reason="h")
    clip = ls.finalize_clip(raw, cand(), transcript())
    assert clip.title == "Insane clutch"
    assert clip.tags == ["Gaming", "fps"]
    assert clip.hook_title == "He Had One Bullet Left"
    assert (clip.hook_score, clip.shareability_score) == (25, 0)      # each part clamped to 0-25
    assert clip.virality_score == 25 + 20 + 18 + 0                    # total is always the sum of the parts
    assert clip.content_type == "podcast"
    assert 2500 <= clip.start < clip.end <= 2560
    assert clip.start == pytest.approx(2510.0 - 0.1)   # 2510.2 was mid-word -> pulled back to word start


def test_heuristic_when_no_api_key():
    cands = [cand("c00", score=1.0), cand("c01", 3000, 3060, 3040, score=5.0)]
    trs = [transcript("c00"), transcript("c01", 3000, 3060)]
    clips = ls.score_candidates(cands, trs)
    assert [c.candidate_id for c in clips] == ["c01", "c00"]       # sorted by virality
    assert all(c.source == "heuristic" for c in clips)
    assert all(c.hook_score is None and c.hook_title == "" for c in clips)   # nothing to break down or headline
    for c in clips:
        assert ls.MIN_CLIP <= c.duration <= ls.MAX_CLIP


def test_prompt_contains_markers_and_transcript():
    p = ls.build_prompt([cand()], {"c00": transcript()}, "My Stream", "gaming")
    assert "candidate_id: c00" in p and "kekw" in p and "[00:41:40] w0" in p and "My Stream" in p


class FakeModels:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def gemini_reply(clips):
    batch = ls.LLMClipBatch(clips=[ls.LLMClip(**c) for c in clips])
    return SimpleNamespace(parsed=batch, text=batch.model_dump_json())


def test_gemini_path_with_retry_and_skipped_candidate():
    class RateLimited(Exception):
        code = 429

    reply = gemini_reply([dict(candidate_id="c00", title="Big moment", hook_title="Nobody expected this", description="desc",
                               tags=["a"], hook_score=24, engagement_score=22, value_score=21,
                               shareability_score=21, content_type="gaming", start_time="00:41:45",
                               end_time="00:42:25", hook_reason="reaction")])
    models = FakeModels([RateLimited("429 RESOURCE_EXHAUSTED"), reply])
    client = SimpleNamespace(models=models)
    clips = ls.score_candidates([cand("c00"), cand("c01", 3000, 3060, 3030)],
                                [transcript("c00"), transcript("c01", 3000, 3060)],
                                client=client, sleep=lambda s: None)
    by_id = {c.candidate_id: c for c in clips}
    assert by_id["c00"].source == "gemini" and by_id["c00"].virality_score == 88
    assert by_id["c01"].source == "heuristic"          # model skipped it
    assert len(models.calls) == 2
    config = models.calls[-1][2]
    assert config.response_mime_type == "application/json" and config.response_schema is ls.LLMClipBatch


def test_gemini_text_fallback_when_parsed_missing():
    payload = {"clips": [dict(candidate_id="c00", title="t", hook_title="", description="d", tags=[], hook_score=10,
                              engagement_score=10, value_score=15, shareability_score=15, content_type="gaming", start_time="00:41:45", end_time="00:42:25", hook_reason="")]}
    models = FakeModels([SimpleNamespace(parsed=None, text=json.dumps(payload))])
    out = ls.call_gemini("p", client=SimpleNamespace(models=models), sleep=lambda s: None)
    assert out.clips[0].candidate_id == "c00"


def test_gemini_non_retryable_error_falls_back_to_heuristic():
    class BadRequest(Exception):
        code = 400

    models = FakeModels([BadRequest("invalid argument")])
    clips = ls.score_candidates([cand()], [transcript()], client=SimpleNamespace(models=models), sleep=lambda s: None)
    assert clips[0].source == "heuristic" and len(models.calls) == 1


def _clip(**kw):
    base = dict(candidate_id="c00", title="t", content_type="film", start_time="1", end_time="40")
    return ls.ScoredClip(**{**base, **kw})


@pytest.mark.parametrize("raw,expected", [
    ("Movie", "film"), ("gameplay", "gaming"), ("interview", "podcast"), ("education", "education"),
    ("", "other"), ("cooking show", "other"),
])
def test_content_type_is_normalised_to_known_set(raw, expected):
    assert _clip(content_type=raw).content_type == expected


def test_partial_score_breakdown_is_dropped():
    c = _clip(virality_score=61, hook_score=20, engagement_score=None, value_score=10, shareability_score=10)
    assert c.virality_score == 61 and c.hook_score is None and c.value_score is None


def test_sanitize_hook_title_limits_length():
    assert ls.sanitize_hook_title("  “one two\nthree”  ") == "one two three"
    assert len(ls.sanitize_hook_title("word " * 30).split()) <= ls.HOOK_TITLE_MAX_WORDS
    assert len(ls.sanitize_hook_title("supercalifragilistic " * 9)) <= ls.HOOK_TITLE_MAX_CHARS


def test_prompt_carries_category_and_type_guess():
    p = ls.build_prompt([cand()], {"c00": transcript()}, "Toronto - Full Movie", "film", ["Film & Animation"])
    assert "Film & Animation" in p and "content type (correct it if wrong): film" in p


class RateLimited(Exception):
    code = 429


DAILY = ("429 RESOURCE_EXHAUSTED. quotaId: 'GenerateRequestsPerDayPerProjectPerModel-FreeTier' "
         "Please retry in 20h52m10.26s. 'retryDelay': '75130s'")
MINUTE = ("429 RESOURCE_EXHAUSTED. quotaId: 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier' "
          "Please retry in 33.5s. 'retryDelay': '33s'")


def many(n):
    cands = [cand(f"c{i:02d}", 1000 + 100 * i, 1060 + 100 * i, 1030 + 100 * i) for i in range(n)]
    return cands, [transcript(c.id, c.start, c.end) for c in cands]


def scored(cands, score=20):
    return gemini_reply([dict(candidate_id=c.id, title="t", hook_title="Wait for it", description="d", tags=[],
                              hook_score=score, engagement_score=score, value_score=score, shareability_score=score,
                              content_type="film", start_time=ls.format_timestamp(c.start + 5),
                              end_time=ls.format_timestamp(c.start + 45), hook_reason="") for c in cands])


def test_retry_delay_parsing_tells_daily_from_minute_limits():
    assert ls.retry_delay(Exception(DAILY)) == 75130
    assert ls.retry_delay(Exception("Please retry in 1h2m3s.")) == 3723
    assert ls.retry_delay(Exception("boom")) is None
    assert ls.QuotaExceeded("m", Exception(DAILY)).daily
    minute = ls.QuotaExceeded("m", Exception(MINUTE))
    assert not minute.daily and minute.delay == 33
    assert ls.QuotaExceeded("m", Exception("429 RESOURCE_EXHAUSTED")).daily   # no hint: assume the worst


def test_split_batches_is_even_and_minimal():
    assert [len(b) for b in ls.split_batches(list(range(25)), 13)] == [13, 12]
    assert [len(b) for b in ls.split_batches(list(range(14)), 13)] == [7, 7]
    assert [len(b) for b in ls.split_batches(list(range(4)), 13)] == [4]
    assert ls.split_batches([], 13) == []


def test_exhausted_model_falls_through_to_the_next_and_is_not_asked_again():
    cands, trs = many(ls.BATCH_SIZE + 2)                                  # two batches
    b1, b2 = ls.split_batches(cands)
    models = FakeModels([RateLimited(DAILY), scored(b1), scored(b2)])
    notes = []
    clips = ls.score_candidates(cands, trs, models=["big", "mid", "lite"], notes=notes,
                                client=SimpleNamespace(models=models), sleep=lambda s: pytest.fail("no waiting"))
    assert [c[0] for c in models.calls] == ["big", "mid", "mid"]          # batch 2 skips the dead model
    assert all(c.source == "gemini" and c.model == "mid" for c in clips) and len(clips) == len(cands)
    assert notes == [] and ls.models_used(clips) == ["mid"]


def test_every_model_out_of_daily_quota_scores_locally_and_says_so():
    cands, trs = many(ls.BATCH_SIZE + 2)
    models = FakeModels([RateLimited(DAILY)] * 3)
    notes = []
    clips = ls.score_candidates(cands, trs, stream_type="film", models=["big", "mid", "lite"], notes=notes,
                                client=SimpleNamespace(models=models), sleep=lambda s: pytest.fail("no waiting"))
    assert [c[0] for c in models.calls] == ["big", "mid", "lite"]         # one try each, none for batch 2
    assert len(clips) == len(cands) and all(c.source == "heuristic" and c.content_type == "film" for c in clips)
    assert len(notes) == 1 and "daily quota used up on all 3 Gemini models" in notes[0]


def test_unknown_model_is_skipped():
    class NotFound(Exception):
        code = 404

    cands, trs = many(2)
    models = FakeModels([NotFound("models/gone is not found"), scored(cands)])
    clips = ls.score_candidates(cands, trs, models=["gone", "lite"], client=SimpleNamespace(models=models),
                                sleep=lambda s: None)
    assert [c[0] for c in models.calls] == ["gone", "lite"] and all(c.model == "lite" for c in clips)


def test_waits_once_when_only_per_minute_limits_are_left():
    cands, trs = many(2)
    slept = []
    models = FakeModels([RateLimited(DAILY), RateLimited(MINUTE), scored(cands)])
    clips = ls.score_candidates(cands, trs, models=["big", "lite"], client=SimpleNamespace(models=models),
                                sleep=slept.append)
    assert [c[0] for c in models.calls] == ["big", "lite", "lite"]
    assert len(slept) == 1 and 30 <= slept[0] <= 36 and all(c.source == "gemini" for c in clips)
    # still limited after the wait: give up on the LLM instead of waiting again
    slept.clear()
    models = FakeModels([RateLimited(MINUTE), RateLimited(MINUTE)])
    clips = ls.score_candidates(cands, trs, models=["lite"], client=SimpleNamespace(models=models), sleep=slept.append)
    assert len(slept) == 1 and all(c.source == "heuristic" for c in clips)


def test_server_errors_retry_same_model_then_move_on():
    class Overloaded(Exception):
        code = 503

    cands, trs = many(2)
    models = FakeModels([Overloaded("503 UNAVAILABLE")] * ls.MAX_RETRIES + [scored(cands)])
    clips = ls.score_candidates(cands, trs, models=["big", "lite"], client=SimpleNamespace(models=models),
                                sleep=lambda s: None)
    assert [c[0] for c in models.calls] == ["big"] * ls.MAX_RETRIES + ["lite"]
    assert all(c.model == "lite" for c in clips)


def test_model_list_setting(monkeypatch):
    from clip_pipeline import config

    monkeypatch.setenv("GEMINI_MODELS", " a , b,a ,, c ")
    assert config._gemini_models() == ("a", "b", "c")
    monkeypatch.setenv("GEMINI_MODELS", "")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")                 # legacy single setting goes first
    chain = config._gemini_models()
    assert chain[0] == "gemini-2.5-flash" and len(chain) == len(config.DEFAULT_GEMINI_MODELS)
    monkeypatch.setenv("GEMINI_MODEL", "")
    assert config._gemini_models() == config.DEFAULT_GEMINI_MODELS

def test_dominant_content_type_ignores_heuristic_clips():
    clips = [_clip(content_type="film"), _clip(content_type="film"), _clip(content_type="vlog"),
             _clip(content_type="gaming", source="heuristic"), _clip(content_type="gaming", source="heuristic"),
             _clip(content_type="gaming", source="heuristic")]
    assert ls.dominant_content_type(clips) == "film"
    assert ls.dominant_content_type(clips[3:]) is None

# ---------------------------------------------------------------- an unanswered request must never block

def hung():
    import httpx
    return httpx.ReadTimeout("The read operation timed out")


def test_unanswered_request_is_retried_then_the_next_model_answers():
    cands, trs = many(2)
    models = FakeModels([hung(), hung(), scored(cands)])
    said = []
    clips = ls.score_candidates(cands, trs, models=["big", "lite"], client=SimpleNamespace(models=models),
                                sleep=lambda s: None, status=said.append)
    assert [c[0] for c in models.calls] == ["big", "big", "lite"]             # one retry, then move on
    assert all(c.source == "gemini" and c.model == "lite" for c in clips)
    assert "Scoring with big" in said and "Scoring with lite" in said
    assert any("did not answer" in s for s in said)                           # the wait is explained


def test_when_no_model_answers_clips_are_scored_offline_and_later_batches_do_not_wait_again():
    cands, trs = many(ls.BATCH_SIZE + 2)                                      # two batches
    models = FakeModels([hung()] * 4)
    notes = []
    clips = ls.score_candidates(cands, trs, stream_type="film", models=["big", "lite"], notes=notes,
                                client=SimpleNamespace(models=models), sleep=lambda s: None)
    assert [c[0] for c in models.calls] == ["big", "big", "lite", "lite"]     # batch 2 asks nobody
    assert len(clips) == len(cands) and all(c.source == "heuristic" for c in clips)
    assert len(notes) == 1 and "Gemini unavailable" in notes[0]


def test_scoring_gives_up_when_its_time_budget_is_spent():
    cands, trs = many(2)
    clock = [0.0]

    class Slow(FakeModels):
        def generate_content(self, model, contents, config):
            clock[0] += 120                                                   # every request eats its full timeout
            return super().generate_content(model, contents, config)

    models = Slow([hung()] * 6)
    pool = ls.ModelPool(["a", "b", "c", "d"], now=lambda: clock[0])
    with pytest.raises(ls.GeminiUnavailable, match="did not answer within 300 s"):
        ls.call_with_fallback("p", pool, client=SimpleNamespace(models=models), sleep=lambda s: None,
                              budget=300, now=lambda: clock[0])
    assert len(models.calls) == 3                                             # 360 s of waiting, not 16 minutes


def test_real_client_is_built_with_a_time_limit(monkeypatch):
    from google import genai

    seen = {}

    class FakeClient:
        def __init__(self, **kw):
            seen.update(kw)
            self.models = FakeModels([scored(many(1)[0])])

    monkeypatch.setattr(genai, "Client", FakeClient)
    ls.call_gemini("p", model="m", sleep=lambda s: None)
    assert seen["http_options"].timeout == 120_000                            # milliseconds; never "wait forever"
