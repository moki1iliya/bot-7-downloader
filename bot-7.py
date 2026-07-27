# -*- coding: utf-8 -*-
"""Downloader Bot — yt-dlp + instagrapi"""
import asyncio, json, logging, os, re, shutil, time, uuid
from pathlib import Path
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

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
COOKIES_FILE = Path("cookies.txt")
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
        log.info("Instagram login OK")
    except Exception as e:
        log.error("Instagram login FAILED: %s", e)
        ig_client = None


def main_panel(uid):
    rows = [[InlineKeyboardButton("Help", callback_data="help"), InlineKeyboardButton("Status", callback_data="status")]]
    if uid in ADMIN_IDS: rows.append([InlineKeyboardButton("Admin", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def quality_panel(token):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Best", callback_data=f"dl|best|{token}"), InlineKeyboardButton("1080p", callback_data=f"dl|1080|{token}")],
        [InlineKeyboardButton("720p", callback_data=f"dl|720|{token}"), InlineKeyboardButton("480p", callback_data=f"dl|480|{token}")],
        [InlineKeyboardButton("MP3", callback_data=f"dl|audio|{token}"), InlineKeyboardButton("Cancel", callback_data=f"cancel|{token}")]
    ])

def admin_panel():
    return InlineKeyboardMarkup([[InlineKeyboardButton("Stats", callback_data="a_stats"), InlineKeyboardButton("Toggle On/Off", callback_data="a_toggle")], [InlineKeyboardButton("Back", callback_data="home")]])

def progress_bar(pct):
    return "#" * int(pct/10) + "-" * (10 - int(pct/10))


# ---- Upload progress wrapper ----
class ProgressFile:
    """Wraps a file object; reports read progress back to the status message
    via a threadsafe callback, throttled to avoid Telegram edit rate limits."""
    def __init__(self, path, status, context, loop, label="Uploading", interval=1.5):
        self.f = open(path, "rb")
        self.size = path.stat().st_size
        self.read_bytes = 0
        self.status = status
        self.context = context
        self.loop = loop
        self.label = label
        self.interval = interval
        self.last_edit = 0.0
        self.start = time.time()

    def read(self, n=-1):
        chunk = self.f.read(n)
        self.read_bytes += len(chunk)
        self._maybe_report()
        return chunk

    def _maybe_report(self):
        now = time.time()
        if now - self.last_edit < self.interval:
            return
        self.last_edit = now
        pct = (self.read_bytes / self.size * 100) if self.size else 0
        elapsed = max(now - self.start, 0.001)
        spd = (self.read_bytes / 1048576) / elapsed
        text = f"{self.label} {progress_bar(pct)} {pct:.0f}%\n{spd:.1f} MB/s"
        asyncio.run_coroutine_threadsafe(se(self.status, text), self.loop)

    def __len__(self):
        return self.size

    def seekable(self):
        return self.f.seekable()

    def seek(self, *a, **kw):
        return self.f.seek(*a, **kw)

    def tell(self):
        return self.f.tell()

    def close(self):
        self.f.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


async def start(update, context):
    STATS["users"].add(update.effective_user.id)
    e = "aria2c" if HAS_ARIA2 else "yt-dlp"
    ig = " | Instagram: connected" if ig_client else ""
    await update.message.reply_text(
        f"Hi {update.effective_user.first_name}\n\n"
        f"Downloader Bot | Engine: {e}{ig}\n"
        f"Send a link to get started.",
        reply_markup=main_panel(update.effective_user.id))

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
        status = await context.bot.send_message(chat_id, "Fetching...")
        asyncio.create_task(dl_ytdlp(status, context, url, "best", chat_id, update.message.message_id))
        return

    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("Send a link.", reply_markup=main_panel(uid))
        return
    url = m.group(0)
    token = uuid.uuid4().hex[:10]
    PENDING[token] = url
    await update.message.reply_text("Choose quality:", reply_markup=quality_panel(token))

async def dl_ig(status, context, url, chat_id, delete_msg_id=None):
    """Download Instagram via instagrapi"""
    if not ig_client:
        await se(status, "Instagram login not configured")
        return
    loop = asyncio.get_running_loop()
    try:
        m = re.search(r"/(p|reel|tv)/([A-Za-z0-9_-]+)", url)
        if not m:
            await se(status, "Invalid link"); return
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
            await se(status, "Uploading 0%")
            with ProgressFile(path, status, context, loop, label="Uploading") as pf:
                await context.bot.send_video(chat_id, pf, caption=f"{sz:.1f} MB", supports_streaming=True)
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
            await se(status, "Unsupported post type"); return

        if delete_msg_id:
            try: await context.bot.delete_message(chat_id=chat_id, message_id=delete_msg_id)
            except: pass
        await status.delete()

    except asyncio.TimeoutError:
        STATS["errors"] += 1
        await se(status, "Timeout")
    except Exception as e:
        STATS["errors"] += 1
        log.error("IG fail: %s", e)
        await dl_ytdlp(status, context, url, "best", chat_id, delete_msg_id)

async def dl_ytdlp(status, context, url, quality, chat_id, delete_msg_id=None):
    loop = asyncio.get_running_loop()
    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir(exist_ok=True)
    last_edit = {"t": 0.0}
    def hook(d):
        if d["status"] != "downloading": return
        now = time.time()
        if now - last_edit["t"] < 2: return
        last_edit["t"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        pct = done/total*100 if total else 0
        spd = (d.get("speed") or 0)/(1024*1024)
        asyncio.run_coroutine_threadsafe(se(status, f"Downloading {progress_bar(pct)} {pct:.0f}%\n{spd:.1f} MB/s"), loop)

    opts = {
        "outtmpl": str(folder/"%(title).60s.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "retries": 3, "socket_timeout": 15,
        "concurrent_fragment_downloads": 1,
        "max_filesize": MAX_FILE_MB*1024*1024,
        "progress_hooks": [hook],
    }
    if INSTA_RE.search(url) and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    elif quality in ("1080","720","480"):
        opts["format"] = f"best[height<={quality}][ext=mp4]/best[height<={quality}]/best"
    else:
        opts["format"] = "best[ext=mp4]/best"

    try:
        await se(status, "Fetching...")
        def do_dl():
            log.info("Starting download: %s", url)
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.extract_info(url, download=True)
            log.info("Download complete: %s", url)
            return result
        info = await asyncio.wait_for(loop.run_in_executor(None, do_dl), timeout=120)
        files = [f for f in folder.iterdir() if f.suffix.lower() in (".mp4",".mkv",".webm",".mp3",".m4a",".opus",".ogg")]
        if not files:
            files = [f for f in folder.iterdir() if f.suffix.lower() not in (".jpg",".png",".webp")]
        if not files:
            await se(status, "Download failed."); return
        path = max(files, key=lambda f: f.stat().st_size)
        sz = path.stat().st_size/1048576
        if sz > MAX_FILE_MB:
            await se(status, f"File is {sz:.0f}MB, over the {MAX_FILE_MB}MB limit."); return
        cap = f"{sz:.1f} MB"
        await se(status, "Uploading...")
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
        await se(status, "Timeout")
    except Exception as e:
        STATS["errors"] += 1
        log.error("DL fail: %s", e)
        await se(status, f"Error: {str(e)[:200]}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)

async def callbacks(update, context):
    global BOT_ENABLED
    q = update.callback_query; await q.answer()
    data = q.data; uid = q.from_user.id
    if data == "home": await q.edit_message_text("Home", reply_markup=main_panel(uid))
    elif data == "help": await q.edit_message_text("Send a link to download.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="home")]]))
    elif data == "status":
        e = "aria2c" if HAS_ARIA2 else "yt-dlp"
        ig = " | Instagram: connected" if ig_client else " | Instagram: not connected"
        await q.edit_message_text(f"Online | Engine: {e}{ig}\nDownloads: {STATS['downloads']}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="home")]]))
    elif data == "admin" and uid in ADMIN_IDS: await q.edit_message_text("Admin panel", reply_markup=admin_panel())
    elif data == "a_stats" and uid in ADMIN_IDS:
        up = int(time.time()-STATS["started"])
        await q.edit_message_text(f"Users: {len(STATS['users'])} | Downloads: {STATS['downloads']} | Errors: {STATS['errors']}\nUptime: {up//3600}h", reply_markup=admin_panel())
    elif data == "a_toggle" and uid in ADMIN_IDS:
        BOT_ENABLED = not BOT_ENABLED
        await q.edit_message_text("Bot enabled" if BOT_ENABLED else "Bot disabled", reply_markup=admin_panel())
    elif data.startswith("cancel|"): PENDING.pop(data.split("|")[1], None); await q.edit_message_text("Cancelled", reply_markup=main_panel(uid))
    elif data.startswith("dl|"):
        _, quality, token = data.split("|"); url = PENDING.pop(token, None)
        if not url: await q.edit_message_text("Expired, send the link again."); return
        sh = StatusHandle(context.bot, q.message.chat_id, q.message.message_id)
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
    # Tuned HTTPX client: default timeouts (5s connect/read/write, 1s pool)
    # are far too tight for multi-MB uploads on a constrained egress link,
    # causing silent retries that make uploads look "stuck".
    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=15.0,
        read_timeout=60.0,
        write_timeout=120.0,
        pool_timeout=15.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot running... aria2=%s ig=%s", HAS_ARIA2, ig_client is not None)
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__": main()
