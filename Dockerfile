FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Cloudflare WARP as a local proxy for YouTube (YouTube answers 403 to Render's IPs):
# wgcf registers a free WARP device, wireproxy runs WireGuard in userspace (no root needed).
ARG WGCF_VERSION=2.3.1
ARG WIREPROXY_VERSION=1.1.3
RUN python -c "import io, tarfile, urllib.request as u; \
u.urlretrieve('https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_amd64', '/usr/local/bin/wgcf'); \
t = tarfile.open(fileobj=io.BytesIO(u.urlopen('https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_amd64.tar.gz').read())); \
open('/usr/local/bin/wireproxy', 'wb').write(t.extractfile([m for m in t.getmembers() if m.name.endswith('wireproxy')][0]).read())" \
 && chmod +x /usr/local/bin/wgcf /usr/local/bin/wireproxy \
 && wgcf --help > /dev/null && wireproxy --version

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
