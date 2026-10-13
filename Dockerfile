# clip-pipeline: one image, one data volume.
#
#   docker compose up -d --build
#
# Everything the service writes (database, working files, inbox, saved logins, downloaded models)
# lives under /data, which compose maps to ./data on the host.
FROM python:3.11-slim-bookworm

# ffmpeg            cutting, rendering and (with a GPU passed in) hardware encoding
# fonts-liberation  Liberation Sans stands in for Arial, so captions look the same as on Windows
# nodejs            the JavaScript runtime yt-dlp needs for YouTube
# libgl1, libglib   needed by OpenCV / MediaPipe
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-liberation fontconfig nodejs libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# TikTok is posted through a real browser. Skip with:  --build-arg WITH_TIKTOK=false  (saves ~400 MB)
ARG WITH_TIKTOK=true
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN if [ "$WITH_TIKTOK" = "true" ]; then python -m playwright install --with-deps chromium; fi

COPY clip_pipeline ./clip_pipeline

# Not root. The uid matches the usual first user on a Linux host, so files in ./data stay yours.
RUN useradd --create-home --uid 1000 clip && mkdir -p /data && chown clip:clip /data
USER clip

ENV DATA_DIR=/data \
    BIND_HOST=0.0.0.0 \
    PORT=8000 \
    HF_HOME=/data/cache/huggingface \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)"

CMD ["python", "-m", "clip_pipeline", "serve"]
