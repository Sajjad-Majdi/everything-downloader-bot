import time

import pytest
from conftest import MB, insta_info, yt_info

from dlbot import formats as F
from dlbot import signing, texts
from dlbot.config import Config
from dlbot.stream import disposition, ffmpeg_cmd, safe_name


# ---- format picking ----------------------------------------------------------
def test_youtube_options_prefer_avc_and_add_best():
    opts = F.build_options(yt_info())
    labels = [o.label for o in opts]
    assert labels[:3] == ["360p", "720p", "1080p"]
    o360, o720, o1080 = opts[:3]
    assert o360.spec == "18"                    # progressive avc beats video-only at same codec
    assert o720.spec == "136+140"               # avc preferred over vp9, m4a audio
    assert o720.size == 70 * MB and not o720.approx
    assert o1080.spec == "137+140"
    assert opts[-1].label == "2160p" and opts[-1].spec == "313+140"   # vp9-only top tier, no duplicate "best"
    assert not any(o.best for o in opts)


def test_best_added_when_top_tier_has_better_non_avc():
    info = yt_info()
    info["formats"].append({"format_id": "248", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 1920,
                            "height": 1080, "tbr": 4000, "filesize": 150 * MB, "protocol": "https", "url": "u"})
    info["formats"] = [f for f in info["formats"] if f["format_id"] != "313"]
    opts = F.build_options(info)
    assert opts[-2].spec == "137+140" and opts[-1].best and opts[-1].spec == "248+140"
    assert opts[-1].label == "بهترین 1080p"


def test_storyboard_and_audio_are_not_video():
    info = yt_info()
    vids = [f["format_id"] for f in info["formats"] if F.is_video(f)]
    assert "sb0" not in vids and "140" not in vids and "251" not in vids


def test_size_estimate_from_tbr_when_no_filesize():
    size, approx = F.fmt_size({"tbr": 800}, 100)
    assert size == 800 * 100 * 125 and approx
    assert F.fmt_size({}, None) == (None, True)


def test_vertical_video_label_uses_short_side():
    opts = F.build_options(insta_info())
    assert opts[0].label == "720p"
    assert opts[0].spec == "8"                   # muxed progressive mp4 wins
    assert F.direct_url(opts[0]).startswith("https://scontent-fra.cdninstagram.com/")


def test_single_url_info_without_formats():
    opts = F.build_options({"url": "https://video.twimg.com/x.mp4", "ext": "mp4", "format_id": "0", "height": 720,
                            "width": 1280, "duration": 10})
    assert len(opts) == 1 and opts[0].spec == "0"


def test_no_video_formats():
    assert F.build_options({"formats": [{"format_id": "a", "vcodec": "none", "acodec": "mp3", "url": "x"}]}) == []


# ---- delivery plan -----------------------------------------------------------
def test_plan_small_youtube_uploads():
    o = F.build_options(yt_info())[0]
    assert F.plan(o) == "upload"


def test_plan_big_youtube_streams():
    o = F.build_options(yt_info())[1]          # 70 MB, googlevideo is IP locked
    assert F.direct_url(o) is None
    assert F.plan(o) == "stream_link"


def test_plan_small_instagram_sends_url_then_big_gives_direct():
    o = F.build_options(insta_info())[0]
    assert F.plan(o) == "url_send"
    o.size = 80 * MB
    assert F.plan(o) == "direct_link"
    o.size = 30 * MB
    assert F.plan(o) == "upload"
    o.size = None
    assert F.plan(o) == "upload"


def test_direct_url_rejects_lookalike_host():
    o = F.Option("x", "1", 720, 1, False, {"url": "https://evilcdninstagram.com/a.mp4", "protocol": "https"})
    assert F.direct_url(o) is None


def test_entries():
    pl = {"_type": "playlist", "entries": [{"_type": "url", "url": "https://youtu.be/1"}, None]}
    assert len(F.entries_of(pl)) == 1
    assert not F.entry_is_resolved(pl["entries"][0])
    assert F.entry_is_resolved(insta_info())


# ---- signing -----------------------------------------------------------------
def test_sign_roundtrip_and_tamper():
    tok = signing.sign(b"k", {"u": "https://x", "f": "137+140", "t": "عنوان"}, 3600)
    body = signing.verify(b"k", tok)
    assert body["f"] == "137+140" and body["t"] == "عنوان"
    with pytest.raises(signing.BadToken):
        signing.verify(b"other", tok)
    raw, mac = tok.split(".")
    with pytest.raises(signing.BadToken):
        signing.verify(b"k", raw[:-2] + "AA." + mac)
    with pytest.raises(signing.BadToken):
        signing.verify(b"k", "garbage")


def test_sign_expiry():
    tok = signing.sign(b"k", {"u": "x"}, 3600, now=time.time() - 7200)
    with pytest.raises(signing.BadToken, match="expired"):
        signing.verify(b"k", tok)


# ---- config / text -----------------------------------------------------------
def test_config_derives_secrets_and_ids():
    c = Config.from_env({"TELEGRAM_BOT_TOKEN": "1:abc", "RENDER_EXTERNAL_URL": "https://x.onrender.com/",
                         "ALLOWED_USER_IDS": "394891587, 5"})
    assert c.public_url == "https://x.onrender.com"
    assert c.allowed_ids == {394891587, 5}
    assert len(c.webhook_secret) == 48 and c.link_key
    assert Config.from_env({}).webhook_secret == ""


def test_persian_formatting():
    assert texts.human_size(5 * MB) == "۵٫۰ مگ"
    assert texts.human_size(70 * MB, True) == "~۷۰ مگ"
    assert texts.human_size(None) == "حجم نامعلوم"
    assert texts.human_duration(3725) == "۱:۰۲:۰۵"
    assert texts.human_duration(65) == "۱:۰۵"
    assert "کوکی" in texts.error_text(Exception("Sign in to confirm you're not a bot"))


# ---- stream helpers ----------------------------------------------------------
def test_ffmpeg_cmd_remux_copy_fragmented():
    cmd = ffmpeg_cmd([{"url": "https://v", "http_headers": {"User-Agent": "UA", "Referer": "r"}},
                      {"url": "https://a", "protocol": "https"}])
    assert cmd.count("-i") == 2 and "copy" in cmd and "pipe:1" in cmd
    assert "frag_keyframe+empty_moov+default_base_moof" in cmd
    assert cmd[cmd.index("-user_agent") + 1] == "UA"
    assert "Referer: r\r\n" in cmd


def test_safe_name_and_disposition():
    assert safe_name('a/b:c?"d') == "a b c d.mp4"
    assert "filename*=UTF-8''" in disposition("ویدیو.mp4")
