import sqlite3

import pytest

from clip_pipeline import db as dbmod
from clip_pipeline.db import Database
from clip_pipeline.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(Database(tmp_path / "t.db"), tmp_path / "sources")


def clip_row(cid="c00", score=80, **kw):
    return {"candidate_id": cid, "title": f"clip {cid}", "start": 10.0, "end": 50.0, "virality_score": score,
            "tags": ["a", "b"], **kw}


def test_migrations_are_applied_once_and_versioned(tmp_path):
    path = tmp_path / "m.db"
    db = Database(path)
    assert db.version() == len(dbmod.MIGRATIONS)
    with db.connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"sources", "clips", "posts", "post_targets"} <= tables
    Database(path)                                             # opening again must not re-run anything
    assert Database(path).version() == len(dbmod.MIGRATIONS)


def test_database_from_newer_code_is_refused(tmp_path):
    path = tmp_path / "m.db"
    Database(path)
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {len(dbmod.MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer than this code"):
        Database(path)


def test_failed_migration_leaves_no_half_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(dbmod, "MIGRATIONS", dbmod.MIGRATIONS + ["CREATE TABLE extra (id TEXT); CREATE TABLE sources (x);"])
    path = tmp_path / "m.db"
    with pytest.raises(sqlite3.OperationalError):
        Database(path)
    monkeypatch.undo()
    db = Database(path)                                        # still at the last good version
    with db.connect() as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'extra'").fetchone() is None
    assert db.version() == len(dbmod.MIGRATIONS)


def test_source_is_a_url_or_a_file(store, tmp_path):
    with pytest.raises(ValueError):
        store.create_source()
    with pytest.raises(ValueError):
        store.create_source(url="https://x.test/v", file_path=tmp_path / "v.mp4")
    a = store.create_source(url="https://x.test/v")
    b = store.create_source(file_path=tmp_path / "v.mp4", title="v", content_hint="film")
    assert (a.kind, b.kind, b.content_hint) == ("url", "file", "film")
    assert store.get_source(b.id).file_path == str(tmp_path / "v.mp4")
    assert [s.id for s in store.list_sources()] == [b.id, a.id]          # newest first
    with pytest.raises(KeyError):
        store.get_source("missing")


def test_update_rejects_unknown_columns(store):
    src = store.create_source(url="https://x.test/v")
    store.update_source(src.id, status="running", stage="whisper", stage_progress=0.5)
    got = store.get_source(src.id)
    assert (got.status, got.stage, got.stage_progress) == ("running", "whisper", 0.5)
    with pytest.raises(KeyError):
        store.update_source(src.id, id="other")
    with pytest.raises(KeyError):
        store.update_source(src.id, **{"status = 'x'; --": 1})


def test_queue_order_is_oldest_first_and_skips_finished(store):
    a = store.create_source(url="https://x.test/a")
    b = store.create_source(url="https://x.test/b")
    assert store.next_queued_source().id == a.id
    store.update_source(a.id, status="failed")
    assert store.next_queued_source().id == b.id
    store.update_source(b.id, status="ready")
    assert store.next_queued_source() is None


def test_clips_roundtrip_sorted_by_score(store):
    src = store.create_source(url="https://x.test/v")
    store.add_clips(src.id, [clip_row("c00", 71), clip_row("c01", 93, hook_score=24, hook_title="Wait"),
                             clip_row("c02", 40, status="below_threshold")])
    clips = store.list_clips(source_id=src.id)
    assert [c.candidate_id for c in clips] == ["c01", "c00", "c02"]
    assert clips[0].tags == ["a", "b"] and clips[0].hook_score == 24 and clips[1].hook_score is None
    assert [c.candidate_id for c in store.list_clips(status="candidate")] == ["c01", "c00"]
    store.update_clip(clips[0].id, tags=["x"], end=55.5, status="review")
    got = store.get_clip(clips[0].id)
    assert (got.tags, got.end, got.status) == (["x"], 55.5, "review")


def test_add_clips_is_all_or_nothing(store):
    src = store.create_source(url="https://x.test/v")
    with pytest.raises(sqlite3.IntegrityError):
        store.add_clips(src.id, [clip_row("c00"), clip_row("c00")])       # duplicate candidate
    assert store.list_clips(source_id=src.id) == []


def test_claim_clip_only_from_expected_state(store):
    src = store.create_source(url="https://x.test/v")
    clip = store.add_clips(src.id, [clip_row(status="review", error="old problem")])[0]
    assert store.claim_clip(clip.id, ("review",), "rendering")
    assert not store.claim_clip(clip.id, ("review",), "rendering")        # second approve loses the race
    got = store.get_clip(clip.id)
    assert got.status == "rendering" and got.error is None


def test_restart_puts_half_done_work_back(store):
    running = store.create_source(url="https://x.test/a")
    done = store.create_source(url="https://x.test/b")
    store.update_source(running.id, status="running", stage="whisper")
    store.update_source(done.id, status="ready")
    a, b, c = store.add_clips(done.id, [clip_row("c00", status="slicing"), clip_row("c01", status="rendering"),
                                        clip_row("c02", status="scheduled")])
    assert store.recover_interrupted() == {"sources": 1, "clips": 2}
    resumed = store.get_source(running.id)
    assert resumed.status == "queued" and resumed.stage == "whisper"      # carries on from where it was
    assert [store.get_clip(x.id).status for x in (a, b, c)] == ["candidate", "sliced", "scheduled"]
    assert store.recover_interrupted() == {"sources": 0, "clips": 0}


def test_prefs_roundtrip_and_overwrite(store):
    assert store.get_prefs() == {}
    store.set_prefs({"review_min_score": 80, "default_layout": "fit"})
    store.set_prefs({"review_min_score": 65})
    assert store.get_prefs() == {"review_min_score": 65, "default_layout": "fit"}


def test_deleting_a_source_removes_its_clips(store):
    src = store.create_source(url="https://x.test/v")
    store.add_clips(src.id, [clip_row()])
    with store.db.connect() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (src.id,))
    assert store.list_clips() == []


def test_source_dir_rejects_path_like_ids(store):
    with pytest.raises(KeyError):
        store.source_dir("../etc")
