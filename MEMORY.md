# Everything Downloader (Telegram bot) - memory

## What
- Sajjad's private video downloader bot, @sajjad_downloader_bot. YouTube, Instagram, X and any yt-dlp site. Video only.
- Only user ids in `ALLOWED_USER_IDS` (default 394891587; first id = owner) can use it; others get a polite refusal.

## Stack
- Python 3.12, aiohttp webhook server, yt-dlp[default] + deno (YouTube JS challenges), ffmpeg. Docker (python:3.12-slim + apt ffmpeg).
- Package `dlbot/`: `server.py` (routes /health, /webhook, /dl/<token>, /bench, /netbench; sets webhook + profile photo + yt-dlp warm-up on startup), `bot.py` (handlers, `run_job` pipeline with per-phase timings), `formats.py` (quality options, size, delivery plan), `ytdl.py` (yt-dlp calls), `thumbs.py` (<=320 px JPEG preview), `stream.py` (/dl pipe), `signing.py`, `tg.py` (tiny Bot API client), `texts.py` (Persian UI).

## Delivery rules
- Fits 49 MB: download to temp dir (stream-copy merge), sendVideo with thumbnail + width/height/duration, temp dir deleted in `finally`.
- Instagram/X CDN (cdninstagram, fbcdn, twimg) <= 20 MB: sendVideo by URL (Telegram fetches it); falls back to upload.
- Too big: direct CDN link when not IP-locked, else signed 1 h link `/dl/<token>` (YouTube); split formats piped through ffmpeg as fragmented mp4, single files proxied with Range. No disk writes.
- file_id LRU cache per (url, item, format). 1 job at a time (`MAX_JOBS`), 2 streams (`MAX_STREAMS`).
- Audio pick prefers plain AAC over YouTube's `-drc` twin.

## Speed (measured on Render free, 0.1 CPU, 2026-10-20)
- Download reuses the menu's extraction (no 2nd yt-dlp run; info kept 20 min, newest 6 only).
- yt-dlp cache in /tmp + preprocessed-player cache for YouTube JS challenges (deno 11 s -> ~5 s). YouTube extraction still ~8-10 s (deno ~5 s, network ~3 s). Warm-up extraction at boot.
- Uploads: 1 MiB reads (64 KiB reads were CPU-bound), fresh TCP connection per file, stall watchdog (bytes acked by Telegram, via the kernel send queue) retries below 400 KB/s after 8 s. Root cause of the 11-min case: some connections Singapore -> api.telegram.org crawl at ~50 KB/s (22 MB took 5-9 min, reproduced 3x). BBR is not available on Render (ENOENT).
- After the fixes: 12:54 YouTube 360p (35 MB) ~10-11 s after the click (+9-10 s extract before the menu); 480p merged 22 MB 10-23 s; 19 s clip ~3 s (+7 s extract); X 360p by URL ~0.6 s (+3.7 s extract); IG 1.7 MB reel 4-25 s (Telegram's own processing after the body varies 1-22 s).
- `YT_FAST_CLIENT` (JS-less visionos, no cookies) is off: Render IP gets LOGIN_REQUIRED.
- `/bench?k=BENCH_KEY&url=..&p=360` runs the real pipeline to the owner silently and deletes the message; returns timings. `/netbench?k=..&target=tg|cf` measures egress.

## Env (Render)
- `TELEGRAM_BOT_TOKEN`, `COOKIES_B64` (Sajjad pastes them), `WEBHOOK_SECRET`, `BENCH_KEY` (Render-generated), `ALLOWED_USER_IDS`, `PUBLIC_URL` (else `RENDER_EXTERNAL_URL`).
- Link signing key derives from the token unless `LINK_SECRET` is set.
- Deploy hook + bench key live in `C:\Users\Sajjad\secrets\everything-downloader-ops.env` (never commit).

## Deploy
- Repo: github.com/Sajjad-Majdi/everything-downloader-bot (public), branch main.
- Render: service `everything-downloader` (srv-dbbkqb67bikc738tbigg), https://everything-downloader-1r7j.onrender.com, Docker, free, Singapore, health /health (shows running commit), same workspace as HadithYab. No auto-deploy: after a push, `curl -X POST $RENDER_DEPLOY_HOOK` (or dashboard Manual Deploy > Deploy latest commit).
- Keep-alive: Cloudflare Worker `downloader-keepalive` (`keepalive/`, cron */13 -> /health), `wrangler deploy` from `keepalive/` (laptop needs HTTPS_PROXY=http://127.0.0.1:10808).
- Update yt-dlp: bump the pin in `requirements.txt`, push, deploy.

## Tests
- `.venv\Scripts\python.exe -m pytest -q` with `NO_PROXY=127.0.0.1,localhost` (mocks + real local ffmpeg/yt-dlp/multipart runs on 127.0.0.1; no internet). Real site tests only on Render via /bench.
