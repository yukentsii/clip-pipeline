"""Stage 5a: face detection + layout planning for vertical / square reframing.

Produces a `LayoutPlan` that video_renderer turns into an ffmpeg filtergraph:
  * track   - single subject; piecewise-constant crop that cuts between "shots"
              (deadzone + hold time, like a human editor) instead of jittery panning
  * split   - podcast with two stable speakers left/right -> stacked 9:16
  * facecam - small, static face near an edge (streamer cam over gameplay)
              -> facecam on top, gameplay below
  * fit     - no faces at all: the whole frame over a blurred copy of itself, so nothing is cropped off
  * center  - no face information (detector unavailable) or nothing to crop

A track plan can also mix both: shots with a face are cropped, shots without one (screens, wide
establishing shots, gameplay) become `kind="fit"` segments.
"""
from __future__ import annotations

import logging
import statistics
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from clip_pipeline.config import get_settings

log = logging.getLogger(__name__)

Aspect = Literal["9:16", "1:1", "16:9"]
LayoutMode = Literal["track", "split", "facecam", "fit", "center"]
Framing = Literal["auto", "crop", "fit"]  # the user's choice in the review UI

FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/latest/blaze_face_short_range.tflite"
)

OUTPUT_SIZES: dict[str, tuple[int, int]] = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}

DEADZONE = 0.08        # normalised x movement tolerated before re-framing
HOLD_SECONDS = 1.0     # subject must stay moved this long before we cut
MIN_SHOT_SECONDS = 1.5

# Blurred-fit shots: a stretch counts as faceless when under FACE_PRESENCE_RATE of the samples in a
# FACE_RATE_WINDOW around it show a face, so one missed detection doesn't flip the layout.
FACE_RATE_WINDOW = 2.0
FACE_PRESENCE_RATE = 0.3
FACE_MIN_WIDTH = 0.04      # ignore specks (posters, crowd) when deciding whether a shot has a subject
MIN_FIT_SECONDS = 2.5      # shorter faceless stretches keep the neighbouring crop
FIT_SNAP_WINDOW = 0.6      # move a layout switch onto a scene cut this close to it
FIT_MIN_LOSS = 0.2         # only bother when cropping would throw away this much of the frame


@dataclass
class FaceBox:
    x: float  # normalised top-left
    y: float
    w: float
    h: float
    score: float = 1.0

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def area(self) -> float:
        return self.w * self.h


@dataclass
class Sample:
    t: float
    faces: list[FaceBox]


@dataclass
class CropSegment:
    start: float
    end: float
    cx: float  # normalised crop centre
    cy: float = 0.5
    kind: str = "crop"  # "crop" | "fit" (full frame over blurred background)


@dataclass
class Region:
    """A normalised source rectangle (used for split/facecam panels)."""
    x: float
    y: float
    w: float
    h: float


@dataclass
class LayoutPlan:
    mode: LayoutMode
    src_w: int
    src_h: int
    duration: float
    segments: list[CropSegment] = field(default_factory=list)
    top: Region | None = None
    bottom: Region | None = None
    # (x, y, w, h) px of the picture inside baked-in black bars; src_w/src_h then describe that picture.
    src_crop: tuple[int, int, int, int] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================ detection

def ensure_face_model(path: Path | None = None) -> Path | None:
    path = path or get_settings().face_model_path
    if path.exists():
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        log.info("Downloading MediaPipe face model to %s", path)
        urllib.request.urlretrieve(FACE_MODEL_URL, tmp)  # fixed https URL, not user input
        tmp.replace(path)
        return path
    except OSError as exc:
        log.warning("Face model unavailable (%s); falling back to centre crop", exc)
        return None


class FaceDetector:
    """MediaPipe Tasks BlazeFace. Short-range model is tuned for close faces, so when
    the full frame yields nothing we also try each half (small faces in wide shots)."""

    def __init__(self, model_path: Path, min_confidence: float = 0.5):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self._mp_image_cls = mp.Image
        self._mp_format = mp.ImageFormat.SRGB
        opts = vision.FaceDetectorOptions(base_options=BaseOptions(model_asset_path=str(model_path)),
                                          min_detection_confidence=min_confidence)
        self._det = vision.FaceDetector.create_from_options(opts)

    def _run(self, rgb) -> list[FaceBox]:
        h, w = rgb.shape[:2]
        img = self._mp_image_cls(image_format=self._mp_format, data=rgb)
        res = self._det.detect(img)
        out = []
        for d in res.detections:
            bb = d.bounding_box
            score = d.categories[0].score if d.categories else 1.0
            out.append(FaceBox(bb.origin_x / w, bb.origin_y / h, bb.width / w, bb.height / h, float(score)))
        return out

    def detect(self, rgb) -> list[FaceBox]:
        faces = self._run(rgb)
        if faces:
            return faces
        import numpy as np

        w = rgb.shape[1]
        half = w // 2
        found: list[FaceBox] = []
        for x0 in (0, half):
            sub = np.ascontiguousarray(rgb[:, x0:x0 + half])
            for f in self._run(sub):
                found.append(FaceBox((x0 + f.x * half) / w, f.y, f.w * half / w, f.h, f.score))
        return found

    def close(self):
        self._det.close()


SCENE_CUT_THRESHOLD = 28.0  # mean |Δ| of a 32x18 grey thumbnail (0-255); motion stays well below

# Baked-in letterbox / pillarbox bars. A row (or column) is bar if its mean brightness never rises
# above BAR_MAX_LUMA in any sampled frame of the clip.
BAR_MAX_LUMA = 12.0
BAR_MIN_FRAC = 0.02     # thinner than this isn't worth a crop
BAR_MAX_FRAC = 0.30     # per side; more than this is a dark scene, not a bar
BAR_SYMMETRY = 0.02     # bars come in matching pairs; a lopsided "bar" is dark picture content
BAR_EDGE_PAD = 2        # px shaved off the picture so the bar's soft encoded edge goes too

Box = tuple[int, int, int, int]  # x, y, w, h in source pixels


def _bar_pair(profile, n: int) -> tuple[int, int]:
    """(leading, trailing) bar thickness along one axis of a brightest-seen profile."""
    lead = 0
    while lead < n and profile[lead] < BAR_MAX_LUMA:
        lead += 1
    trail = 0
    while trail < n - lead and profile[n - 1 - trail] < BAR_MAX_LUMA:
        trail += 1
    if abs(lead - trail) > BAR_SYMMETRY * n:
        lead = trail = min(lead, trail)
    if max(lead, trail) < BAR_MIN_FRAC * n or max(lead, trail) > BAR_MAX_FRAC * n:
        return 0, 0
    return lead, trail


def content_box(row_max, col_max, width: int, height: int) -> Box | None:
    """The picture inside any black bars, or None when there are no bars to remove."""
    if row_max is None or col_max is None:
        return None
    top, bottom = _bar_pair(row_max, height)
    left, right = _bar_pair(col_max, width)
    if not (top or bottom or left or right):
        return None
    x = (left + BAR_EDGE_PAD + 1) // 2 * 2 if left else 0
    y = (top + BAR_EDGE_PAD + 1) // 2 * 2 if top else 0
    w = (width - x - (right + BAR_EDGE_PAD if right else 0)) // 2 * 2
    h = (height - y - (bottom + BAR_EDGE_PAD if bottom else 0)) // 2 * 2
    return (x, y, w, h) if w >= 16 and h >= 16 else None


def sample_faces(video_path: Path | str, sample_fps: float = 4.0, *, detect_faces: bool = True
                 ) -> tuple[list[Sample], int, int, float, list[float], Box | None]:
    """Return (face samples, width, height, duration, scene-cut times, content box) for the clip.

    Faces are detected at `sample_fps`; scene cuts are measured on *every* frame (cheap thumbnail
    diff) so re-framing can switch exactly on the editor's cut instead of up to 1/sample_fps late.
    Black bars are found in the same pass from the grey frame the cut detector already made, so
    they cost two row/column averages per sampled frame and no extra decode."""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = frames / fps if frames > 0 else 0.0
    model = ensure_face_model() if detect_faces else None
    detector = FaceDetector(model) if model is not None else None
    step = max(1, int(round(fps / sample_fps)))
    samples: list[Sample] = []
    cuts: list[float] = []
    prev = None
    row_max = col_max = None
    idx = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (32, 18), interpolation=cv2.INTER_AREA)
            small = small.astype(np.int16)
            if prev is not None and float(np.abs(small - prev).mean()) > SCENE_CUT_THRESHOLD:
                cuts.append(round(idx / fps, 3))
            prev = small
            if idx % step == 0:
                rows = cv2.reduce(gray, 1, cv2.REDUCE_AVG, dtype=cv2.CV_32F).ravel()
                cols = cv2.reduce(gray, 0, cv2.REDUCE_AVG, dtype=cv2.CV_32F).ravel()
                row_max = rows if row_max is None else np.maximum(row_max, rows)
                col_max = cols if col_max is None else np.maximum(col_max, cols)
                if detector is not None:
                    samples.append(Sample(idx / fps, detector.detect(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))))
            idx += 1
    finally:
        if detector is not None:
            detector.close()
        cap.release()
    height, width = (gray.shape[:2] if idx else (height, width))
    return samples, width, height, max(duration, idx / fps), cuts, content_box(row_max, col_max, width, height)


# ============================================================================ planning

def crop_size(src_w: int, src_h: int, aspect: Aspect) -> tuple[int, int]:
    """Largest even-sized crop of the given aspect that fits the source."""
    num, den = (int(p) for p in aspect.split(":"))
    ratio = num / den
    cw, ch = src_h * ratio, float(src_h)
    if cw > src_w:
        cw, ch = float(src_w), src_w / ratio
    return int(cw) // 2 * 2, int(ch) // 2 * 2


def primary_face(faces: list[FaceBox]) -> FaceBox | None:
    return max(faces, key=lambda f: f.area * f.score, default=None)


def segment_track(samples: list[Sample], duration: float, *, deadzone: float = DEADZONE,
                  hold: float = HOLD_SECONDS, min_shot: float = MIN_SHOT_SECONDS,
                  cuts: list[float] | None = None) -> list[CropSegment]:
    """Turn noisy per-sample face centres into a few stable shots."""
    points = [(s.t, f.cx, f.cy) for s in samples if (f := primary_face(s.faces)) is not None]
    if not points:
        return [CropSegment(0.0, duration, 0.5, 0.5)]

    cuts = sorted(cuts or [])
    segments: list[CropSegment] = []
    cur_start, cur_x, cur_y = 0.0, points[0][1], points[0][2]
    pending: list[tuple[float, float, float]] = []
    last_ok = 0.0  # time of the last sample still matching the current framing
    for t, x, y in points:
        if abs(x - cur_x) <= deadzone:
            pending.clear()
            last_ok = t
            continue
        pending.append((t, x, y))
        if pending[-1][0] - pending[0][0] >= hold:
            cut = pending[0][0]
            # The subject actually moved somewhere in (last_ok, cut]; if the editor cut there, switch on that frame.
            in_gap = [c for c in cuts if last_ok < c <= cut + 1e-6]
            if in_gap:
                cut = in_gap[-1]
            segments.append(CropSegment(cur_start, cut, cur_x, cur_y))
            cur_start = cut
            cur_x = statistics.median(p[1] for p in pending)
            cur_y = statistics.median(p[2] for p in pending)
            last_ok = pending[-1][0]
            pending.clear()
    segments.append(CropSegment(cur_start, duration, cur_x, cur_y))

    # merge shots that are too short into their predecessor
    merged: list[CropSegment] = []
    for seg in segments:
        if merged and seg.end - seg.start < min_shot:
            merged[-1].end = seg.end
        else:
            merged.append(seg)
    if len(merged) > 1 and merged[0].end - merged[0].start < min_shot:
        merged[1].start = 0.0
        merged.pop(0)
    return merged


def faceless_spans(samples: list[Sample], duration: float, cuts: list[float] | None = None) -> list[tuple[float, float]]:
    """Stretches of at least MIN_FIT_SECONDS with (almost) no face, as (start, end) in clip time."""
    if not samples:
        return []
    times = [s.t for s in samples]
    present = [any(f.w >= FACE_MIN_WIDTH for f in s.faces) for s in samples]
    half = FACE_RATE_WINDOW / 2
    has_face = []
    for t in times:
        near = [p for u, p in zip(times, present) if abs(u - t) <= half]
        has_face.append(sum(near) / len(near) >= FACE_PRESENCE_RATE)

    # runs of constant state: [start, end, has_face]
    runs: list[list] = [[0.0, duration, has_face[0]]]
    for t, state in zip(times[1:], has_face[1:]):
        if state != runs[-1][2]:
            runs[-1][1] = t
            runs.append([t, duration, state])
    # absorb runs too short to be worth a layout switch into what came before (or after, for the first)
    merged: list[list] = []
    for run in runs:
        if merged and (run[1] - run[0] < MIN_FIT_SECONDS or merged[-1][2] == run[2]):
            merged[-1][1] = run[1]
        else:
            merged.append(run)
    if len(merged) > 1 and merged[0][1] - merged[0][0] < MIN_FIT_SECONDS:
        merged[1][0] = 0.0
        merged.pop(0)
        if len(merged) > 1 and merged[0][2] == merged[1][2]:
            merged[1][0] = 0.0
            merged.pop(0)
    # switch layouts on the editor's cut when there is one nearby
    for prev, nxt in zip(merged, merged[1:]):
        near = [c for c in (cuts or []) if abs(c - prev[1]) <= FIT_SNAP_WINDOW and prev[0] < c < nxt[1]]
        if near:
            prev[1] = nxt[0] = min(near, key=lambda c: abs(c - prev[1]))
    return [(r[0], r[1]) for r in merged if not r[2]]


def apply_fit_spans(segments: list[CropSegment], spans: list[tuple[float, float]], duration: float) -> list[CropSegment]:
    """Re-cut the crop track so every faceless span becomes a `fit` segment."""
    if not spans:
        return segments
    edges = sorted({0.0, duration, *(s.start for s in segments), *(s.end for s in segments),
                    *(t for span in spans for t in span)})
    edges = [e for e in edges if 0.0 <= e <= duration]
    out: list[CropSegment] = []
    for a, b in zip(edges, edges[1:]):
        if b - a < 1e-6:
            continue
        mid = (a + b) / 2
        if any(s <= mid < e for s, e in spans):
            piece = CropSegment(a, b, 0.5, 0.5, "fit")
        else:
            src = next((s for s in segments if s.start <= mid < s.end), segments[-1])
            piece = CropSegment(a, b, src.cx, src.cy)
        if out and (out[-1].kind, out[-1].cx, out[-1].cy) == (piece.kind, piece.cx, piece.cy):
            out[-1].end = b
        else:
            out.append(piece)
    return out


def crop_loss(src_w: int, src_h: int, aspect: Aspect) -> float:
    """Fraction of the source frame a straight crop to `aspect` throws away."""
    cw, ch = crop_size(src_w, src_h, aspect)
    return 1.0 - (cw * ch) / max(src_w * src_h, 1)


def detect_facecam(samples: list[Sample]) -> FaceBox | None:
    """A streamer facecam = small face, near an edge, barely moving, present most of the time."""
    faces = [primary_face(s.faces) for s in samples]
    faces = [f for f in faces if f is not None]
    if not samples or len(faces) < 0.5 * len(samples):
        return None
    small = [f for f in faces if f.w < 0.18]
    if len(small) < 0.7 * len(faces):
        return None
    cx = [f.cx for f in small]
    cy = [f.cy for f in small]
    if statistics.pstdev(cx) > 0.03 or statistics.pstdev(cy) > 0.03:
        return None
    mx, my = statistics.median(cx), statistics.median(cy)
    near_edge = mx < 0.3 or mx > 0.7 or my < 0.3 or my > 0.7
    if not near_edge:
        return None
    return FaceBox(mx - statistics.median(f.w for f in small) / 2, my - statistics.median(f.h for f in small) / 2,
                   statistics.median(f.w for f in small), statistics.median(f.h for f in small))


def detect_two_speakers(samples: list[Sample]) -> tuple[float, float] | None:
    """Two stable faces on opposite halves for most of the clip -> (left cx, right cx)."""
    if not samples:
        return None
    left, right, both = [], [], 0
    for s in samples:
        big = [f for f in s.faces if f.w >= 0.05]
        l = [f.cx for f in big if f.cx < 0.5]
        r = [f.cx for f in big if f.cx >= 0.5]
        left += l[:1]
        right += r[:1]
        both += bool(l and r)
    if both < 0.4 * len(samples) or not left or not right:
        return None
    lx, rx = statistics.median(left), statistics.median(right)
    return (lx, rx) if rx - lx > 0.25 else None


def panel_region(cx: float, cy: float, src_w: int, src_h: int, panel_aspect: float,
                 width_frac: float | None = None) -> Region:
    """Normalised region of `panel_aspect` (w/h) centred on (cx, cy), clamped to frame."""
    if width_frac is None:
        h_px = float(src_h)
        w_px = h_px * panel_aspect
        if w_px > src_w / 2:  # never wider than half the frame for split panels
            w_px = src_w / 2
            h_px = w_px / panel_aspect
    else:
        w_px = width_frac * src_w
        h_px = w_px / panel_aspect
        if h_px > src_h:
            h_px = float(src_h)
            w_px = h_px * panel_aspect
    x = min(max(cx * src_w - w_px / 2, 0), src_w - w_px)
    y = min(max(cy * src_h - h_px / 2, 0), src_h - h_px)
    return Region(x / src_w, y / src_h, w_px / src_w, h_px / src_h)


def plan_layout(samples: list[Sample], src_w: int, src_h: int, duration: float, *,
                aspect: Aspect = "9:16", content_type: str = "gaming",
                cuts: list[float] | None = None, layout: Framing = "auto") -> LayoutPlan:
    """`layout`: "auto" crops shots with a face and fits the rest; "crop" never uses the blurred fit;
    "fit" shows the whole frame over a blurred copy for the entire clip."""
    if aspect == "16:9" and src_w / max(src_h, 1) >= 1.7:
        return LayoutPlan("center", src_w, src_h, duration, [CropSegment(0, duration, 0.5)])
    if layout == "fit":
        return LayoutPlan("fit", src_w, src_h, duration, [CropSegment(0.0, duration, 0.5, 0.5, "fit")])

    if aspect == "9:16":
        out_w, out_h = OUTPUT_SIZES["9:16"]
        if content_type == "podcast":
            pair = detect_two_speakers(samples)
            if pair:
                panel = out_w / (out_h / 2)
                return LayoutPlan("split", src_w, src_h, duration,
                                  top=panel_region(pair[0], 0.45, src_w, src_h, panel),
                                  bottom=panel_region(pair[1], 0.45, src_w, src_h, panel))
        elif content_type != "film":  # a streamer cam over other content; never in a movie scene
            cam = detect_facecam(samples)
            if cam:
                cam_panel = out_w / (out_h * 0.35)
                game_panel = out_w / (out_h * 0.65)
                top = panel_region(cam.cx, cam.cy, src_w, src_h, cam_panel, width_frac=min(cam.w * 2.6, 0.5))
                bottom = panel_region(0.5, 0.5, src_w, src_h, game_panel)
                return LayoutPlan("facecam", src_w, src_h, duration, top=top, bottom=bottom)

    segments = segment_track(samples, duration, cuts=cuts)
    if layout == "auto" and crop_loss(src_w, src_h, aspect) >= FIT_MIN_LOSS:
        segments = apply_fit_spans(segments, faceless_spans(samples, duration, cuts), duration)
    if segments and all(s.kind == "fit" for s in segments):
        return LayoutPlan("fit", src_w, src_h, duration, segments)
    has_faces = any(s.faces for s in samples)
    return LayoutPlan("track" if has_faces else "center", src_w, src_h, duration, segments)


def faces_in_box(samples: list[Sample], box: Box, src_w: int, src_h: int) -> list[Sample]:
    """Re-express face boxes relative to the picture inside the bars."""
    x, y, w, h = box
    return [Sample(s.t, [FaceBox((f.x * src_w - x) / w, (f.y * src_h - y) / h, f.w * src_w / w, f.h * src_h / h,
                                 f.score) for f in s.faces]) for s in samples]


def analyze_video(video_path: Path | str, *, aspect: Aspect = "9:16", content_type: str = "gaming",
                  sample_fps: float = 4.0, layout: Framing = "auto") -> LayoutPlan:
    # A forced full-frame fit never looks at faces, so skip the detector (most of the analysis time).
    samples, w, h, duration, cuts, box = sample_faces(video_path, sample_fps, detect_faces=layout != "fit")
    if box is None:
        return plan_layout(samples, w, h, duration, aspect=aspect, content_type=content_type, cuts=cuts,
                           layout=layout)
    # Plan against the picture only; the renderer crops the bars off before anything else.
    plan = plan_layout(faces_in_box(samples, box, w, h), box[2], box[3], duration, aspect=aspect,
                       content_type=content_type, cuts=cuts, layout=layout)
    plan.src_crop = box
    return plan
