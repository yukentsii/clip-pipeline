from dataclasses import replace

from fastapi.testclient import TestClient

from clip_pipeline import __main__ as cli
from clip_pipeline.api.app import create_app
from clip_pipeline.config import get_settings


def test_system_reports_schema_and_queue_sizes(tmp_path):
    s = replace(get_settings(), data_dir=tmp_path)
    app = create_app(s, background=False)
    store = app.state.store
    src = store.create_source(url="https://youtu.be/x")
    store.add_clips(src.id, [{"candidate_id": "c00", "title": "t", "start": 0.0, "end": 30.0, "status": "review"}])
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True}
        body = client.get("/system").json()
    assert body["schema"] >= 1
    assert body["sources_waiting"] == 1 and body["clips_in_review"] == 1 and body["review_min_score"] == 70
    assert (tmp_path / "clip_pipeline.db").exists()


def test_cli_add_and_status(tmp_path, monkeypatch, capsys):
    s = replace(get_settings(), data_dir=tmp_path)
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    video = tmp_path / "A.Movie.mp4"
    video.write_bytes(b"x")
    assert cli.main(["add", str(video), "--type", "film"]) == 0
    assert cli.main(["add", "https://youtu.be/abc"]) == 0
    assert cli.main(["add", "not a link"]) == 2
    assert cli.main(["add", str(video), "--type", "nonsense"]) == 2
    (tmp_path / "notes.txt").write_text("x")
    assert cli.main(["add", str(tmp_path / "notes.txt")]) == 2
    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "A Movie" in out and "https://youtu.be/abc" in out and out.count("queued") == 2
