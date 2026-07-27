# -*- coding: utf-8 -*-
"""
ربات دانلودر PRO + آپلود خودکار اینستاگرام
pip install python-telegram-bot yt-dlp instagrapi
env: BOT_TOKEN / INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD
"""

import asyncio, json, logging, os, re, shutil, time, uuid
from pathlib import Path
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ClientError
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# ───────────── تنظیمات ─────────────
BOT_TOKEN          = os.environ.get("BOT_TOKEN", "توکن-اینجا")
ADMIN_IDS          = {7438138322}
IG_USERNAME        = os.environ.get("INSTAGRAM_USERNAME", "")
IG_PASSWORD        = os.environ.get("INSTAGRAM_PASSWORD", "")
IG_SESSION_FILE    = Path("ig_session.json")
DOWNLOAD_DIR       = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)
CACHE_FILE         = Path("cache.json")
MAX_FILE_MB        = 49
MAX_CONCURRENT     = 4
HAS_ARIA2          = shutil.which("aria2c") is not None

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")
URL_RE      = re.compile(r"https?://\S+")
INSTA_YT_RE = re.compile(r"https?://(www\.)?(instagram\.com|instagr\.am|youtu\.be|youtube\.com|m\.youtube\.com)/\S+", re.I)
semaphore   = asyncio.Semaphore(MAX_CONCURRENT)
STATS       = {"users": set(), "downloads": 0, "cache_hits": 0, "errors": 0, "started": time.time()}
PENDING: dict[str, str] = {}
BOT_ENABLED = True
CACHE: dict  = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

def save_cache():
    CACHE_FILE.write_text(json.dumps(CACHE, ensure_ascii=False))

# ───────────── اینستاگرام ─────────────
_ig: Client | None = None

def ig_client() -> Client:
    global _ig
    if _ig:
        return _ig
    cl = Client()
    cl.delay_range = [1, 3]
    if IG_SESSION_FILE.exists():
        try:
            cl.load_settings(IG_SESSION_FILE)
            cl.login(IG_USERNAME, IG_PASSWORD)
            cl.dump_settings(IG_SESSION_FILE)
            _ig = cl
            return cl
        except LoginRequired:
            IG_SESSION_FILE.unlink(missing_ok=True)
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings(IG_SESSION_FILE)
    _ig = cl
    log.info("Instagram login OK ✅")
    return cl

async def ig_upload(path: Path, caption: str) -> str:
    loop = asyncio.get_running_loop()
    def _do():
        cl = ig_client()
        is_vid = path.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm")
        media = cl.video_upload(path, caption=caption) if is_vid else cl.photo_upload(path, caption=caption)
        return f"https://www.instagram.com/p/{media.code}/"
    return await loop.run_in_executor(None, _do)

# ───────────── پنل‌ها ─────────────
def main_panel(uid):
    rows = [
        [InlineKeyboardButton("📥 راهنما", callback_data="help"),
         InlineKeyboardButton("🌐 سایت‌ها", callback_data="sites")],
        [InlineKeyboardButton("📊 وضعیت", callback_data="status")],
    ]
    if uid in ADMIN_IDS:
        rows.append([InlineKeyboardButton("👑 ادمین", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def quality_panel(token):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 بهترین", callback_data=f"dl|best|{token}"),
         InlineKeyboardButton("🎬 1080p",  callback_data=f"dl|1080|{token}")],
        [InlineKeyboardButton("⚡ 720p",   callback_data=f"dl|720|{token}"),
         InlineKeyboardButton("📱 480p",   callback_data=f"dl|480|{token}")],
        [InlineKeyboardButton("🎵 MP3",    callback_data=f"dl|audio|{token}")],
        [InlineKeyboardButton("❌ انصراف", callback_data=f"cancel|{token}")],
    ])

def admin_panel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 آمار",        callback_data="a_stats"),
         InlineKeyboardButton("🗑 پاک کش",      callback_data="a_clearcache")],
        [InlineKeyboardButton("🔴 خاموش" if BOT_ENABLED else "🟢 روشن", callback_data="a_toggle"),
         InlineKeyboardButton("📢 همگانی",      callback_data="a_bcast")],
        [InlineKeyboardButton("🔙 برگشت",       callback_data="home")],
    ])

def back_panel():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 برگشت", callback_data="home")]])

def pbar(pct):
    f = int(pct / 10)
    return "▰" * f + "▱" * (10 - f)

# ───────────── دستورات ─────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATS["users"].add(update.effective_user.id)
    await update.message.reply_text(
        f"سلام {update.effective_user.first_name} 👋\n\n"
        "🔥 <b>ربات دانلودر PRO</b>\n"
        "لینک بفرست، دانلود + آپلود اینستاگرام خودکار!\n"
        "💡 لینک‌های تکراری آنی ارسال می‌شن ⚡",
        parse_mode=ParseMode.HTML,
        reply_markup=main_panel(update.effective_user.id),
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    STATS["users"].add(uid)

    if context.user_data.pop("bcast_mode", False) and uid in ADMIN_IDS:
        sent = 0
        for u in list(STATS["users"]):
            try:
                await context.bot.send_message(u, update.message.text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await update.message.reply_text(f"📢 ارسال به {sent} نفر.")
        return

    if not BOT_ENABLED and uid not in ADMIN_IDS:
        await update.message.reply_text("🔴 ربات در حال به‌روزرسانیه.")
        return

    text = update.message.text or ""
    chat_type = update.effective_chat.type

    if chat_type in ("group", "supergroup"):
        gm = INSTA_YT_RE.search(text)
        if not gm:
            return
        url    = gm.group(0)
        chat_id = update.effective_chat.id
        status = await context.bot.send_message(chat_id, "⏬ ...")
        asyncio.create_task(download_and_send(
            StatusHandle(context.bot, chat_id, status.message_id),
            context, url, "best",
            delete_message_id=update.message.message_id,
            source_chat_id=chat_id,
        ))
        return

    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("❗ یه لینک معتبر بفرست.", reply_markup=main_panel(uid))
        return
    token = uuid.uuid4().hex[:10]
    PENDING[token] = m.group(0)
    await update.message.reply_text(
        "🔗 لینک گرفتم! کیفیت رو انتخاب کن:",
        reply_markup=quality_panel(token),
    )

# ───────────── موتور دانلود ─────────────
def build_opts(quality, out_tmpl, hook):
    base = {
        "outtmpl": out_tmpl, "noplaylist": True, "quiet": True,
        "no_warnings": True, "retries": 5, "fragment_retries": 5,
        "socket_timeout": 20, "concurrent_fragment_downloads": 16,
        "http_chunk_size": 10 * 1024 * 1024,
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
        "progress_hooks": [hook], "writethumbnail": True,
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}],
    }
    if HAS_ARIA2:
        base.update({"external_downloader": "aria2c",
                     "external_downloader_args": ["-x16", "-s16", "-k1M"]})
    if quality == "audio":
        base["format"] = "bestaudio/best"
        base["postprocessors"].append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"})
    elif quality == "best":
        base["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    else:
        h = {"1080": 1080, "720": 720, "480": 480}.get(quality, 720)
        base["format"] = f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/best[height<={h}]"
    if Path("cookies.txt").exists():
        base["cookiefile"] = "cookies.txt"
    return base

def do_download(url, quality, folder, hook):
    out  = str(folder / "%(title).60s.%(ext)s")
    opts = build_opts(quality, out, hook)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = sorted(folder.iterdir(), key=lambda f: f.stat().st_size, reverse=True)
    main  = next((f for f in files if f.suffix not in (".jpg", ".png", ".webp")), files[0])
    thumb = next((f for f in files if f.suffix in (".jpg", ".png", ".webp")), None)
    return main, {
        "title":    info.get("title", "video"),
        "duration": int(info.get("duration") or 0),
        "width":    info.get("width"),
        "height":   info.get("height"),
        "thumb":    str(thumb) if thumb else None,
    }

class StatusHandle:
    def __init__(self, bot, chat_id, message_id):
        self.bot, self.chat_id, self.message_id = bot, chat_id, message_id
    async def edit_message_text(self, text, parse_mode=None):
        await self.bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id,
                                         text=text, parse_mode=parse_mode)
    async def delete(self):
        try:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)
        except Exception:
            pass

async def safe_edit(q, text, html=False):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML if html else None)
    except BadRequest:
        pass

async def download_and_send(status: StatusHandle, context, url, quality,
                             delete_message_id=None, source_chat_id=None):
    chat_id   = status.chat_id
    cache_key = f"{url}|{quality}"

    if cache_key in CACHE:
        c = CACHE[cache_key]
        try:
            sender = {"video": context.bot.send_video,
                      "audio": context.bot.send_audio,
                      "document": context.bot.send_document}[c["type"]]
            await sender(chat_id, c["file_id"], caption=c.get("caption", ""))
            STATS["cache_hits"] += 1
            await finish_success(status, context, delete_message_id)
            return
        except BadRequest:
            CACHE.pop(cache_key, None)

    loop     = asyncio.get_running_loop()
    last_edit = {"t": 0.0}

    def hook(d):
        if d["status"] != "downloading":
            return
        now = time.time()
        if now - last_edit["t"] < 2.5:
            return
        last_edit["t"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done  = d.get("downloaded_bytes", 0)
        pct   = done / total * 100 if total else 0
        speed = (d.get("speed") or 0) / (1024 * 1024)
        asyncio.run_coroutine_threadsafe(
            safe_edit(status,
                f"⏬ دانلود...\n{pbar(pct)} {pct:.0f}%\n"
                f"🚀 {speed:.1f} MB/s | {done/1048576:.1f}/{total/1048576:.1f} MB"),
            loop,
        )

    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir()
    try:
        async with semaphore:
            await safe_edit(status, "🔍 دریافت اطلاعات...")
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
            path, meta = await loop.run_in_executor(None, do_download, url, quality, folder, hook)

            size_mb = path.stat().st_size / (1024 * 1024)
            if size_mb > MAX_FILE_MB:
                raise RuntimeError(f"حجم {size_mb:.0f}MB از حد مجاز بیشتره")

            await safe_edit(status, f"📤 آپلود تلگرام ({size_mb:.1f}MB)...")
            caption = f"🎬 {meta['title'][:80]}\n📦 {size_mb:.1f}MB"
            thumb_f = open(meta["thumb"], "rb") if meta.get("thumb") else None
            kw = dict(caption=caption, read_timeout=600, write_timeout=600)

            with open(path, "rb") as f:
                if quality == "audio" or path.suffix in (".mp3", ".m4a", ".opus", ".ogg"):
                    msg = await context.bot.send_audio(chat_id, f, thumbnail=thumb_f,
                                                       duration=meta["duration"], **kw)
                    ftype, fid = "audio", msg.audio.file_id
                elif path.suffix in (".mp4", ".mkv", ".webm", ".mov"):
                    msg = await context.bot.send_video(chat_id, f, thumbnail=thumb_f,
                                                       supports_streaming=True,
                                                       duration=meta["duration"],
                                                       width=meta.get("width"),
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

            # ─── آپلود خودکار اینستاگرام ───
            if IG_USERNAME and IG_PASSWORD:
                await safe_edit(status, "📲 آپلود به اینستاگرام...")
                try:
                    post_url = await ig_upload(path, caption=meta["title"][:2200])
                    await context.bot.send_message(chat_id, f"✅ اینستاگرام آپلود شد!\n🔗 {post_url}")
                except ClientError as e:
                    await context.bot.send_message(chat_id, f"❌ خطای اینستاگرام: {e}")

    except Exception as e:
        STATS["errors"] += 1
        log.error("failed: %s", e)
        await safe_edit(status, f"❌ خطا:\n<code>{str(e)[:300]}</code>", html=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

async def finish_success(status: StatusHandle, context, delete_message_id):
    if delete_message_id:
        try:
            await context.bot.delete_message(chat_id=status.chat_id, message_id=delete_message_id)
        except Exception:
            pass
        await status.delete()
    else:
        await safe_edit(status, "✅ تموم شد! لینک بعدی؟ 😉")

# ───────────── کالبک‌ها ─────────────
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global BOT_ENABLED
    q    = update.callback_query
    uid  = q.from_user.id
    data = q.data
    await q.answer()

    if data == "home":
        await q.edit_message_text("خونه 🏠", reply_markup=main_panel(uid))
    elif data == "help":
        await q.edit_message_text(
            "📥 <b>راهنما</b>\n\nلینک بفرست → کیفیت انتخاب کن → دانلود + آپلود اینستاگرام خودکار!",
            parse_mode=ParseMode.HTML, reply_markup=back_panel())
    elif data == "sites":
        await q.edit_message_text(
            "🌐 <b>سایت‌های پشتیبانی‌شده</b>\n\n"
            "✅ اینستاگرام ✅ یوتیوب ✅ توییتر\n✅ تیک‌تاک ✅ فیسبوک ✅ آپارات و...",
            parse_mode=ParseMode.HTML, reply_markup=back_panel())
    elif data == "status":
        await q.edit_message_text(
            f"📊 دانلودها: {STATS['downloads']} | کش: {STATS['cache_hits']} | خطا: {STATS['errors']}",
            reply_markup=back_panel())
    elif data == "admin" and uid in ADMIN_IDS:
        await q.edit_message_text("👑 پنل ادمین", reply_markup=admin_panel())
    elif data == "a_stats" and uid in ADMIN_IDS:
        up = int(time.time() - STATS["started"])
        await q.edit_message_text(
            f"📈 کاربران: {len(STATS['users'])}\n"
            f"📥 دانلود: {STATS['downloads']}\n⚡ کش: {STATS['cache_hits']}\n"
            f"⏱ آپتایم: {up//3600}h {(up%3600)//60}m",
            reply_markup=admin_panel())
    elif data == "a_clearcache" and uid in ADMIN_IDS:
        CACHE.clear(); save_cache()
        await q.edit_message_text("🗑 کش پاک شد.", reply_markup=admin_panel())
    elif data == "a_toggle" and uid in ADMIN_IDS:
        BOT_ENABLED = not BOT_ENABLED
        await q.edit_message_text(f"{'🟢 روشن' if BOT_ENABLED else '🔴 خاموش'}", reply_markup=admin_panel())
    elif data == "a_bcast" and uid in ADMIN_IDS:
        context.user_data["bcast_mode"] = True
        await q.edit_message_text("📢 پیام همگانی رو بفرست:")
    elif data.startswith("cancel|"):
        PENDING.pop(data.split("|")[1], None)
        await q.edit_message_text("❌ لغو شد.", reply_markup=main_panel(uid))
    elif data.startswith("dl|"):
        _, quality, token = data.split("|")
        url = PENDING.pop(token, None)
        if not url:
            await q.edit_message_text("⏰ منقضی شد، دوباره لینک بفرست.")
            return
        status = StatusHandle(context.bot, q.message.chat_id, q.message.message_id)
        asyncio.create_task(download_and_send(status, context, url, quality))

# ───────────── اجرا ─────────────
def main():
    try:
        import uvloop; uvloop.install()
    except ImportError:
        pass
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot running | aria2c: %s | Instagram: %s", HAS_ARIA2, bool(IG_USERNAME))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
