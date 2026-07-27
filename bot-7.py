# -*- coding: utf-8 -*-
"""ربات دانلودر PRO — yt-dlp + instagrapi"""
import asyncio, json, logging, os, re, shutil, time, uuid
from pathlib import Path
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# instagrapi - optional, fail gracefully
try:
    from instagrapi import Client as InstaClient
    HAS_INSTA = True
except ImportError:
    HAS_INSTA = False

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {7438138322}
DOWNLOAD_DIR = Path("downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_FILE_MB = 49
HAS_ARIA2 = shutil.which("aria2c") is not None
IG_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "")
IG_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD", "")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("bot")
URL_RE = re.compile(r"https?://\S+")
INSTA_RE = re.compile(r"https?://(www\.)?(instagram\.com|instagr\.am)/\S+", re.I)
STATS = {"users": set(), "downloads": 0, "errors": 0, "started": time.time()}
PENDING = {}
BOT_ENABLED = True

# Instagram client
ig_client = None
if HAS_INSTA and IG_USERNAME and IG_PASSWORD:
    try:
        ig_client = InstaClient()
        ig_client.login(IG_USERNAME, IG_PASSWORD)
        log.info("Instagram login OK ✅")
    except Exception as e:
        log.error("Instagram login FAILED: %s", e)
        ig_client = None

def main_panel(uid):
    rows = [[InlineKeyboardButton("📥 راهنما", callback_data="help"), InlineKeyboardButton("📊 وضعیت", callback_data="status")]]
    if uid in ADMIN_IDS: rows.append([InlineKeyboardButton("👑 مدیریت", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def quality_panel(token):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏆 بهترین", callback_data=f"dl|best|{token}"), InlineKeyboardButton("🎬 1080p", callback_data=f"dl|1080|{token}")],
        [InlineKeyboardButton("⚡ 720p", callback_data=f"dl|720|{token}"), InlineKeyboardButton("📱 480p", callback_data=f"dl|480|{token}")],
        [InlineKeyboardButton("🎵 MP3", callback_data=f"dl|audio|{token}"), InlineKeyboardButton("❌ لغو", callback_data=f"cancel|{token}")]
    ])

def admin_panel():
    return InlineKeyboardMarkup([[InlineKeyboardButton("📈 آمار", callback_data="a_stats"), InlineKeyboardButton("🔴/🟢", callback_data="a_toggle")], [InlineKeyboardButton("🔙", callback_data="home")]])

def progress_bar(pct):
    return "▰" * int(pct/10) + "▱" * (10 - int(pct/10))

async def start(update, context):
    STATS["users"].add(update.effective_user.id)
    e = "aria2c 🚀" if HAS_ARIA2 else "yt-dlp ⚡"
    ig = " | IG ✅" if ig_client else ""
    await update.message.reply_text(f"سلام {update.effective_user.first_name} 👋\n\n🔥 <b>دانلودر PRO</b> | {e}{ig}\nلینک بفرست!", parse_mode=ParseMode.HTML, reply_markup=main_panel(update.effective_user.id))

async def handle_text(update, context):
    uid = update.effective_user.id
    STATS["users"].add(uid)
    text = update.message.text or ""
    chat_type = update.effective_chat.type
    if not BOT_ENABLED and uid not in ADMIN_IDS: return

    if chat_type in ("group", "supergroup"):
        m = URL_RE.search(text)
        if not m: return
        url = m.group(0)
        chat_id = update.effective_chat.id
        status = await context.bot.send_message(chat_id, "⏬ ...")
        if INSTA_RE.search(url) and ig_client:
            asyncio.create_task(dl_ig(status, context, url, chat_id, update.message.message_id))
        else:
            asyncio.create_task(dl_ytdlp(status, context, url, "best", chat_id, update.message.message_id))
        return

    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("❗ لینک بفرست.", reply_markup=main_panel(uid))
        return
    url = m.group(0)
    if INSTA_RE.search(url) and ig_client:
        status = await update.message.reply_text("⏬ اینستاگرام...")
        asyncio.create_task(dl_ig(status, context, url, update.effective_chat.id))
    else:
        token = uuid.uuid4().hex[:10]
        PENDING[token] = url
        await update.message.reply_text("🎯 کیفیت:", reply_markup=quality_panel(token))

async def dl_ig(status, context, url, chat_id, delete_msg_id=None):
    """Download Instagram via instagrapi"""
    if not ig_client:
        await se(status, "❌ Instagram login نیست")
        return
    loop = asyncio.get_running_loop()
    try:
        m = re.search(r"/(p|reel|tv)/([A-Za-z0-9_-]+)", url)
        if not m:
            await se(status, "❌ لینک نامعتبر"); return
        shortcode = m.group(2)

        def do_dl():
            post = ig_client.media_info(ig_client.media_pk_from_code(shortcode))
            return post

        post = await asyncio.wait_for(loop.run_in_executor(None, do_dl), timeout=30)

        if post.media_type == 2:  # video
            def dl_vid():
                path = ig_client.video_download(post.pk, folder=str(DOWNLOAD_DIR))
                return path
            path = await asyncio.wait_for(loop.run_in_executor(None, dl_vid), timeout=60)
            sz = path.stat().st_size / 1048576
            with open(path, "rb") as f:
                await context.bot.send_video(chat_id, f, caption=f"📦 {sz:.1f}MB", supports_streaming=True)
            STATS["downloads"] += 1
        elif post.media_type == 1:  # photo
            def dl_pic():
                path = ig_client.photo_download(post.pk, folder=str(DOWNLOAD_DIR))
                return path
            path = await asyncio.wait_for(loop.run_in_executor(None, dl_pic), timeout=30)
            with open(path, "rb") as f:
                await context.bot.send_photo(chat_id, f)
            STATS["downloads"] += 1
        else:
            await se(status, "❌ نوع پست پشتیبانی نمیشه"); return

        if delete_msg_id:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=delete_msg_id)
            except: pass
        await status.delete()

    except asyncio.TimeoutError:
        STATS["errors"] += 1
        await se(status, "❌ timeout")
    except Exception as e:
        STATS["errors"] += 1
        log.error("IG fail: %s", e)
        # Fallback to yt-dlp
        await dl_ytdlp(status, context, url, "best", chat_id, delete_msg_id)

async def dl_ytdlp(status, context, url, quality, chat_id, delete_msg_id=None):
    loop = asyncio.get_running_loop()
    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir(exist_ok=True)
    last_edit = {"t": 0.0}
    def hook(d):
        if d["status"] != "downloading": return
        now = time.time()
        if now - last_edit["t"] < 3: return
        last_edit["t"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        pct = done/total*100 if total else 0
        spd = (d.get("speed") or 0)/(1024*1024)
        asyncio.run_coroutine_threadsafe(se(status, f"⏬ {progress_bar(pct)} {pct:.0f}%\n🚀 {spd:.1f} MB/s"), loop)

    opts = {"outtmpl": str(folder/"%(title).60s.%(ext)s"), "noplaylist": True, "quiet": True, "no_warnings": True, "retries": 3, "socket_timeout": 15, "concurrent_fragment_downloads": 16, "max_filesize": MAX_FILE_MB*1024*1024, "progress_hooks": [hook]}
    if HAS_ARIA2:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = ["-x", "16", "-s", "16", "-k", "1M"]
    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    elif quality in ("1080","720","480"):
        opts["format"] = f"bv*[height<={quality}]+ba/b[height<={quality}]/best"
    else:
        opts["format"] = "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"

    try:
        await se(status, "🔍 دریافت...")
        def do_dl():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        info = await asyncio.wait_for(loop.run_in_executor(None, do_dl), timeout=120)
        files = [f for f in folder.iterdir() if f.suffix.lower() in (".mp4",".mkv",".webm",".mp3",".m4a",".opus",".ogg")]
        if not files:
            files = [f for f in folder.iterdir() if f.suffix.lower() not in (".jpg",".png",".webp")]
        if not files:
            await se(status, "❌ دانلود نشد."); return
        path = max(files, key=lambda f: f.stat().st_size)
        sz = path.stat().st_size/1048576
        if sz > MAX_FILE_MB:
            await se(status, f"❌ حجم {sz:.0f}MB زیاده."); return
        cap = f"📦 {sz:.1f}MB"
        await se(status, f"📤 ({sz:.1f}MB)...")
        with open(path, "rb") as f:
            if path.suffix in (".mp3",".m4a",".opus",".ogg"):
                await context.bot.send_audio(chat_id, f, caption=cap)
            elif path.suffix in (".mp4",".mkv",".webm"):
                await context.bot.send_video(chat_id, f, caption=cap, supports_streaming=True, duration=info.get("duration"), width=info.get("width"), height=info.get("height"))
            else:
                await context.bot.send_document(chat_id, f, caption=cap)
        STATS["downloads"] += 1
        if delete_msg_id:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=delete_msg_id)
            except: pass
        await status.delete()
    except asyncio.TimeoutError:
        STATS["errors"] += 1
        await se(status, "❌ timeout")
    except Exception as e:
        STATS["errors"] += 1
        log.error("DL fail: %s", e)
        await se(status, f"❌ {str(e)[:200]}", html=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)

async def callbacks(update, context):
    global BOT_ENABLED
    q = update.callback_query; await q.answer()
    data = q.data; uid = q.from_user.id
    if data == "home": await q.edit_message_text("🏠", reply_markup=main_panel(uid))
    elif data == "help": await q.edit_message_text("لینک بفرست! 🚀", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="home")]]))
    elif data == "status":
        e = "aria2c 🚀" if HAS_ARIA2 else "yt-dlp ⚡"
        ig = " | IG ✅" if ig_client else " | IG ❌"
        await q.edit_message_text(f"🟢 {e}{ig}\n📥 {STATS['downloads']}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="home")]]))
    elif data == "admin" and uid in ADMIN_IDS: await q.edit_message_text("👑", reply_markup=admin_panel())
    elif data == "a_stats" and uid in ADMIN_IDS:
        up = int(time.time()-STATS["started"])
        await q.edit_message_text(f"👥 {len(STATS['users'])} 📥 {STATS['downloads']} ⚠️ {STATS['errors']}\n⏱ {up//3600}h", reply_markup=admin_panel())
    elif data == "a_toggle" and uid in ADMIN_IDS:
        BOT_ENABLED = not BOT_ENABLED
        await q.edit_message_text("🟢" if BOT_ENABLED else "🔴", reply_markup=admin_panel())
    elif data.startswith("cancel|"): PENDING.pop(data.split("|")[1], None); await q.edit_message_text("❌", reply_markup=main_panel(uid))
    elif data.startswith("dl|"):
        _, quality, token = data.split("|"); url = PENDING.pop(token, None)
        if not url: await q.edit_message_text("⏰"); return
        sh = StatusHandle(context.bot, q.message.chat_id, q.message.message_id)
        if INSTA_RE.search(url) and ig_client:
            asyncio.create_task(dl_ig(sh, context, url, q.message.chat_id))
        else:
            asyncio.create_task(dl_ytdlp(sh, context, url, quality, q.message.chat_id))

class StatusHandle:
    def __init__(self, bot, cid, mid): self.bot=bot; self.cid=cid; self.mid=mid
    async def edit_message_text(self, text, parse_mode=None):
        try: await self.bot.edit_message_text(chat_id=self.cid, message_id=self.mid, text=text, parse_mode=parse_mode)
        except: pass
    async def delete(self):
        try: await self.bot.delete_message(chat_id=self.cid, message_id=self.mid)
        except: pass

async def se(q, text, html=False):
    try: await q.edit_message_text(text, parse_mode=ParseMode.HTML if html else None)
    except BadRequest: pass

def main():
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot running... aria2=%s ig=%s", HAS_ARIA2, ig_client is not None)
    app.run_polling(drop_pending_updates=True)
if __name__ == "__main__": main()
