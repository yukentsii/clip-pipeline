"""Dry-run every configured uploader against a real video without publishing anything.

    python -m clip_pipeline.publish.scripts.preflight path/to/clip.mp4 [--only youtube,instagram,tiktok] [--screenshot out.png]

YouTube: OAuth refresh + request body. Instagram: real container upload + processing, never published.
TikTok: real Studio session, file attached, caption typed, Post button enabled, never clicked.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from clip_pipeline.publish.uploaders import UploadError, UploadJob, default_uploaders


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--only", default="youtube,instagram,tiktok")
    ap.add_argument("--title", default="clip-pipeline preflight check")
    ap.add_argument("--screenshot", type=Path, help="save TikTok Studio screenshot before the Post click")
    args = ap.parse_args(argv)
    if not args.video.is_file():
        print(f"no such file: {args.video}")
        return 2

    failures = 0
    for name, up in default_uploaders().items():
        if name not in args.only.split(","):
            continue
        job = UploadJob("preflight", name, args.video, args.title, "Preflight dry run, not published.",
                        ["clippipeline", "test"], {"visibility": "private"})
        t0 = time.monotonic()
        if not up.is_configured():
            print(f"[{name:9}] NOT CONFIGURED")
            failures += 1
            continue
        try:
            kwargs = {"screenshot": args.screenshot} if name == "tiktok" and args.screenshot else {}
            msg = up.preflight(job, **kwargs)
            print(f"[{name:9}] OK   ({time.monotonic() - t0:4.0f}s) {msg}", flush=True)
        except UploadError as exc:
            failures += 1
            print(f"[{name:9}] FAIL ({time.monotonic() - t0:4.0f}s) retryable={exc.retryable}: {exc}", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
