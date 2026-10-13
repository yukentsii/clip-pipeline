"""Stage 5c: crop/stack + burn subtitles + encode (h264_amf -> libx264 fallback).

The filtergraph is assembled from a LayoutPlan. ffmpeg runs with list argv and
cwd set to the job directory, so the subtitles filter only ever sees a fixed,
relative file name (sidesteps Windows drive-letter escaping in filtergraphs).
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clip_pipeline.hardware import (encoder_args, encoder_filter_suffix, encoder_input_args, ffmpeg_binary,
                                    preferred_encoders)
from clip_pipeline.render.face_tracker import OUTPUT_SIZES, CropSegment, LayoutPlan, Region, crop_size

log = logging.getLogger(__name__)

SUBTITLE_FILENAME = "captions.ass"
# EBU R128 single-pass: -14 LUFS integrated is what YouTube/TikTok/Instagram normalise to; -1.5 dBTP
# leaves headroom for their AAC re-encode.
LOUDNORM_FILTER = "loudnorm=I=-14:TP=-1.5:LRA=11"


class RenderError(RuntimeError):
    pass


@dataclass
class RenderRequest:
    source: Path               # the surgical slice
    output: Path
    trim_start: float          # seconds, relative to the slice
    trim_end: float
    aspect: str = "9:16"
    plan: LayoutPlan | None = None
    subtitles: Path | None = None  # must live in `workdir` under SUBTITLE_FILENAME
    loudnorm: bool = True          # level the audio to the platforms' playback target

    @property
    def duration(self) -> float:
        return max(0.1, self.trim_end - self.trim_start)


def _even(v: float) -> int:
    """Even size (yuv420p needs even dimensions); never zero."""
    return max(2, int(round(v)) // 2 * 2)


def _even_pos(v: float) -> int:
    """Even offset; 0 is valid."""
    return max(0, int(round(v)) // 2 * 2)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def piecewise_expr(boundaries: list[float], values: list[int]) -> str:
    """values[i] holds until boundaries[i]; last value holds forever.
    Commas are escaped for the filtergraph parser."""
    expr = str(values[-1])
    for b, v in zip(reversed(boundaries), reversed(values[:-1])):
        expr = f"if(lt(t\\,{b:.3f})\\,{v}\\,{expr})"
    return expr


def track_crop_filter(segments: list[CropSegment], src_w: int, src_h: int, aspect: str,
                      offset: float, duration: float) -> str:
    cw, ch = crop_size(src_w, src_h, aspect)  # type: ignore[arg-type]
    segs = [s for s in segments if s.end - offset > 0 and s.start - offset < duration] or segments[:1]
    xs = [_even_pos(_clamp(s.cx * src_w - cw / 2, 0, src_w - cw)) for s in segs]
    ys = [_even_pos(_clamp(s.cy * src_h - ch / 2, 0, src_h - ch)) for s in segs]
    bounds = [s.end - offset for s in segs[:-1]]
    x_expr = piecewise_expr(bounds, xs) if len(set(xs)) > 1 else str(xs[0])
    y_expr = piecewise_expr(bounds, ys) if len(set(ys)) > 1 else str(ys[0])
    return f"crop=w={cw}:h={ch}:x={x_expr}:y={y_expr}"


def region_crop(region: Region, src_w: int, src_h: int) -> str:
    w = _even(region.w * src_w)
    h = _even(region.h * src_h)
    x = _even_pos(_clamp(region.x * src_w, 0, src_w - w))
    y = _even_pos(_clamp(region.y * src_h, 0, src_h - h))
    return f"crop=w={w}:h={h}:x={x}:y={y}"


def build_filtergraph(plan: LayoutPlan, aspect: str, *, offset: float, duration: float,
                      subtitles: bool) -> str:
    out_w, out_h = OUTPUT_SIZES[aspect]
    sub = f",subtitles={SUBTITLE_FILENAME}" if subtitles else ""
    scale = "flags=lanczos"
    src = "[0:v]"
    if plan.src_crop:  # baked-in black bars: cut down to the picture first, everything below works on that
        x, y, w, h = plan.src_crop
        src = f"[0:v]crop=w={w}:h={h}:x={x}:y={y}[pic];[pic]"

    if plan.mode in ("split", "facecam") and aspect == "9:16" and plan.top and plan.bottom:
        top_h = out_h // 2 if plan.mode == "split" else _even(out_h * 0.35)
        bot_h = out_h - top_h
        return (
            f"{src}split=2[a][b];"
            f"[a]{region_crop(plan.top, plan.src_w, plan.src_h)},scale={out_w}:{top_h}:{scale},setsar=1[top];"
            f"[b]{region_crop(plan.bottom, plan.src_w, plan.src_h)},scale={out_w}:{bot_h}:{scale},setsar=1[bot];"
            f"[top][bot]vstack=inputs=2{sub}[vout]"
        )

    if aspect == "16:9" and plan.src_w / max(plan.src_h, 1) >= 1.7:
        return (f"{src}scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:{scale},"
                f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1{sub}[vout]")

    segments = plan.segments or [CropSegment(0.0, plan.duration or duration + offset, 0.5)]
    visible = [s for s in segments if s.end - offset > 0 and s.start - offset < duration] or segments[:1]
    crop = track_crop_filter(segments, plan.src_w, plan.src_h, aspect, offset, duration)
    cropped = f"{crop},scale={out_w}:{out_h}:{scale},setsar=1"
    if not any(s.kind == "fit" for s in visible):
        return f"{src}{cropped}{sub}[vout]"

    fit = fit_filter("fg0", "bg0", out_w, out_h)
    crop_spans = [(max(0.0, s.start - offset), min(duration, s.end - offset)) for s in visible if s.kind != "fit"]
    if not crop_spans:
        return f"{src}split=2[fg0][bg0];{fit}{sub}[vout]"
    # Both layouts run for the whole clip; the cropped one is switched on over the fit one for its shots.
    enable = "+".join(f"gte(t\\,{a:.3f})*lt(t\\,{b:.3f})" for a, b in crop_spans)
    return (f"{src}split=3[fg0][bg0][c0];{fit}[fitted];[c0]{cropped}[cropped];"
            f"[fitted][cropped]overlay=0:0:enable={enable}{sub}[vout]")


def fit_filter(fg: str, bg: str, out_w: int, out_h: int) -> str:
    """Whole frame scaled to fit, centred over a blurred, slightly darkened cover-scaled copy of itself.
    The background is blurred at quarter resolution: same look, a fraction of the cost."""
    bw, bh = _even(out_w / 4), _even(out_h / 4)
    return (
        f"[{bg}]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=8:2,"
        f"eq=brightness=-0.08,scale={out_w}:{out_h}:flags=bilinear,setsar=1[bgblur];"
        f"[{fg}]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
        f"setsar=1[fgfit];"
        f"[bgblur][fgfit]overlay=(W-w)/2:(H-h)/2"
    )


def build_render_cmd(req: RenderRequest, encoder: str, ffmpeg: str = "ffmpeg") -> list[str]:
    if req.plan is None:
        raise ValueError("RenderRequest.plan is required")
    graph = build_filtergraph(req.plan, req.aspect, offset=req.trim_start, duration=req.duration,
                              subtitles=req.subtitles is not None)
    out_label = "[vout]"
    suffix = encoder_filter_suffix(encoder)
    if suffix:   # e.g. VAAPI: upload the finished frames to the GPU as the last step
        graph += f";[vout]{suffix}[venc]"
        out_label = "[venc]"
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", *encoder_input_args(encoder),
        "-ss", f"{req.trim_start:.3f}", "-i", str(req.source.resolve()),
        "-t", f"{req.duration:.3f}",
        "-filter_complex", graph,
        "-map", out_label, "-map", "0:a?",
        *encoder_args(encoder),
        *(["-af", LOUDNORM_FILTER] if req.loudnorm else []),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(req.output.resolve()),
    ]


def render(req: RenderRequest) -> tuple[Path, str]:
    """Render with the best encoder; on failure retry with the next. Returns (path, encoder)."""
    ffmpeg = ffmpeg_binary()
    workdir = req.subtitles.parent if req.subtitles else req.output.parent
    if req.subtitles and req.subtitles.name != SUBTITLE_FILENAME:
        raise ValueError(f"subtitle file must be named {SUBTITLE_FILENAME}")
    req.output.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    for encoder in preferred_encoders():
        cmd = build_render_cmd(req, encoder, ffmpeg)
        log.info("Rendering %s with %s", req.output.name, encoder)
        try:
            proc = subprocess.run(cmd, cwd=str(workdir), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=1800, check=False)
        except subprocess.TimeoutExpired:
            errors.append(f"{encoder}: timeout")
            continue
        if proc.returncode == 0 and req.output.exists() and req.output.stat().st_size > 0:
            return req.output, encoder
        errors.append(f"{encoder}: {proc.stderr.strip()[-600:]}")
        log.warning("Encoder %s failed, trying fallback", encoder)
    raise RenderError("All encoders failed:\n" + "\n".join(errors))


# ------------------------------------------------------------------ helpers (OpenCV, no ffmpeg CLI)

def probe_video(path: Path) -> tuple[int, int, float, float]:
    """(width, height, fps, duration)."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        return (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                float(fps), float(frames / fps if fps else 0))
    finally:
        cap.release()


def extract_thumbnail(video: Path, out: Path, at: float, max_width: int = 640) -> Path | None:
    import cv2

    cap = cv2.VideoCapture(str(video))
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, at) * 1000)
        ok, frame = cap.read()
        if not ok:
            return None
        h, w = frame.shape[:2]
        if w > max_width:
            frame = cv2.resize(frame, (max_width, int(h * max_width / w)), interpolation=cv2.INTER_AREA)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return out
    finally:
        cap.release()
