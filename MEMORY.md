# Everything Downloader (Telegram bot) - memory

## What
- Sajjad's private video downloader bot, t.me/sajjaddownloaderbot. YouTube, Instagram, X and any yt-dlp site. Video only.
- Only user ids in `ALLOWED_USER_IDS` (default 394891587) can use it; others get a polite refusal.

## Stack
- Python 3.12, aiohttp webhook server, yt-dlp[default] + deno (YouTube JS challenges), ffmpeg. Docker (python:3.12-slim + apt ffmpeg).
- Package `dlbot/`: `server.py` (routes /health, /webhook, /dl/<token>; sets its own webhook on startup), `bot.py` (handlers), `formats.py` (quality options, size, delivery plan), `ytdl.py` (yt-dlp calls), `stream.py` (/dl pipe), `signing.py`, `tg.py` (tiny Bot API client), `texts.py` (Persian UI).

## Delivery rules
- Fits 49 MB: download to temp dir (stream-copy merge), sendVideo, temp dir deleted in `finally`.
- Instagram/X CDN (cdninstagram, fbcdn, twimg) <= 20 MB: sendVideo by URL (Telegram fetches it); falls back to upload.
- Too big: direct CDN link when not IP-locked, else signed 1 h link `/dl/<token>` (YouTube); split formats are piped through ffmpeg as fragmented mp4, single files proxied with Range support. No disk writes.
- file_id LRU cache per (url, item, format). 1 job at a time (`MAX_JOBS`), 2 streams (`MAX_STREAMS`).

## Env (Render)
- `TELEGRAM_BOT_TOKEN` (Sajjad pastes it), `WEBHOOK_SECRET` (random, set), `ALLOWED_USER_IDS`, optional `COOKIES_B64` (Netscape cookies.txt, base64), `PUBLIC_URL` (else Render's `RENDER_EXTERNAL_URL`).
- Link signing key derives from the token unless `LINK_SECRET` is set.

## Deploy
- Repo: github.com/Sajjad-Majdi/everything-downloader-bot (private), branch main. Render auto-deploys on push.
- Keep-alive: Cloudflare Worker `downloader-keepalive` (`keepalive/`, cron */13 -> /health), deploy with `wrangler deploy` from `keepalive/`.
- Update yt-dlp: bump the pin in `requirements.txt`, push (Render rebuilds).

## Tests
- `.venv\Scripts\python.exe -m pytest -q` (mocks + real local ffmpeg/yt-dlp runs on 127.0.0.1; no internet). Real site tests only on Render.
