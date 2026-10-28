"""Persian UI text and small formatters.

Short, standard written Persian, no chatty first person («در حال دانلود ویدیو…», not «دارم برات
دانلودش می‌کنم»): it is a downloader bot, not an assistant (Sajjad, 2026-10-26).
"""
from __future__ import annotations

FA_DIGITS = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")


def fa(x) -> str:
    return str(x).translate(FA_DIGITS)


def human_size(n: int | None, approx: bool = False) -> str:
    if n is None:
        return "حجم نامعلوم"
    mb = n / (1024 * 1024)
    if mb >= 1024:
        s = f"{mb / 1024:.1f} گیگابایت"
    elif mb >= 10:
        s = f"{mb:.0f} مگابایت"
    else:
        s = f"{mb:.1f} مگابایت"
    return fa(("~" if approx else "") + s)


def human_duration(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return fa(f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")


START = (
    "لینک ویدیو را از یوتیوب، اینستاگرام، ایکس (توییتر) یا سایت‌های دیگر ارسال کنید.\n"
    "پس از انتخاب کیفیت، ویدیو ارسال می‌شود."
)
NOT_ALLOWED = "این ربات خصوصی است و فقط برای مالک آن فعال است."
CAPACITY_FULL = "ظرفیت کاربران این ربات تکمیل شده است و کاربر جدید پذیرفته نمی‌شود."
TRY_LATER = "ربات موقتاً در دسترس نیست. چند دقیقه بعد دوباره تلاش کنید."
MONTH_LIMIT = "سقف حجم ماهانه‌ی ربات پر شده است. ارسال ویدیو از اول ماه میلادی بعد دوباره ممکن است."
NO_LINK = "لطفاً لینک یک ویدیو را ارسال کنید."
CHECKING = "در حال بررسی لینک…"
CHOOSE = "کیفیت را انتخاب کنید:"
NO_VIDEO = "در این لینک ویدیویی یافت نشد."
QUEUED = "در صف انتظار…"
PREPARING = "در حال آماده‌سازی…"
UPLOADING = "در حال ارسال ویدیو…"
EXPIRED = "این دکمه منقضی شده است. لینک را دوباره ارسال کنید."
BUTTON_DL = "دانلود مستقیم"
PLAYLIST = "این لینک یک فهرست پخش با {n} ویدیو است. ویدیوی موردنظر را انتخاب کنید:"
CAROUSEL = "این پست {n} ویدیو دارد. ویدیوی موردنظر را انتخاب کنید:"
ALL_ITEMS = "همه‌ی ویدیوها"
ITEM = "ویدیوی {i}"


def downloading(pct: float | None, total: int | None) -> str:
    if pct is None:
        return "در حال دانلود ویدیو…"
    tail = f" از {human_size(total)}" if total else ""
    return fa(f"در حال دانلود ویدیو… {pct:.0f}٪") + tail


def uploading(pct: float | None) -> str:
    if pct is None:
        return UPLOADING
    return fa(f"در حال ارسال ویدیو… {pct:.0f}٪")


def too_big_stream(size_txt: str) -> str:
    return (
        f"حجم ویدیو ({size_txt}) از سقف ۵۰ مگابایتی تلگرام بیشتر است.\n"
        "لینک دانلود مستقیم (معتبر تا یک ساعت):"
    )


def too_big_direct(size_txt: str) -> str:
    return (
        f"حجم ویدیو ({size_txt}) از سقف ۵۰ مگابایتی تلگرام بیشتر است.\n"
        "لینک مستقیم سایت (ممکن است پس از چند ساعت منقضی شود):"
    )


def error_text(exc: BaseException) -> str:
    msg = str(exc).lower()
    if "confirm you" in msg and "bot" in msg:
        return "یوتیوب درخواست را به‌عنوان ربات رد کرد. کوکی یوتیوب (COOKIES_B64) لازم است."
    if "private" in msg or "login" in msg or "log in" in msg or "sign in" in msg:
        return "این ویدیو خصوصی است یا ورود به حساب لازم دارد. کوکی (COOKIES_B64) لازم است."
    if "unsupported url" in msg:
        return "این لینک پشتیبانی نمی‌شود."
    if "not available" in msg or "unavailable" in msg or "removed" in msg or "404" in msg:
        return "این ویدیو در دسترس نیست؛ ممکن است حذف شده باشد."
    if "geo" in msg or "country" in msg:
        return "این ویدیو در کشور سرور در دسترس نیست."
    if "timed out" in msg or "timeout" in msg:
        return "سایت پاسخ نداد. دوباره تلاش کنید."
    return "دریافت ویدیو ناموفق بود. دوباره تلاش کنید یا لینک دیگری ارسال کنید."
