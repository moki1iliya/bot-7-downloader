# -*- coding: utf-8 -*-
"""
🔥 ربات دانلودر PRO — نسخه ۲ (تقویت‌شده)
─────────────────────────────────────────
سرعت:  aria2c چندرشته‌ای + دانلود ۱۶ تکه‌ای هم‌زمان + کش هوشمند file_id (ارسال آنی لینک تکراری)
امکانات: نوار پیشرفت زنده، کیفیت 1080p، تامبنیل، پنل ادمین شیشه‌ای، پیام همگانی، کوکی اینستاگرام

راه‌اندازی:
    pip install -r requirements.txt
    sudo apt install ffmpeg aria2      # aria2 برای حداکثر سرعت (اختیاری ولی خیلی مؤثر)
    export BOT_TOKEN="توکن"
    python bot.py
"""

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

# ─────────────────────── تنظیمات ───────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "توکن-ربات-اینجا")
ADMIN_IDS = {123456789}                  # آیدی عددی ادمین‌ها
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)
CACHE_FILE = Path("cache.json")          # کش file_id — لینک تکراری = ارسال آنی ⚡
COOKIES_FILE = Path("cookies.txt")       # اگر باشد، برای اینستاگرام/یوتیوب استفاده می‌شود
MAX_FILE_MB = 49                         # با Local Bot API Server می‌توانید تا 1990 بگذارید
MAX_CONCURRENT = 4
HAS_ARIA2 = shutil.which("aria2c") is not None

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("dlbot")
URL_RE = re.compile(r"https?://\S+")
INSTA_YT_RE = re.compile(
    r"https?://(www\.)?(instagram\.com|instagr\.am|youtu\.be|youtube\.com|m\.youtube\.com)/\S+",
    re.I,
)
semaphore = asyncio.Semaphore(MAX_CONCURRENT)

STATS = {"users": set(), "downloads": 0, "cache_hits": 0, "errors": 0, "started": time.time()}
PENDING: dict[str, str] = {}
BOT_ENABLED = True

# کش پایدار: {"url|quality": {"file_id": ..., "type": "video|audio|document", "caption": ...}}
CACHE: dict[str, dict] = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

def save_cache():
    CACHE_FILE.write_text(json.dumps(CACHE, ensure_ascii=False))


# ─────────────────────── پنل‌های شیشه‌ای ───────────────────────
def main_panel(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📥 راهنمای دانلود", callback_data="help"),
         InlineKeyboardButton("🌐 سایت‌ها", callback_data="sites")],
        [InlineKeyboardButton("📊 وضعیت ربات", callback_data="status")],
    ]
    if uid in ADMIN_IDS:
        rows.append([InlineKeyboardButton("👑 پنل مدیریت", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def quality_panel(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 بهترین کیفیت", callback_data=f"dl|best|{token}"),
         InlineKeyboardButton("🎬 1080p", callback_data=f"dl|1080|{token}")],
        [InlineKeyboardButton("⚡ 720p", callback_data=f"dl|720|{token}"),
         InlineKeyboardButton("📱 480p", callback_data=f"dl|480|{token}")],
        [InlineKeyboardButton("🎵 فقط صدا (MP3)", callback_data=f"dl|audio|{token}")],
        [InlineKeyboardButton("❌ انصراف", callback_data=f"cancel|{token}")],
    ])


def admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 آمار کامل", callback_data="a_stats"),
         InlineKeyboardButton("🗑 پاک‌کردن کش", callback_data="a_clearcache")],
        [InlineKeyboardButton("🔴 خاموش" if BOT_ENABLED else "🟢 روشن", callback_data="a_toggle"),
         InlineKeyboardButton("📢 پیام همگانی", callback_data="a_bcast")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="home")],
    ])


def back_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="home")]])


def progress_bar(pct: float) -> str:
    filled = int(pct / 10)
    return "▰" * filled + "▱" * (10 - filled)


# ─────────────────────── دستورات ───────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATS["users"].add(update.effective_user.id)
    speed = "aria2c چندرشته‌ای 🚀" if HAS_ARIA2 else "چندتکه‌ای ⚡"
    await update.message.reply_text(
        f"سلام {update.effective_user.first_name} عزیز 👋\n\n"
        f"🔥 <b>ربات دانلودر PRO</b>\n"
        f"⚙️ موتور دانلود: {speed}\n\n"
        "لینکت رو بفرست تا با حداکثر سرعت برات دانلود کنم!\n"
        "💡 لینک‌های تکراری در <b>کمتر از ۱ ثانیه</b> ارسال می‌شن (کش هوشمند)",
        parse_mode=ParseMode.HTML, reply_markup=main_panel(update.effective_user.id),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATS["users"].add(uid)

    # حالت پیام همگانی ادمین
    if context.user_data.pop("bcast_mode", False) and uid in ADMIN_IDS:
        sent = 0
        for u in list(STATS["users"]):
            try:
                await context.bot.send_message(u, update.message.text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await update.message.reply_text(f"📢 پیام به {sent} کاربر ارسال شد.")
        return

    if not BOT_ENABLED and uid not in ADMIN_IDS:
        await update.message.reply_text("🔴 ربات موقتاً در حال به‌روزرسانی است.")
        return

    chat_type = update.effective_chat.type
    text = update.message.text or ""

    # ── حالت گروه: لینک اینستاگرام/یوتیوب => دانلود و ارسال کاملاً خودکار، بدون پنل و بدون حرف اضافه ──
    if chat_type in ("group", "supergroup"):
        gm = INSTA_YT_RE.search(text)
        if not gm:
            return  # در گروه به هیچ چیز دیگه‌ای واکنش نشون نمی‌ده
        url = gm.group(0)
        chat_id = update.effective_chat.id
        status = await context.bot.send_message(chat_id, "⏬ ...")
        asyncio.create_task(
            download_and_send(
                StatusHandle(context.bot, chat_id, status.message_id),
                context, url, "best",
                delete_message_id=update.message.message_id,
                source_chat_id=chat_id,
            )
        )
        return

    # ── حالت خصوصی: رفتار قبلی با پنل انتخاب کیفیت ──
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("❗ لطفاً یک لینک معتبر بفرست.", reply_markup=main_panel(uid))
        return
    token = uuid.uuid4().hex[:10]
    PENDING[token] = m.group(0)
    await update.message.reply_text(
        "🔗 لینکت رو گرفتم!\n🎯 کیفیت مورد نظرت رو انتخاب کن:",
        reply_markup=quality_panel(token),
    )


# ─────────────────────── موتور دانلود پرسرعت ───────────────────────
def build_opts(quality: str, out_tmpl: str, hook) -> dict:
    opts = {
        "outtmpl": out_tmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 16,        # ⚡ ۱۶ تکه هم‌زمان
        "http_chunk_size": 10 * 1024 * 1024,        # چانک‌های ۱۰ مگابایتی
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
        "progress_hooks": [hook],
        "writethumbnail": True,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if HAS_ARIA2:
        # aria2c: دانلود ۱۶ اتصالی از هر سرور — جهش سرعت واقعی 🚀
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = ["-x", "16", "-s", "16", "-k", "1M",
                                            "--min-split-size=1M", "--file-allocation=none"]
    if quality == "audio":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
                {"key": "EmbedThumbnail"},
            ],
        })
    elif quality in ("1080", "720", "480"):
        opts["format"] = (f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]"
                          f"/bv*[height<={quality}]+ba/b[height<={quality}]/best")
    else:
        opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
    return opts


def do_download(url: str, quality: str, folder: Path, hook) -> tuple[Path, dict]:
    out = str(folder / "%(title).60s.%(ext)s")
    with yt_dlp.YoutubeDL(build_opts(quality, out, hook)) as ydl:
        info = ydl.extract_info(url, download=True)
    media_exts = (".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".opus", ".ogg")
    files = [f for f in folder.iterdir() if f.suffix.lower() in media_exts]
    if not files:
        files = [f for f in folder.iterdir() if f.suffix.lower() not in (".jpg", ".png", ".webp")]
    if not files:
        raise RuntimeError("فایلی دانلود نشد")
    path = max(files, key=lambda f: f.stat().st_size)
    thumb = next((f for f in folder.iterdir() if f.suffix.lower() in (".jpg", ".png", ".webp")), None)
    meta = {"duration": int(info.get("duration") or 0),
            "width": info.get("width"), "height": info.get("height"),
            "title": info.get("title", "فایل"), "thumb": thumb}
    return path, meta


# ─────────────────────── دکمه‌ها ───────────────────────
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ENABLED
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    if data == "home":
        await q.edit_message_text("🏠 منوی اصلی:", reply_markup=main_panel(uid))
    elif data == "help":
        await q.edit_message_text(
            "📥 <b>راهنما</b>\n\n۱. لینک رو کپی کن\n۲. همینجا بفرست\n"
            "۳. کیفیت رو انتخاب کن\n۴. با نوار پیشرفت زنده دانلود میشه 🚀\n\n"
            "💡 لینک تکراری؟ آنی از کش ارسال میشه!",
            parse_mode=ParseMode.HTML, reply_markup=back_panel())
    elif data == "sites":
        await q.edit_message_text(
            "🌐 <b>سایت‌های پشتیبانی‌شده</b>\n\n"
            "✅ اینستاگرام (پست، ریلز، استوری با کوکی)\n✅ یوتیوب + شورتز (تا 1080p)\n"
            "✅ توییتر / X\n✅ تیک‌تاک\n✅ فیسبوک\n✅ ساندکلاد\n✅ آپارات\n"
            "✅ و صدها سایت دیگر...",
            parse_mode=ParseMode.HTML, reply_markup=back_panel())
    elif data == "status":
        engine = "aria2c 🚀" if HAS_ARIA2 else "استاندارد ⚡"
        await q.edit_message_text(
            f"📊 <b>وضعیت</b>\n\n🟢 آنلاین\n⚙️ موتور: {engine}\n"
            f"📥 دانلودها: {STATS['downloads']}\n⚡ ارسال از کش: {STATS['cache_hits']}",
            parse_mode=ParseMode.HTML, reply_markup=back_panel())

    # ── پنل ادمین ──
    elif data == "admin" and uid in ADMIN_IDS:
        await q.edit_message_text("👑 <b>پنل مدیریت</b>", parse_mode=ParseMode.HTML,
                                  reply_markup=admin_panel())
    elif data == "a_stats" and uid in ADMIN_IDS:
        up = int(time.time() - STATS["started"])
        await q.edit_message_text(
            f"📈 <b>آمار کامل</b>\n\n👥 کاربران: <code>{len(STATS['users'])}</code>\n"
            f"📥 دانلودها: <code>{STATS['downloads']}</code>\n"
            f"⚡ ارسال از کش: <code>{STATS['cache_hits']}</code>\n"
            f"💾 حجم کش: <code>{len(CACHE)}</code> فایل\n"
            f"⚠️ خطاها: <code>{STATS['errors']}</code>\n"
            f"⏱ آپتایم: <code>{up // 3600}h {(up % 3600) // 60}m</code>",
            parse_mode=ParseMode.HTML, reply_markup=admin_panel())
    elif data == "a_clearcache" and uid in ADMIN_IDS:
        CACHE.clear(); save_cache()
        await q.edit_message_text("🗑 کش پاک شد.", reply_markup=admin_panel())
    elif data == "a_toggle" and uid in ADMIN_IDS:
        BOT_ENABLED = not BOT_ENABLED
        await q.edit_message_text(f"وضعیت ربات: {'🟢 روشن' if BOT_ENABLED else '🔴 خاموش'}",
                                  reply_markup=admin_panel())
    elif data == "a_bcast" and uid in ADMIN_IDS:
        context.user_data["bcast_mode"] = True
        await q.edit_message_text("📢 پیام همگانی رو بفرست (پیام بعدی شما به همه ارسال می‌شود):")

    elif data.startswith("cancel|"):
        PENDING.pop(data.split("|")[1], None)
        await q.edit_message_text("❌ لغو شد.", reply_markup=main_panel(uid))
    elif data.startswith("dl|"):
        _, quality, token = data.split("|")
        url = PENDING.pop(token, None)
        if not url:
            await q.edit_message_text("⏰ منقضی شده؛ دوباره لینک رو بفرست.")
            return
        status = StatusHandle(context.bot, q.message.chat_id, q.message.message_id)
        asyncio.create_task(download_and_send(status, context, url, quality))


# ─────────────────────── رابط یکسان برای ویرایش/حذف پیام وضعیت ───────────────────────
class StatusHandle:
    """یک رابط مشترک برای ویرایش پیام وضعیت، چه از CallbackQuery بیاد چه از پیام معمولی گروه."""
    def __init__(self, bot, chat_id: int, message_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id

    async def edit_message_text(self, text: str, parse_mode=None):
        await self.bot.edit_message_text(
            chat_id=self.chat_id, message_id=self.message_id,
            text=text, parse_mode=parse_mode,
        )

    async def delete(self):
        try:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)
        except Exception:
            pass


# ─────────────────────── دانلود + ارسال ───────────────────────
async def download_and_send(status: "StatusHandle", context, url: str, quality: str,
                             delete_message_id: int | None = None, source_chat_id: int | None = None):
    chat_id = status.chat_id
    cache_key = f"{url}|{quality}"

    # ⚡ کش: ارسال آنی بدون دانلود مجدد
    if cache_key in CACHE:
        c = CACHE[cache_key]
        try:
            sender = {"video": context.bot.send_video, "audio": context.bot.send_audio,
                      "document": context.bot.send_document}[c["type"]]
            await sender(chat_id, c["file_id"], caption=c.get("caption", ""))
            STATS["cache_hits"] += 1
            await finish_success(status, context, delete_message_id)
            return
        except BadRequest:
            CACHE.pop(cache_key, None)  # file_id نامعتبر شده؛ دانلود عادی

    loop = asyncio.get_running_loop()
    last_edit = {"t": 0.0}

    def hook(d):
        if d["status"] != "downloading":
            return
        now = time.time()
        if now - last_edit["t"] < 2.5:           # هر ۲.۵ ثانیه یک‌بار (ضد فلاد)
            return
        last_edit["t"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        pct = done / total * 100 if total else 0
        speed = (d.get("speed") or 0) / (1024 * 1024)
        txt = (f"⏬ در حال دانلود...\n\n{progress_bar(pct)} {pct:.0f}%\n"
               f"🚀 سرعت: {speed:.1f} MB/s\n"
               f"📦 {done / 1048576:.1f} / {total / 1048576:.1f} MB")
        asyncio.run_coroutine_threadsafe(safe_edit(status, txt), loop)

    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir()
    try:
        async with semaphore:
            await safe_edit(status, "🔍 در حال دریافت اطلاعات لینک...")
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            path, meta = await loop.run_in_executor(None, do_download, url, quality, folder, hook)

            size_mb = path.stat().st_size / (1024 * 1024)
            if size_mb > MAX_FILE_MB:
                raise RuntimeError(f"حجم ({size_mb:.0f}MB) بیشتر از حد مجاز تلگرام است")

            await safe_edit(status, f"📤 در حال آپلود ({size_mb:.1f}MB)...")
            caption = f"🎬 {meta['title'][:80]}\n📦 {size_mb:.1f}MB | 🤖 @{context.bot.username}"
            thumb_f = open(meta["thumb"], "rb") if meta.get("thumb") else None
            kw = dict(caption=caption, read_timeout=600, write_timeout=600)
            with open(path, "rb") as f:
                if quality == "audio" or path.suffix in (".mp3", ".m4a", ".opus", ".ogg"):
                    msg = await context.bot.send_audio(chat_id, f, thumbnail=thumb_f,
                                                       duration=meta["duration"], **kw)
                    ftype, fid = "audio", msg.audio.file_id
                elif path.suffix in (".mp4", ".mkv", ".webm", ".mov"):
                    msg = await context.bot.send_video(
                        chat_id, f, thumbnail=thumb_f, supports_streaming=True,
                        duration=meta["duration"], width=meta.get("width"),
                        height=meta.get("height"), **kw)
                    ftype, fid = "video", msg.video.file_id
                else:
                    msg = await context.bot.send_document(chat_id, f, **kw)
                    ftype, fid = "document", msg.document.file_id
            if thumb_f:
                thumb_f.close()

            CACHE[cache_key] = {"file_id": fid, "type": ftype, "caption": caption}
            save_cache()
            STATS["downloads"] += 1
            await finish_success(status, context, delete_message_id)
    except Exception as e:
        STATS["errors"] += 1
        log.error("Download failed: %s", e)
        await safe_edit(status, f"❌ خطا:\n<code>{str(e)[:300]}</code>", html=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def finish_success(status: "StatusHandle", context, delete_message_id: int | None):
    """بعد از آپلود موفق: در حالت خودکار گروه، پیام لینک اصلی + پیام وضعیت حذف می‌شن تا گروه شلوغ نشه."""
    if delete_message_id:
        try:
            await context.bot.delete_message(chat_id=status.chat_id, message_id=delete_message_id)
        except Exception as e:
            log.warning("Could not delete source message: %s", e)
        await status.delete()
    else:
        await safe_edit(status, "✅ ارسال شد! لینک بعدی رو بفرست 😉")


async def safe_edit(q, text: str, html: bool = False):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML if html else None)
    except BadRequest:
        pass  # پیام تغییری نکرده یا حذف شده


def main():
    try:
        import uvloop; uvloop.install()   # حلقه رویداد سریع‌تر (اختیاری)
        log.info("uvloop enabled ⚡")
    except ImportError:
        pass
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot PRO is running... | aria2c: %s", HAS_ARIA2)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
