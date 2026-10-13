import json

import numpy as np
import pytest

from clip_pipeline.analysis import audio_analyzer as aa

SR = aa.ANALYSIS_SR


def sine(freq, seconds, amp=1.0, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def chunks(x, sizes=(1000, 4321, 16000, 777)):
    i, k = 0, 0
    while i < x.size:
        n = sizes[k % len(sizes)]
        yield x[i:i + n], x.size / SR
        i += n
        k += 1


@pytest.mark.parametrize("fs", [48_000, 16_000])
def test_k_weighting_calibration_997hz_full_scale(fs):
    """BS.1770: a 0 dBFS 997 Hz sine in one channel reads -3.01 LUFS."""
    from scipy import signal as sps

    x = sine(997, 5, sr=fs).astype(np.float64)
    y = sps.sosfilt(aa.k_weighting_sos(fs), x)
    lufs = float(aa.ms_to_lufs(np.mean(y[fs:] ** 2)))  # skip filter warm-up
    assert lufs == pytest.approx(-3.01, abs=0.15)


def test_k_weighting_48k_matches_spec_coefficients():
    sos = aa.k_weighting_sos(48_000)
    np.testing.assert_allclose(sos[0, :3], [1.53512485958697, -2.69169618940638, 1.19839281085285], rtol=1e-6)
    np.testing.assert_allclose(sos[1, 3:], [1.0, -1.99004745483398, 0.99007225036621], rtol=1e-6)


def test_streaming_features_match_regardless_of_chunking():
    x = np.concatenate([sine(440, 3, 0.1), sine(440, 2, 0.8)])
    a = aa.compute_features(chunks(x))
    b = aa.compute_features(iter([(x, x.size / SR)]))
    assert a.duration == pytest.approx(5.0)
    np.testing.assert_allclose(a.block_ms, b.block_ms, rtol=1e-9)
    np.testing.assert_allclose(a.block_ms_k, b.block_ms_k, rtol=1e-6, atol=1e-12)


def test_per_second_loudness_tracks_level_change():
    x = np.concatenate([sine(1000, 10, 0.05), sine(1000, 10, 0.5)])
    st, rms = aa.compute_features(chunks(x)).per_second()
    assert st.size == 20
    assert st[15] - st[4] == pytest.approx(20.0, abs=0.5)  # 10x amplitude = +20 dB
    assert rms[15] > rms[4]


def synthetic_stream(seconds=300, bursts=(100, 220), seed=0):
    rng = np.random.default_rng(seed)
    x = (rng.standard_normal(seconds * SR) * 0.02).astype(np.float32)
    for b in bursts:
        s = b * SR
        x[s:s + 4 * SR] += sine(600, 4, 0.7)
    return x


def test_detects_loud_bursts_as_candidates():
    x = synthetic_stream()
    feats = aa.compute_features(chunks(x, sizes=(16000 * 7,)))
    res = aa.analyze_features(feats, [], count=2, pre_roll=20, post_roll=15)
    peaks = sorted(c.peak_time for c in res.candidates)
    assert len(peaks) == 2
    assert abs(peaks[0] - 102) <= 4 and abs(peaks[1] - 222) <= 4
    for c in res.candidates:
        assert c.start <= c.peak_time <= c.end


def test_chat_burst_drives_candidate_with_lag_compensation():
    x = synthetic_stream(bursts=())
    feats = aa.compute_features(chunks(x, sizes=(16000 * 7,)))
    msgs = [aa.ChatMessage(t=float(t), text="hello") for t in range(0, 300, 7)]
    msgs += [aa.ChatMessage(160 + i * 0.05, "OMEGALUL KEKW clip it") for i in range(80)]
    res = aa.analyze_features(feats, msgs, count=1, pre_roll=20, post_roll=15, chat_lag=6)
    top = res.candidates[0]
    assert abs(top.peak_time - 155) <= 4      # chat at ~160 s -> moment at ~154 s
    assert "clip it" in top.top_reactions and top.chat_messages >= 80


def test_candidates_never_overlap():
    rng = np.random.default_rng(1)
    score = rng.random(3600)
    cands = aa.pick_candidates(score, count=25, pre_roll=35, post_roll=25, duration=3600)
    spans = sorted((c.start, c.end) for c in cands)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))
    assert len({c.id for c in cands}) == len(cands)


def test_score_message_weights_and_spam_dedup():
    w, hits = aa.score_message("OMEGALUL OMEGALUL OMEGALUL clip it")
    assert w == pytest.approx(3.0 + 4.0)
    assert "omegalul" in hits and "clip it" in hits
    assert aa.score_message("W W W")[0] == pytest.approx(1.5)
    assert aa.score_message("?????")[0] == pytest.approx(1.5)
    assert aa.score_message("what a normal message")[0] == 0


def test_parse_youtube_live_chat_jsonl(tmp_path):
    line = {"replayChatItemAction": {"videoOffsetTimeMsec": "12500", "actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"message": {"runs": [
            {"text": "no way "}, {"emoji": {"emojiId": "x", "shortcuts": [":face-with-tears-of-joy:"]}}]}}}}}]}}
    p = tmp_path / "audio.live_chat.json"
    p.write_text(json.dumps(line) + "\n" + "garbage line\n", encoding="utf-8")
    msgs = aa.parse_chat_file(p)
    assert len(msgs) == 1 and msgs[0].t == 12.5
    w, hits = aa.score_message(msgs[0].text)
    assert "no way" in hits and ":face-with-tears-of-joy:" in hits


def test_parse_twitch_rechat(tmp_path):
    p = tmp_path / "audio.rechat.json"
    p.write_text(json.dumps({"comments": [
        {"content_offset_seconds": 61.2, "message": {"body": "KEKW"}},
        {"content_offset_seconds": 3.0, "message": {"body": "hi"}},
    ]}))
    msgs = aa.parse_chat_file(p)
    assert [m.t for m in msgs] == [3.0, 61.2]


def test_missing_chat_is_empty():
    assert aa.parse_chat_file(None) == []


def test_robust_z_constant_series_is_zero():
    assert not aa.robust_z(np.ones(10)).any()


def test_pyav_decode_full_and_segment(tmp_path):
    sf = pytest.importorskip("soundfile")
    sr = 48_000
    x = np.zeros(sr * 20, dtype=np.float32)
    x[12 * sr:13 * sr] = sine(440, 1, 0.9, sr=sr)
    path = tmp_path / "a.wav"
    sf.write(path, x, sr)

    feats = aa.compute_features(aa.iter_audio_chunks(path))
    assert feats.duration == pytest.approx(20.0, abs=0.05)

    seg = aa.load_audio_segment(path, 10.0, 15.0)
    assert seg.size == pytest.approx(5 * SR, abs=SR * 0.01)
    loud = np.flatnonzero(np.abs(seg) > 0.5)
    assert loud.size and abs(loud[0] / SR - 2.0) < 0.05   # burst at 12 s -> 2 s into segment
