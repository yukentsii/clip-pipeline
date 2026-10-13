"""Hardware capability detection: ffmpeg hardware encoders (NVIDIA, Intel, AMD), AVX-512, DirectML.

Everything here is probed lazily and cached, so importing the module is free.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

from clip_pipeline.config import get_settings

log = logging.getLogger(__name__)

AMF_ENCODER = "h264_amf"          # AMD on Windows
NVENC_ENCODER = "h264_nvenc"      # NVIDIA, Windows and Linux
QSV_ENCODER = "h264_qsv"          # Intel Quick Sync, Windows and Linux
VAAPI_ENCODER = "h264_vaapi"      # Intel / AMD on Linux
SOFTWARE_ENCODER = "libx264"
# Tried in this order; the first one that really encodes a test frame on this machine is used.
HARDWARE_ENCODERS = (NVENC_ENCODER, QSV_ENCODER, AMF_ENCODER, VAAPI_ENCODER)
ENCODER_NAMES = {"amf": AMF_ENCODER, "nvenc": NVENC_ENCODER, "qsv": QSV_ENCODER, "vaapi": VAAPI_ENCODER,
                 "x264": SOFTWARE_ENCODER, "cpu": SOFTWARE_ENCODER, **{e: e for e in HARDWARE_ENCODERS},
                 SOFTWARE_ENCODER: SOFTWARE_ENCODER}


class FFmpegNotFoundError(RuntimeError):
    pass


def resolve_binary(name: str, override: str = "") -> str | None:
    """Resolve a binary from an explicit override path or PATH."""
    if override:
        return override if os.path.isfile(override) else shutil.which(override)
    return shutil.which(name)


def ffmpeg_binary() -> str:
    path = resolve_binary("ffmpeg", get_settings().ffmpeg_path)
    if not path:
        raise FFmpegNotFoundError(
            "ffmpeg not found. Install it (Windows: `winget install Gyan.FFmpeg`; Debian/Ubuntu: `apt install ffmpeg`) "
            "or set FFMPEG_PATH in .env."
        )
    return path


def ffmpeg_available() -> bool:
    return resolve_binary("ffmpeg", get_settings().ffmpeg_path) is not None


def parse_encoders(encoders_output: str) -> set[str]:
    """Parse `ffmpeg -encoders` output into a set of encoder names."""
    names: set[str] = set()
    for line in encoders_output.splitlines():
        # Lines look like: " V....D h264_amf             AMD AMF H.264 Encoder"
        m = re.match(r"^\s*[VAS][\.A-Z]{5}\s+(\w[\w\-]*)\s", line)
        if m:
            names.add(m.group(1))
    return names


@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    if not ffmpeg_available():
        return frozenset()
    try:
        proc = subprocess.run(
            [ffmpeg_binary(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not list ffmpeg encoders: %s", exc)
        return frozenset()
    return frozenset(parse_encoders(proc.stdout))


def encoder_input_args(encoder: str) -> list[str]:
    """Arguments that must come before the input for this encoder (VAAPI needs its render device)."""
    if encoder == VAAPI_ENCODER:
        return ["-vaapi_device", get_settings().vaapi_device]
    return []


def encoder_filter_suffix(encoder: str) -> str:
    """Filters to append to the end of the video chain (VAAPI encodes frames uploaded to the GPU)."""
    return "format=nv12,hwupload" if encoder == VAAPI_ENCODER else ""


@lru_cache(maxsize=None)
def encoder_works(encoder: str) -> bool:
    """`-encoders` lists an encoder whenever ffmpeg was *built* with it; a 1-frame test encode proves
    the driver and hardware are actually usable on this machine."""
    if encoder == SOFTWARE_ENCODER:
        return SOFTWARE_ENCODER in available_encoders()
    if encoder not in available_encoders():
        return False
    suffix = encoder_filter_suffix(encoder)
    try:
        proc = subprocess.run(
            [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", *encoder_input_args(encoder),
             "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1", *(["-vf", suffix] if suffix else []),
             "-frames:v", "1", "-c:v", encoder, "-f", "null", "-"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        log.info("%s is listed but a test encode failed; not using it. %s", encoder, proc.stderr.strip()[:200])
    return proc.returncode == 0


def amf_works() -> bool:
    return encoder_works(AMF_ENCODER)


@lru_cache(maxsize=1)
def working_hardware_encoder() -> str | None:
    return next((e for e in HARDWARE_ENCODERS if encoder_works(e)), None)


def encoder_args(encoder: str, bitrate: str | None = None) -> list[str]:
    """ffmpeg video codec arguments for the given encoder name."""
    bitrate = bitrate or get_settings().video_bitrate
    peak = _scale_bitrate(bitrate, 1.5)
    if encoder == AMF_ENCODER:
        return ["-c:v", AMF_ENCODER, "-usage", "transcoding", "-quality", "quality", "-rc", "vbr_peak",
                "-b:v", bitrate, "-maxrate", peak, "-pix_fmt", "yuv420p"]
    if encoder == NVENC_ENCODER:
        return ["-c:v", NVENC_ENCODER, "-preset", "p5", "-rc", "vbr", "-b:v", bitrate, "-maxrate", peak,
                "-pix_fmt", "yuv420p"]
    if encoder == QSV_ENCODER:
        return ["-c:v", QSV_ENCODER, "-preset", "medium", "-b:v", bitrate, "-maxrate", peak, "-pix_fmt", "nv12"]
    if encoder == VAAPI_ENCODER:   # frames arrive as GPU surfaces (see encoder_filter_suffix): no pix_fmt here
        return ["-c:v", VAAPI_ENCODER, "-b:v", bitrate, "-maxrate", peak]
    return [
        "-c:v", SOFTWARE_ENCODER,
        "-preset", "veryfast",
        "-b:v", bitrate,
        "-maxrate", peak,
        "-bufsize", _scale_bitrate(bitrate, 2.0),
        "-pix_fmt", "yuv420p",
        "-threads", "0",
    ]


def _scale_bitrate(bitrate: str, factor: float) -> str:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", bitrate.strip())
    if not m:
        return bitrate
    value, unit = float(m.group(1)) * factor, m.group(2)
    return f"{value:g}{unit}"


def preferred_encoders() -> list[str]:
    """Encoders to try, best first, honouring VIDEO_ENCODER (auto | nvenc | qsv | amf | vaapi | x264).
    Software encoding is always the last resort, so a render never fails for lack of a GPU."""
    mode = get_settings().video_encoder
    chosen = ENCODER_NAMES.get(mode)
    if chosen == SOFTWARE_ENCODER:
        return [SOFTWARE_ENCODER]
    if chosen:                       # named explicitly: trust the owner, fall back if it fails at render time
        return [chosen, SOFTWARE_ENCODER]
    best = working_hardware_encoder()
    return [best, SOFTWARE_ENCODER] if best else [SOFTWARE_ENCODER]


# --------------------------------------------------------------------------- CPU / GPU

@lru_cache(maxsize=1)
def cpu_has_avx512() -> bool:
    try:
        from numpy._core._multiarray_umath import __cpu_features__ as feats  # numpy >= 2
    except ImportError:
        try:
            from numpy.core._multiarray_umath import __cpu_features__ as feats  # numpy 1.x
        except ImportError:
            return False
    return bool(feats.get("AVX512F"))


@lru_cache(maxsize=1)
def directml_available() -> bool:
    """True when onnxruntime-directml is installed (Radeon 890M via DirectML).

    Note: faster-whisper runs on CTranslate2, which has no DirectML backend, so
    this only matters for ONNX-based models; Whisper stays on CPU int8.
    """
    try:
        import onnxruntime as ort
    except ImportError:
        return False
    return "DmlExecutionProvider" in ort.get_available_providers()


@dataclass(frozen=True)
class HardwareReport:
    ffmpeg: str | None
    amf_listed: bool
    amf_usable: bool
    avx512: bool
    directml: bool
    logical_cpus: int

    hardware_encoder: str | None = None

    @property
    def video_encoder(self) -> str:
        return self.hardware_encoder or SOFTWARE_ENCODER


def hardware_report() -> HardwareReport:
    return HardwareReport(
        ffmpeg=resolve_binary("ffmpeg", get_settings().ffmpeg_path),
        amf_listed=AMF_ENCODER in available_encoders(),
        amf_usable=amf_works(),
        avx512=cpu_has_avx512(),
        directml=directml_available(),
        logical_cpus=os.cpu_count() or 1,
        hardware_encoder=preferred_encoders()[0] if preferred_encoders()[0] != SOFTWARE_ENCODER else None,
    )


if __name__ == "__main__":  # python -m clip_pipeline.hardware
    print(hardware_report())
