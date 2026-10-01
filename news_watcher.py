#!/usr/bin/env python3
"""
News watcher: چک کردن فیدهای خبری انگلیسی، ترجمه به فارسی، ارسال به تلگرام.
کاملا رایگان: RSS + ترجمه‌ی گوگل (بدون کلید) + Telegram Bot.

اجرا:
  python news_watcher.py            # حلقه‌ی دائمی (روی کامپیوتر خودت)
  python news_watcher.py --once     # یک بار چک کن و خارج شو (برای GitHub Actions)

متغیرهای محیطی:
  TELEGRAM_TOKEN   توکن ربات (از @BotFather)
  TELEGRAM_CHAT_ID آیدی چت خودت
  KEYWORDS         اختیاری، کلیدواژه‌های شخصی خودت، مثلا "bitcoin,election".
                   خبری که شامل این‌ها باشه همیشه «🔴 خیلی مهم» حساب می‌شه.
  ONLY_IMPORTANT   پیش‌فرض روشن: فقط خبرهایی که به یکی از دسته‌های مهم می‌خورن میان.
                   برای دریافت همه‌ی خبرها (با برچسب «⚪ عادی») مقدارش رو 0 بذار.
  INTERVAL         اختیاری، فاصله‌ی چک بر حسب ثانیه (پیش‌فرض 60)

تست دسته‌بندی بدون شبکه:
  python news_watcher.py --classify "Iran sanctions tighten as oil jumps"
"""
import argparse
import html
import json
import os
import re
import sys
import time

import feedparser
import requests
from deep_translator import GoogleTranslator

# ---------------------------------------------------------------------------
# منابع. رویترز و AP فید رسمی رایگان ندارن، پس از Google News RSS با فیلتر
# سایت استفاده می‌کنیم (when:1h یعنی فقط خبرهای یک ساعت اخیر).
# ---------------------------------------------------------------------------
FEEDS = {
    "BBC": "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "Guardian": "https://www.theguardian.com/world/rss",
    "NPR": "https://feeds.npr.org/1001/rss.xml",
    "Reuters": "https://news.google.com/rss/search?q=site:reuters.com+when:1h&hl=en-US&gl=US&ceid=US:en",
    "AP": "https://news.google.com/rss/search?q=site:apnews.com+when:1h&hl=en-US&gl=US&ceid=US:en",
}

STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
MAX_SEEN = 5000
SUMMARY_CHARS = 300

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
KEYWORDS = [k.strip().lower() for k in os.environ.get("KEYWORDS", "").split(",") if k.strip()]
ONLY_IMPORTANT = os.environ.get("ONLY_IMPORTANT", "").strip().lower() not in ("0", "false", "no", "off")

translator = GoogleTranslator(source="en", target="fa")
_http_cache = {}  # etag / modified برای هر فید (فقط در حالت حلقه کار می‌کنه)


def load_seen():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f), True
    except (FileNotFoundError, json.JSONDecodeError):
        return [], False


def save_seen(seen):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(seen[-MAX_SEEN:], f)


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


_PERSIAN = re.compile(r"[؀-ۿ]")


def _tr_gtx(text):
    """اندپوینت مستقیم گوگل (بدون کلید)."""
    r = requests.get(
        "https://translate.googleapis.com/translate_a/single",
        params={"client": "gtx", "sl": "en", "tl": "fa", "dt": "t", "q": text},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    r.raise_for_status()
    return "".join(part[0] for part in r.json()[0] if part and part[0])


def _tr_deep(text):
    return translator.translate(text)


def _tr_mymemory(text):
    """MyMemory: رایگان، سقف روزانه دارد، ورودی تا ۵۰۰ کاراکتر."""
    r = requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q": text[:500], "langpair": "en|fa"},
        timeout=20,
    )
    r.raise_for_status()
    out = r.json()["responseData"]["translatedText"]
    if "MYMEMORY WARNING" in out.upper():
        raise RuntimeError(out)
    return out


BACKENDS = [("google-gtx", _tr_gtx), ("deep-translator", _tr_deep), ("mymemory", _tr_mymemory)]


def translate_ex(text):
    """برمی‌گردونه: (متن ترجمه‌شده یا اصلی, اسم موتور موفق یا None, لیست خطاها)."""
    if not text:
        return "", "empty", []
    errors = []
    for name, fn in BACKENDS:
        try:
            out = (fn(text[:4500]) or "").strip()
            if out and _PERSIAN.search(out):
                return out, name, errors
            errors.append(f"{name}: خروجی فارسی نبود")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
    return text, None, errors  # هیچ‌کدوم کار نکرد: متن انگلیسی


def translate(text):
    out, backend, errors = translate_ex(text)
    if backend is None:
        for err in errors:
            print(f"[translate error] {err}", file=sys.stderr)
    return out


def send_telegram(text):
    if not TOKEN or not CHAT_ID:
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID تنظیم نشده؛ فقط چاپ می‌کنم:\n" + text + "\n")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    if not r.ok:
        print(f"[telegram error] {r.status_code} {r.text}", file=sys.stderr)


def fetch(name, url):
    kwargs = {}
    cached = _http_cache.get(name, {})
    if cached.get("etag"):
        kwargs["etag"] = cached["etag"]
    if cached.get("modified"):
        kwargs["modified"] = cached["modified"]
    feed = feedparser.parse(url, agent="Mozilla/5.0 news-watcher", **kwargs)
    _http_cache[name] = {
        "etag": getattr(feed, "etag", None),
        "modified": getattr(feed, "modified", None),
    }
    return feed.entries or []


# ---------------------------------------------------------------------------
# دسته‌بندی اهمیت خبر (کاملا آفلاین و رایگان، بر پایه‌ی کلیدواژه)
#
# هر دسته: (نام, سطح, [کلیدواژه‌ها])
#   سطح 3 = 🔴 خیلی مهم | 2 = 🟠 مهم | 1 = 🟡 قابل توجه
# کلیدواژه‌ای که با ^ شروع بشه حساس به حروف بزرگ/کوچکه (مثل ^Fed).
# اگه خبر به ۲ دسته یا بیشتر بخوره، یک سطح بالاتر می‌ره
# (مثلا «Iran» + «sanctions» = خیلی مهم).
# برای ویرایش، فقط کلمه‌ها رو تو این لیست اضافه یا حذف کن.
# ---------------------------------------------------------------------------
LEVELS = {3: "🔴 خیلی مهم", 2: "🟠 مهم", 1: "🟡 قابل توجه", 0: "⚪ عادی"}

CATEGORIES = [
    ("🇮🇷 هسته‌ای ایران", 3, [
        "JCPOA", "IAEA", "snapback", "enrichment", "uranium",
        "nuclear deal", "nuclear talks", "nuclear program", "nuclear sites",
    ]),
    ("⚔️ تنش ایران و خلیج فارس", 3, [
        "Hormuz", "Persian Gulf", "IRGC", "Revolutionary Guard",
        "Iran-Israel", "Israel-Iran", "Israel and Iran", "Iran and Israel",
        "US-Iran", "Iran-US", "Iran and the US", "Iran and the United States",
    ]),
    ("💱 اقتصاد و ارز ایران", 3, [
        "rial", "Iran's economy", "Iranian economy", "Iran economy",
        "Iranian currency", "Central Bank of Iran", "Tehran Stock", "Iran inflation",
    ]),
    ("🚫 تحریم", 2, ["sanction", "sanctions", "embargo"]),
    ("🛢 نفت و انرژی", 2, [
        "oil price", "oil prices", "crude", "Brent", "OPEC", "oil output",
        "oil supply", "oil exports", "oil tanker", "Iranian oil", "Iran oil",
        "natural gas price",
    ]),
    ("🏦 فدرال رزرو و اقتصاد آمریکا", 2, [
        "^Fed", "Federal Reserve", "Fed chair", "interest rate", "interest rates",
        "rate cut", "rate hike", "US inflation", "inflation data", "inflation report",
        "consumer prices", "^CPI", "jobs report", "nonfarm", "Treasury yield",
        "Treasury yields", "US dollar", "dollar index", "greenback",
    ]),
    ("🥇 طلا و پناهگاه امن", 2, [
        "gold price", "gold prices", "gold rally", "gold surges", "gold hits",
        "gold futures", "gold reserves", "bullion", "safe haven", "safe-haven",
    ]),
    ("💥 تنش نظامی (عمومی)", 2, [
        "missile", "missiles", "airstrike", "airstrikes", "air strike",
        "drone attack", "ceasefire", "Houthi", "Houthis", "Red Sea",
    ]),
    ("📍 خبر مربوط به ایران", 1, ["Iran", "Iranian", "Iranians", "Tehran"]),
    ("🌍 اقتصاد جهانی و تجارت", 1, [
        "recession", "tariff", "tariffs", "trade war", "banking crisis",
        "debt default", "market selloff", "stock market crash",
    ]),
]


def _compile(kw):
    if kw.startswith("^"):
        return kw[1:], re.compile(r"\b" + re.escape(kw[1:]) + r"\b")
    return kw, re.compile(r"\b" + re.escape(kw) + r"\b", re.IGNORECASE)


_COMPILED = [(name, level, [_compile(k) for k in kws]) for name, level, kws in CATEGORIES]
_USER_COMPILED = [_compile(k) for k in KEYWORDS]


def classify(title, summary=""):
    """برمی‌گردونه: (سطح 0..3, [نام دسته‌ها], [کلیدواژه‌های پیداشده])."""
    blob = f"{title} {summary}".replace("’", "'").replace("‘", "'")
    cats, found, level = [], [], 0
    for name, lvl, kws in _COMPILED:
        hits = [label for label, rx in kws if rx.search(blob)]
        if hits:
            cats.append(name)
            found += hits
            level = max(level, lvl)
    user_hits = [label for label, rx in _USER_COMPILED if rx.search(blob)]
    if user_hits:
        cats.append("🔎 کلیدواژه‌ی شما")
        found += user_hits
        level = 3
    elif len(cats) >= 2:
        level = min(3, level + 1)
    uniq = list(dict.fromkeys(found))
    return level, cats, uniq


def format_message(source, title, summary, link, level=0, cats=None, kws=None):
    fa_title = translate(title)
    fa_summary = translate(summary) if summary else ""
    parts = [LEVELS.get(level, LEVELS[0])]
    if cats:
        parts[0] += "\n🏷 دسته: " + " | ".join(cats)
    parts.append(f"<b>{html.escape(fa_title)}</b>")
    if fa_summary:
        parts.append(html.escape(fa_summary))
    parts.append(f"<i>{html.escape(title)}</i>")
    if kws:
        parts.append("🔑 " + html.escape(", ".join(kws[:5])))
    parts.append(f"📰 {html.escape(source)} — <a href=\"{html.escape(link)}\">لینک خبر</a>")
    return "\n\n".join(parts)


def check_once(send_initial=False):
    seen, existed = load_seen()
    seen_set = set(seen)
    first_run = not existed and not send_initial
    new_count = 0

    for name, url in FEEDS.items():
        try:
            entries = fetch(name, url)
        except Exception as e:
            print(f"[{name}] fetch error: {e}", file=sys.stderr)
            continue

        # قدیمی‌ترین اول، که ترتیب پیام‌ها درست باشه
        for e in reversed(entries):
            uid = e.get("id") or e.get("link")
            if not uid or uid in seen_set:
                continue
            seen_set.add(uid)
            seen.append(uid)

            if first_run:
                continue  # اولین اجرا: فقط علامت بزن، سیل پیام نفرست

            title = clean(e.get("title", ""))
            summary = clean(e.get("summary", ""))[:SUMMARY_CHARS]
            if not title:
                continue
            level, cats, kws = classify(title, summary)
            if ONLY_IMPORTANT and level == 0:
                continue

            send_telegram(format_message(name, title, summary, e.get("link", ""), level, cats, kws))
            new_count += 1
            time.sleep(1)  # محدودیت نرخ تلگرام

    save_seen(seen)
    if first_run:
        print(f"اولین اجرا: {len(seen)} خبر موجود علامت‌گذاری شد. از این به بعد فقط خبرهای جدید میاد.")
    else:
        print(f"{new_count} خبر جدید ارسال شد.")


def run_test():
    """یک پیام آزمایشی (با ترجمه) می‌فرسته تا از درستی توکن و آیدی چت مطمئن بشی."""
    if not TOKEN or not CHAT_ID:
        print("خطا: TELEGRAM_TOKEN یا TELEGRAM_CHAT_ID تنظیم نشده (Secrets رو چک کن).", file=sys.stderr)
        sys.exit(1)
    sample = "Iran sanctions tighten as oil prices and gold jump after Fed signals rate cut"
    _, backend, errors = translate_ex(sample)
    print(f"موتور ترجمه‌ی موفق: {backend}")
    for err in errors:
        print(f"  [خطای ترجمه] {err}", file=sys.stderr)
    level, cats, kws = classify(sample)
    text = format_message(
        "Test (نمونه‌ی آزمایشی، خبر واقعی نیست)",
        sample,
        "If you can read this in Persian, translation and Telegram delivery both work.",
        "https://www.bbc.com/news",
        level, cats, kws,
    )
    if backend is None:
        text = "⚠️ ترجمه کار نکرد (جزئیات تو لاگ GitHub Actions)\n\n" + text
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=20,
    )
    if not r.ok:
        print(f"[telegram error] {r.status_code} {r.text}", file=sys.stderr)
        sys.exit(1)
    print("پیام آزمایشی ارسال شد. تلگرامت رو چک کن.")
    if backend is None:
        print("هشدار: همه‌ی موتورهای ترجمه شکست خوردن (بالا رو ببین).", file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="فقط یک پیام آزمایشی به تلگرام بفرست")
    ap.add_argument("--classify", metavar="TEXT", help="فقط دسته‌بندی یک تیتر رو چاپ کن (بدون شبکه)")
    ap.add_argument("--once", action="store_true", help="یک بار چک کن و خارج شو")
    ap.add_argument("--send-initial", action="store_true", help="در اولین اجرا هم خبرهای فعلی رو بفرست")
    args = ap.parse_args()

    if args.classify:
        level, cats, kws = classify(args.classify)
        print(f"{LEVELS[level]} | دسته‌ها: {cats} | کلیدواژه‌ها: {kws}")
        return

    if args.test:
        run_test()
        return

    if args.once:
        check_once(args.send_initial)
        return

    interval = int(os.environ.get("INTERVAL", "60"))
    print(f"شروع شد. هر {interval} ثانیه چک می‌کنم. (Ctrl+C برای توقف)")
    while True:
        try:
            check_once(args.send_initial)
        except Exception as e:
            print(f"[loop error] {e}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    main()
