import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MB = 1024 * 1024


def yt_info(duration=600):
    """A trimmed YouTube-like info dict (dash video-only + audio + progressive 18 + storyboard)."""
    return {
        "id": "abc", "title": "Test video", "duration": duration,
        "webpage_url": "https://www.youtube.com/watch?v=abc", "extractor": "youtube",
        "formats": [
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none", "format_note": "storyboard",
             "url": "https://i.ytimg.com/sb"},
            {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 129,
             "filesize": 10 * MB, "protocol": "https", "url": "https://rr1.googlevideo.com/a140"},
            {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 135,
             "filesize": 10 * MB, "protocol": "https", "url": "https://rr1.googlevideo.com/a251"},
            {"format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2", "width": 640,
             "height": 360, "tbr": 500, "filesize_approx": 30 * MB, "protocol": "https",
             "url": "https://rr1.googlevideo.com/v18"},
            {"format_id": "134", "ext": "mp4", "vcodec": "avc1.4d401e", "acodec": "none", "width": 640,
             "height": 360, "tbr": 400, "filesize": 15 * MB, "protocol": "https", "url": "https://rr1.googlevideo.com/v134"},
            {"format_id": "136", "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none", "width": 1280,
             "height": 720, "tbr": 1500, "filesize": 60 * MB, "protocol": "https", "url": "https://rr1.googlevideo.com/v136"},
            {"format_id": "247", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 1280,
             "height": 720, "tbr": 1200, "filesize": 50 * MB, "protocol": "https", "url": "https://rr1.googlevideo.com/v247"},
            {"format_id": "137", "ext": "mp4", "vcodec": "avc1.640028", "acodec": "none", "width": 1920,
             "height": 1080, "tbr": 3000, "filesize": 120 * MB, "protocol": "https", "url": "https://rr1.googlevideo.com/v137"},
            {"format_id": "313", "ext": "webm", "vcodec": "vp9", "acodec": "none", "width": 3840,
             "height": 2160, "tbr": 15000, "filesize": 700 * MB, "protocol": "https", "url": "https://rr1.googlevideo.com/v313"},
        ],
    }


def insta_info():
    return {
        "id": "C1", "title": "Reel", "duration": 30, "webpage_url": "https://www.instagram.com/reel/C1/",
        "formats": [
            {"format_id": "dash-v", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "width": 720, "height": 1280,
             "tbr": 1000, "protocol": "https", "url": "https://scontent.cdninstagram.com/v/dash.mp4"},
            {"format_id": "dash-a", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 64,
             "protocol": "https", "url": "https://scontent.cdninstagram.com/v/a.m4a"},
            {"format_id": "8", "ext": "mp4", "width": 720, "height": 1280, "filesize": 4 * MB,
             "protocol": "https", "url": "https://scontent-fra.cdninstagram.com/v/prog.mp4"},
        ],
    }
