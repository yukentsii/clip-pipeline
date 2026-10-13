"""Isolate every test run from real data dirs, credentials and the scheduler."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="clip-tests-"))

# Must be set before any clip_pipeline module is imported (settings are cached).
os.environ.update({
    "DATA_DIR": str(_TMP / "data"),
    "SCHEDULER_ENABLED": "false",
    "AUTH_REQUIRED": "false",          # tests of signed requests switch it on themselves
    "BIND_HOST": "127.0.0.1",
    "PORT": "8000",
    "VIDEO_ENCODER": "auto",
})
# Empty (not unset) so a developer's real .env can't leak credentials or paths into tests:
# python-dotenv never overrides variables that already exist.
for key in ("GEMINI_API_KEY", "GEMINI_MODELS", "GEMINI_MODEL", "DB_PATH", "INBOX_DIR", "FACE_MODEL_PATH",
            "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
            "INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN", "TIKTOK_STORAGE_STATE", "TIKTOK_USER_DATA_DIR",
            "FFMPEG_PATH", "YTDLP_PATH", "REVIEW_MIN_SCORE", "PUBLIC_URL", "TIMEZONE"):
    os.environ[key] = ""
