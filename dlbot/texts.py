"""Persian UI text (short, colloquial) and small formatters."""
from __future__ import annotations

FA_DIGITS = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")


def fa(x) -> str:
    return str(x).translate(FA_DIGITS)


def human_size(n: int | None, approx: bool = False) -> str:
    if n is None:
        return "حجم نامعلوم"
    mb = n / (1024 * 1024)
    if mb >= 1024:
        s = f"{mb / 1024:.1f} گیگ"
    elif mb >= 10:
        s = f"{mb:.0f} مگ"
    else:
        s = f"{mb:.1f} مگ"
    return fa(("~" if approx else "") + s)


def human_duration(sec) -> str:
    if not sec:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return fa(f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}")


START = (
    "سلام! لینک ویدیو رو از یوتیوب، اینستاگرام، ایکس (توییتر) یا هر سایت دیگه‌ای بفرست.\n"
    "کیفیت رو انتخاب کن، بقیه‌ش با من."
)
NOT_ALLOWED = "ببخشید، این ربات شخصیه و فقط برای صاحبش کار می‌کنه."
NO_LINK = "یه لینک ویدیو بفرست تا برات بگیرمش."
CHECKING = "دارم لینک رو نگاه می‌کنم…"
CHOOSE = "کیفیت رو انتخاب کن:"
NO_VIDEO = "توی این لینک ویدیویی پیدا نکردم."
QUEUED = "یه کار دیگه دستمه؛ نوبتت که شد شروع می‌کنم…"
PREPARING = "دارم آماده‌ش می‌کنم…"
UPLOADING = "دارم برات می‌فرستمش…"
EXPIRED = "این دکمه قدیمی شده؛ لینک رو دوباره بفرست."
DONE_CACHED = "اینو قبلاً گرفته بودم، بفرما."
BUTTON_DL = "دانلود مستقیم"
PLAYLIST = "این یه پلی‌لیسته ({n} تا ویدیو). کدومش رو می‌خوای؟"
CAROUSEL = "این پست {n} تا ویدیو داره. کدومش رو می‌خوای؟"
ALL_ITEMS = "همه‌شون"
ITEM = "ویدیوی {i}"
OTHER_QUALITIES = "کیفیت‌های دیگه"


def downloading(pct: float | None, total: int | None) -> str:
    if pct is None:
        return "دارم دانلود می‌کنم…"
    tail = f" از {human_size(total)}" if total else ""
    return fa(f"دارم دانلود می‌کنم… {pct:.0f}٪") + tail


def uploading(pct: float | None) -> str:
    if pct is None:
        return UPLOADING
    return fa(f"دارم برات می‌فرستمش… {pct:.0f}٪")


def too_big_stream(size_txt: str) -> str:
    return (
        f"حجمش ({size_txt}) از سقف ۵۰ مگ تلگرام بیشتره.\n"
        "از این لینک مستقیم بگیرش؛ تا یه ساعت کار می‌کنه:"
    )


def too_big_direct(size_txt: str) -> str:
    return (
        f"حجمش ({size_txt}) از سقف ۵۰ مگ تلگرام بیشتره.\n"
        "این لینک مستقیم خود سایته؛ چند ساعت بعد ممکنه باطل بشه:"
    )


def error_text(exc: BaseException) -> str:
    msg = str(exc).lower()
    if "confirm you" in msg and "bot" in msg:
        return "یوتیوب گیر داده که «ربات نیستی». باید کوکی یوتیوب بدی (COOKIES_B64)."
    if "private" in msg or "login" in msg or "log in" in msg or "sign in" in msg:
        return "این ویدیو خصوصیه یا لاگین می‌خواد. با کوکی (COOKIES_B64) درست می‌شه."
    if "unsupported url" in msg:
        return "این لینک رو نمی‌شناسم؛ مطمئنی لینک ویدیوئه؟"
    if "not available" in msg or "unavailable" in msg or "removed" in msg or "404" in msg:
        return "این ویدیو در دسترس نیست؛ شاید پاک شده."
    if "geo" in msg or "country" in msg:
        return "این ویدیو برای سرور من در دسترس نیست (محدودیت کشوری)."
    if "timed out" in msg or "timeout" in msg:
        return "سایت جواب نداد؛ یه بار دیگه امتحان کن."
    return "نشد بگیرمش. یه بار دیگه امتحان کن؛ اگه باز نشد، لینک دیگه‌ای بفرست."
