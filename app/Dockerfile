# The app service (issue #8). Build context is this folder (app/).
#
# Same Python minor version as the CI test image. Run as PUID:PGID, which docker-compose.yml
# sets with `user:`: the image has no user of its own and writes nothing at runtime
# (no .pyc files, no cache), so any uid works.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg: yt-dlp needs it to merge YouTube's separate video and audio streams into one mp4 and
# to convert thumbnails (#18). Without recommends, no apt lists left behind.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# Copied on its own so a code change does not re-run the install layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY ytp ./ytp

EXPOSE 8000

# /health answers 503 when the app is degraded, and urlopen raises on that, so the container
# turns unhealthy exactly when /health says so.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["python", "-m", "ytp"]
