# -*- coding: utf-8 -*-
"""🔥 ربات دانلودر PRO v3 — Instagram via instaloader"""
import asyncio, json, logging, os, re, shutil, time, uuid
from pathlib import Path
import yt_dlp, instaloader
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {7438138322}
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)
CACHE_FILE = Path("cache.json")
MAX_FILE_MB = 49
INSTA_USERNAME = os.environ.get("INSTA_USERNAME", "")
INSTA_PASSWORD = os.environ.get("INSTA_PASSWORD", "")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("dlbot")
URL_RE = re.compile(r"https?://\S+")
INSTA_RE = re.compile(r"https?://(www\.)?(instagram\.com|instagr\.am)/\S+", re.I)
YT_RE = re.compile(r"https?://(www\.)?(youtu\.be|youtube\.com|m\.youtube\.com)/\S+", re.I)
STATS = {"users": set(), "downloads": 0, "errors": 0, "started": time.time()}
PENDING = {}
BOT_ENABLED = True
CACHE = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

L = instaloader.Instaloader(download_videos=True, download_video_thumbnails=False, download_geotags=False, download_comments=False, save_metadata=False, compress_json=False, dirname_pattern=str(DOWNLOAD_DIR/"insta"), filename_pattern="{date_utc:%Y%m%d_%H%M%S}_{shortcode}")
if INSTA_USERNAME and INSTA_PASSWORD:
    try:
        L.login(INSTA_USERNAME, INSTA_PASSWORD)
        log.info("Instagram login OK")
    except Exception as e:
        log.error("Instagram login failed: %s", e)

def save_cache(): CACHE_FILE.write_text(json.dumps(CACHE, ensure_ascii=False))
def main_panel(uid):
    rows = [[InlineKeyboardButton("📥 راهنما", callback_data="help"), InlineKeyboardButton("🌐 سایت‌ها", callback_data="sites")], [InlineKeyboardButton("📊 وضعیت", callback_data="status")]]
    if uid in ADMIN_IDS: rows.append([InlineKeyboardButton("👑 مدیریت", callback_data="admin")])
    return InlineKeyboardMarkup(rows)
def quality_panel(token):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏆 بهترین", callback_data=f"dl|best|{token}"), InlineKeyboardButton("🎬 1080p", callback_data=f"dl|1080|{token}")], [InlineKeyboardButton("⚡ 720p", callback_data=f"dl|720|{token}"), InlineKeyboardButton("📱 480p", callback_data=f"dl|480|{token}")], [InlineKeyboardButton("🎵 MP3", callback_data=f"dl|audio|{token}")], [InlineKeyboardButton("❌ لغو", callback_data=f"cancel|{token}")]])
def admin_panel(): return InlineKeyboardMarkup([[InlineKeyboardButton("📈 آمار", callback_data="a_stats"), InlineKeyboardButton("🔴 خاموش" if BOT_ENABLED else "🟢 روشن", callback_data="a_toggle")], [InlineKeyboardButton("🔙 بازگشت", callback_data="home")]])
def back_panel(): return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="home")]])

async def start(update, context):
    STATS["users"].add(update.effective_user.id)
    await update.message.reply_text(f"سلام {update.effective_user.first_name} 👋\n\n🔥 <b>ربات دانلودر PRO v3</b>\nلینک بفرست! 🚀", parse_mode=ParseMode.HTML, reply_markup=main_panel(update.effective_user.id))

async def handle_text(update, context):
    uid = update.effective_user.id
    STATS["users"].add(uid)
    text = update.message.text or ""
    chat_type = update.effective_chat.type
    if not BOT_ENABLED and uid not in ADMIN_IDS: return

    if chat_type in ("group", "supergroup"):
        insta_m = INSTA_RE.search(text)
        if insta_m:
            url = insta_m.group(0)
            chat_id = update.effective_chat.id
            status = await context.bot.send_message(chat_id, "⏬ دانلود اینستاگرام...")
            asyncio.create_task(download_instagram(status, context, url, chat_id, update.message.message_id))
            return
        yt_m = YT_RE.search(text)
        if yt_m:
            url = yt_m.group(0)
            chat_id = update.effective_chat.id
            status = await context.bot.send_message(chat_id, "⏬ دانلود...")
            asyncio.create_task(download_ytdlp(status, context, url, "best", chat_id, update.message.message_id))
            return
        return

    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("❗ لینک بفرست.", reply_markup=main_panel(uid))
        return
    url = m.group(0)
    if INSTA_RE.search(url):
        status = await update.message.reply_text("⏬ دانلود اینستاگرام...")
        asyncio.create_task(download_instagram(status, context, url, update.effective_chat.id))
    else:
        token = uuid.uuid4().hex[:10]
        PENDING[token] = url
        await update.message.reply_text("🎯 کیفیت:", reply_markup=quality_panel(token))

async def download_instagram(status, context, url, chat_id, delete_msg_id=None):
    loop = asyncio.get_running_loop()
    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir(exist_ok=True)
    try:
        m = re.search(r"/(p|reel|tv)/([A-Za-z0-9_-]+)", url)
        if not m:
            await safe_edit(status, "❌ لینک نامعتبره."); return
        shortcode = m.group(2)
        def do_download():
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            L.download_post(post, target=str(folder))
            media = [f for f in folder.rglob("*") if f.suffix.lower() in (".mp4",".jpg",".jpeg",".png",".webp")]
            return media, post
        await safe_edit(status, "🔍 دریافت پست...")
        media_files, post = await loop.run_in_executor(None, do_download)
        if not media_files:
            await safe_edit(status, "❌ فایلی دانلود نشد."); return
        caption = f"🎬 @{post.owner_username} | ❤️ {post.likes}"
        for f in media_files:
            if f.stat().st_size / 1048576 > MAX_FILE_MB: continue
            with open(f, "rb") as fh:
                if f.suffix.lower() == ".mp4":
                    await context.bot.send_video(chat_id, fh, caption=caption, supports_streaming=True)
                else:
                    await context.bot.send_photo(chat_id, fh, caption=caption)
            STATS["downloads"] += 1
        if delete_msg_id:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=delete_msg_id)
            except: pass
        await status.delete()
    except Exception as e:
        STATS["errors"] += 1
        log.error("IG failed: %s", e)
        await safe_edit(status, f"❌ خطا:\n<code>{str(e)[:200]}</code>", html=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

async def download_ytdlp(status, context, url, quality, chat_id, delete_msg_id=None):
    loop = asyncio.get_running_loop()
    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir(exist_ok=True)
    last_edit = {"t": 0.0}
    def hook(d):
        if d["status"] != "downloading": return
        now = time.time()
        if now - last_edit["t"] < 2.5: return
        last_edit["t"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        pct = done / total * 100 if total else 0
        speed = (d.get("speed") or 0) / (1024*1024)
        asyncio.run_coroutine_threadsafe(safe_edit(status, f"⏬ {pct:.0f}% | 🚀 {speed:.1f} MB/s"), loop)
    opts = {"outtmpl": str(folder/"%(title).60s.%(ext)s"), "noplaylist": True, "quiet": True, "no_warnings": True, "retries": 5, "socket_timeout": 20, "concurrent_fragment_downloads": 16, "max_filesize": MAX_FILE_MB*1024*1024, "progress_hooks": [hook]}
    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    elif quality in ("1080","720","480"):
        opts["format"] = f"bv*[height<={quality}]+ba/b[height<={quality}]/best"
    else:
        opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
    try:
        if status: await safe_edit(status, "🔍 دریافت...")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=True))
        files = [f for f in folder.iterdir() if f.suffix.lower() in (".mp4",".mkv",".webm",".mp3",".m4a")]
        if not files:
            if status: await safe_edit(status, "❌ دانلود نشد."); return
        path = max(files, key=lambda f: f.stat().st_size)
        size_mb = path.stat().st_size / 1048576
        caption = f"🎬 {info.get('title','')[:80]}\n📦 {size_mb:.1f}MB"
        with open(path, "rb") as f:
            if path.suffix in (".mp3",".m4a"): await context.bot.send_audio(chat_id, f, caption=caption)
            else: await context.bot.send_video(chat_id, f, caption=caption, supports_streaming=True)
        STATS["downloads"] += 1
        if delete_msg_id:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=delete_msg_id)
            except: pass
        if status: await status.delete()
    except Exception as e:
        STATS["errors"] += 1
        if status: await safe_edit(status, f"❌ خطا:\n<code>{str(e)[:200]}</code>", html=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

async def callbacks(update, context):
    global BOT_ENABLED
    q = update.callback_query; await q.answer()
    data = q.data; uid = q.from_user.id
    if data == "home": await q.edit_message_text("🏠 منوی اصلی:", reply_markup=main_panel(uid))
    elif data == "help": await q.edit_message_text("📥 <b>راهنما</b>\n\nلینک بفرست!\nگروه: خودکار ⚡\nخصوصی: کیفیت انتخاب کن", parse_mode=ParseMode.HTML, reply_markup=back_panel())
    elif data == "sites": await q.edit_message_text("🌐 <b>سایت‌ها</b>\n✅ اینستاگرام\n✅ یوتیوب\n✅ توییتر\n✅ تیک‌تاک", parse_mode=ParseMode.HTML, reply_markup=back_panel())
    elif data == "status": await q.edit_message_text(f"📊 <b>وضعیت</b>\n🟢 آنلاین\n📥 {STATS['downloads']} دانلود", parse_mode=ParseMode.HTML, reply_markup=back_panel())
    elif data == "admin" and uid in ADMIN_IDS: await q.edit_message_text("👑 <b>مدیریت</b>", parse_mode=ParseMode.HTML, reply_markup=admin_panel())
    elif data == "a_stats" and uid in ADMIN_IDS:
        up = int(time.time() - STATS["started"])
        await q.edit_message_text(f"📈\n👥 {len(STATS['users'])}\n📥 {STATS['downloads']}\n⚠️ {STATS['errors']}\n⏱ {up//3600}h", parse_mode=ParseMode.HTML, reply_markup=admin_panel())
    elif data == "a_toggle" and uid in ADMIN_IDS:
        BOT_ENABLED = not BOT_ENABLED
        await q.edit_message_text(f"{'🟢' if BOT_ENABLED else '🔴'}", reply_markup=admin_panel())
    elif data.startswith("cancel|"): PENDING.pop(data.split("|")[1], None); await q.edit_message_text("❌ لغو.", reply_markup=main_panel(uid))
    elif data.startswith("dl|"):
        _, quality, token = data.split("|"); url = PENDING.pop(token, None)
        if not url: await q.edit_message_text("⏰ منقضی."); return
        status = StatusHandle(context.bot, q.message.chat_id, q.message.message_id)
        if INSTA_RE.search(url): asyncio.create_task(download_instagram(status, context, url, q.message.chat_id))
        else: asyncio.create_task(download_ytdlp(status, context, url, quality, q.message.chat_id))

class StatusHandle:
    def __init__(self, bot, chat_id, message_id): self.bot=bot; self.chat_id=chat_id; self.message_id=message_id
    async def edit_message_text(self, text, parse_mode=None): await self.bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id, text=text, parse_mode=parse_mode)
    async def delete(self):
        try: await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)
        except: pass

async def safe_edit(q, text, html=False):
    try: await q.edit_message_text(text, parse_mode=ParseMode.HTML if html else None)
    except BadRequest: pass

def main():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot v3 running..."); app.run_polling(drop_pending_updates=True)
if __name__ == "__main__": main()
