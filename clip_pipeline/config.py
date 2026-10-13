"""All settings for the service, loaded from the environment / `.env` in the project root."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is optional at runtime
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").lower() in ("1", "true", "yes", "on")


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


# Tried in this order; each free-tier model has its own daily quota, so when one runs out the next
# takes over. Strongest first, the high-quota Lite models last.
DEFAULT_GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash",
                         "gemini-3.5-flash-lite", "gemini-2.5-flash-lite")


def _gemini_models() -> tuple[str, ...]:
    """GEMINI_MODELS (comma separated, in order) wins; a legacy single GEMINI_MODEL is tried first otherwise."""
    listed = [m.strip() for m in _env("GEMINI_MODELS").split(",") if m.strip()]
    if not listed:
        legacy = _env("GEMINI_MODEL")
        listed = ([legacy] if legacy else []) + list(DEFAULT_GEMINI_MODELS)
    return tuple(dict.fromkeys(listed))


@dataclass(frozen=True)
class Settings:
    # ---------------------------------------------------------------- storage
    data_dir: Path = field(default_factory=lambda: _resolve(_env("DATA_DIR", "./data")))
    db_file: str = field(default_factory=lambda: _env("DB_PATH"))          # blank = DATA_DIR/clip_pipeline.db
    inbox_path: str = field(default_factory=lambda: _env("INBOX_DIR"))     # blank = DATA_DIR/inbox
    max_upload_gb: float = field(default_factory=lambda: _env_float("MAX_UPLOAD_GB", 30.0))
    # A file in the inbox is taken once it has not changed for this long (its copy has finished).
    inbox_settle_seconds: float = field(default_factory=lambda: _env_float("INBOX_SETTLE_SECONDS", 10.0))
    poll_seconds: float = field(default_factory=lambda: _env_float("POLL_SECONDS", 5.0))
    # Run below normal CPU priority so other things on the machine stay responsive.
    low_priority: bool = field(default_factory=lambda: _env_bool("LOW_PRIORITY", True))

    # ---------------------------------------------------------------- network and access
    # Where the service listens. 127.0.0.1 = this machine only; use the machine's private address (or
    # 0.0.0.0) to let a paired phone on the same private network reach it.
    bind_host: str = field(default_factory=lambda: _env("BIND_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PORT", 8000))
    # The address phones should use, written into the pairing QR code. Blank = http://BIND_HOST:PORT
    public_url_setting: str = field(default_factory=lambda: _env("PUBLIC_URL"))
    # Every request must be signed by a paired device. Turn off only for local development.
    auth_required: bool = field(default_factory=lambda: _env_bool("AUTH_REQUIRED", True))

    # ---------------------------------------------------------------- tools
    ffmpeg_path: str = field(default_factory=lambda: _env("FFMPEG_PATH"))
    ytdlp_path: str = field(default_factory=lambda: _env("YTDLP_PATH"))
    video_encoder: str = field(default_factory=lambda: _env("VIDEO_ENCODER", "auto").lower())
    video_bitrate: str = field(default_factory=lambda: _env("VIDEO_BITRATE", "8M"))
    vaapi_device: str = field(default_factory=lambda: _env("VAAPI_DEVICE", "/dev/dri/renderD128"))
    # Font for burned-in captions and hook titles. Linux has no Arial; the Docker image installs Liberation
    # Sans, which fontconfig substitutes for it, so the default works there too.
    caption_font: str = field(default_factory=lambda: _env("CAPTION_FONT", "Arial"))

    # ---------------------------------------------------------------- finding clips
    whisper_model: str = field(default_factory=lambda: _env("WHISPER_MODEL", "small"))
    whisper_device: str = field(default_factory=lambda: _env("WHISPER_DEVICE", "cpu"))
    whisper_compute_type: str = field(default_factory=lambda: _env("WHISPER_COMPUTE_TYPE", "int8"))
    whisper_cpu_threads: int = field(default_factory=lambda: _env_int("WHISPER_CPU_THREADS", 12))
    whisper_language: str | None = field(default_factory=lambda: _env("WHISPER_LANGUAGE") or None)
    # A language must carry at least this share of a video's speech to be used for its captions. Keeps a
    # song or a stray line in another language from flipping the captions of a whole clip.
    language_min_share: float = field(default_factory=lambda: _env_float("LANGUAGE_MIN_SHARE", 0.15))

    candidate_count: int = field(default_factory=lambda: _env_int("CANDIDATE_COUNT", 25))
    candidate_pre_roll: float = field(default_factory=lambda: _env_float("CANDIDATE_PRE_ROLL", 35.0))
    candidate_post_roll: float = field(default_factory=lambda: _env_float("CANDIDATE_POST_ROLL", 25.0))
    chat_lag_seconds: float = field(default_factory=lambda: _env_float("CHAT_LAG_SECONDS", 6.0))

    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_models: tuple[str, ...] = field(default_factory=_gemini_models)
    # A scoring request that has not answered after this long is abandoned and tried again / elsewhere.
    gemini_timeout_seconds: float = field(default_factory=lambda: _env_float("GEMINI_TIMEOUT_SECONDS", 120.0))
    # The most time one batch of candidates may spend waiting on Gemini before it is scored offline.
    gemini_batch_budget_seconds: float = field(
        default_factory=lambda: _env_float("GEMINI_BATCH_BUDGET_SECONDS", 480.0))

    # Clips scoring at least this are cut and offered for review; the rest are only recorded.
    review_min_score: int = field(default_factory=lambda: _env_int("REVIEW_MIN_SCORE", 70))
    slice_padding: float = field(default_factory=lambda: _env_float("SLICE_PADDING", 10.0))

    face_model_file: str = field(default_factory=lambda: _env("FACE_MODEL_PATH"))  # blank = DATA_DIR/models/…

    # ---------------------------------------------------------------- publishing
    interval_seconds: int = field(default_factory=lambda: _env_int("SCHEDULER_INTERVAL_SECONDS", 60))
    max_attempts: int = field(default_factory=lambda: _env_int("UPLOAD_MAX_ATTEMPTS", 3))
    retry_backoff_seconds: int = field(default_factory=lambda: _env_int("UPLOAD_RETRY_BACKOFF_SECONDS", 300))
    scheduler_enabled: bool = field(default_factory=lambda: _env_bool("SCHEDULER_ENABLED", True))
    # Posting slots are times of day in this zone (IANA name, e.g. Europe/Istanbul). Blank = the machine's own.
    timezone: str = field(default_factory=lambda: _env("TIMEZONE"))
    # How often to look for posted clips whose view counts are due for another reading. 0 = never.
    stats_interval_minutes: int = field(default_factory=lambda: _env_int("STATS_INTERVAL_MINUTES", 30))

    youtube_client_id: str = field(default_factory=lambda: _env("YOUTUBE_CLIENT_ID"))
    youtube_client_secret: str = field(default_factory=lambda: _env("YOUTUBE_CLIENT_SECRET"))
    youtube_refresh_token: str = field(default_factory=lambda: _env("YOUTUBE_REFRESH_TOKEN"))
    youtube_privacy_status: str = field(default_factory=lambda: _env("YOUTUBE_PRIVACY_STATUS", "public"))
    youtube_category_id: str = field(default_factory=lambda: _env("YOUTUBE_CATEGORY_ID", "20"))

    instagram_user_id: str = field(default_factory=lambda: _env("INSTAGRAM_USER_ID"))
    instagram_access_token: str = field(default_factory=lambda: _env("INSTAGRAM_ACCESS_TOKEN"))
    instagram_graph_version: str = field(default_factory=lambda: _env("INSTAGRAM_GRAPH_VERSION", "v26.0"))

    tiktok_state_file: str = field(default_factory=lambda: _env("TIKTOK_STORAGE_STATE"))    # blank = DATA_DIR/secrets/…
    tiktok_profile_dir: str = field(default_factory=lambda: _env("TIKTOK_USER_DATA_DIR"))   # blank = DATA_DIR/secrets/…
    tiktok_headless: bool = field(default_factory=lambda: _env_bool("TIKTOK_HEADLESS", True))

    # ---------------------------------------------------------------- derived
    @property
    def public_url(self) -> str:
        return (self.public_url_setting or f"http://{self.bind_host}:{self.port}").rstrip("/")

    @property
    def db_path(self) -> Path:
        return _resolve(self.db_file) if self.db_file else self.data_dir / "clip_pipeline.db"

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def inbox_dir(self) -> Path:
        return _resolve(self.inbox_path) if self.inbox_path else self.data_dir / "inbox"

    @property
    def face_model_path(self) -> Path:
        if self.face_model_file:
            return _resolve(self.face_model_file)
        return self.data_dir / "models" / "blaze_face_short_range.tflite"

    @property
    def tiktok_storage_state(self) -> Path:
        return _resolve(self.tiktok_state_file) if self.tiktok_state_file else self.data_dir / "secrets" / "tiktok_state.json"

    @property
    def tiktok_user_data_dir(self) -> Path:
        return _resolve(self.tiktok_profile_dir) if self.tiktok_profile_dir else self.data_dir / "secrets" / "tiktok_profile"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.sources_dir.mkdir(parents=True, exist_ok=True)
    return settings
