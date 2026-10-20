FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && yt-dlp --version && deno --version && ffmpeg -version | head -1

COPY dlbot ./dlbot
COPY assets ./assets
RUN useradd -m bot && chown -R bot /app
USER bot

ENV PORT=10000
EXPOSE 10000
CMD ["python", "-m", "dlbot"]
