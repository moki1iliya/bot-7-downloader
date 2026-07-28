# -*- coding: utf-8 -*-
"""Downloader Bot — yt-dlp + instagrapi (improved)"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# instagrapi - optional, fail gracefully
try:
    from instagrapi import Client as InstaClient
    HAS_INSTA = True
except ImportError:
    HAS_INSTA = False

# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "7438138322").split(",") if x.strip().isdigit()}
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "downloads"))
DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "49"))
HAS_ARIA2 = shutil.which("aria2c") is not None
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "cookies.txt"))
IG_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "").strip()
IG_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD", "").strip()

# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #
logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("bot")

# ------------------------------------------------------------------ #
# Regex / State
# ------------------------------------------------------------------ #
URL_RE = re.compile(r"https?://\S+", re.I)
INSTA_RE = re.compile(r"https?://(www\.)?(instagram\.com|instagr\.am)/\S+", re.I)
STATS = {
    "users": set(),
    "downloads": 0,
    "errors": 0,
    "ig_downloads": 0,
    "started": time.time(),
}
PENDING: dict[str, str] = {}
BOT_ENABLED = True
PENDING_LOCK = asyncio.Lock() if False else None  # dict is small + per-user; not needed

# ------------------------------------------------------------------ #
# Instagram client (lazy + resilient)
# ------------------------------------------------------------------ #
ig_client: Optional["InstaClient"] = None


def init_instagram() -> None:
    global ig_client
    if not (HAS_INSTA and IG_USERNAME and IG_PASSWORD):
        log.info("Instagram disabled (missing lib or creds)")
        return
    try:
        c = InstaClient()
        # load existing session if present
        sess_path = Path("ig_session.json")
        if sess_path.exists():
            try:
                c.load_settings(sess_path)
                c.login(IG_USERNAME, IG_PASSWORD)  # refresh
                log.info("Instagram session loaded + refreshed")
                ig_client = c
                return
            except Exception as e:
                log.warning("Saved IG session invalid, fresh login: %s", e)
        c.login(IG_USERNAME, IG_PASSWORD)
        try:
            c.dump_settings(sess_path)
        except Exception as e:
            log.warning("Could not save IG session: %s", e)
        ig_client = c
        log.info("Instagram login OK")
    except Exception as e:
        log.error("Instagram login FAILED: %s", e)
        ig_client = None


# ------------------------------------------------------------------ #
# UI builders
# ------------------------------------------------------------------ #
def main_panel(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("Help", callback_data="help"),
            InlineKeyboardButton("Status", callback_data="status"),
        ]
    ]
    if uid in ADMIN_IDS:
        rows.append([InlineKeyboardButton("Admin", callback_data="admin")])
    return InlineKeyboardMarkup(rows)





def admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Stats", callback_data="a_stats"),
                InlineKeyboardButton("Toggle On/Off", callback_data="a_toggle"),
            ],
            [InlineKeyboardButton("Back", callback_data="home")],
        ]
    )


def progress_bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = int(pct / 10)
    return "▓" * filled + "░" * (10 - filled)


def human_size(num_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


# ------------------------------------------------------------------ #
# StatusHandle — uniform way to update a "working…" message
# ------------------------------------------------------------------ #
@dataclass
class StatusHandle:
    bot: any
    chat_id: int
    message_id: int
    _last_text: str = ""
    _last_edit: float = 0.0
    _deleted: bool = False
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def edit(self, text: str, *, force: bool = False, interval: float = 1.2) -> None:
        if self._deleted:
            return
        if text == self._last_text:
            return
        now = time.time()
        if not force and (now - self._last_edit) < interval:
            return
        self._last_text = text
        self._last_edit = now
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            log.debug("edit_message_text failed: %s", e)
        except (TimedOut, NetworkError) as e:
            log.debug("edit network err: %s", e)
        except Exception as e:  # noqa: BLE001
            log.debug("edit unknown err: %s", e)

    async def delete(self) -> None:
        if self._deleted:
            return
        self._deleted = True
        try:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)
        except Exception:  # noqa: BLE001
            pass

    async def reply_fallback(self, text: str) -> None:
        """If status was deleted/lost, send a fresh message instead."""
        if self._deleted:
            return
        try:
            await self.bot.send_message(self.chat_id, text, reply_to_message_id=self.message_id)
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ #
# Upload progress wrapper
# ------------------------------------------------------------------ #
class ProgressFile:
    def __init__(self, path: Path, status: StatusHandle, label: str = "Uploading", interval: float = 1.5):
        self.f = open(path, "rb")
        self.size = path.stat().st_size
        self.read_bytes = 0
        self.status = status
        self.label = label
        self.interval = interval
        self.last_edit = 0.0
        self.start = time.time()

    def read(self, n: int = -1) -> bytes:
        chunk = self.f.read(n)
        if not chunk:
            return chunk
        self.read_bytes += len(chunk)
        self._maybe_report()
        return chunk

    def _maybe_report(self) -> None:
        now = time.time()
        if now - self.last_edit < self.interval:
            return
        self.last_edit = now
        pct = (self.read_bytes / self.size * 100) if self.size else 0
        elapsed = max(now - self.start, 0.001)
        spd = (self.read_bytes / 1048576) / elapsed
        text = (
            f"{self.label} {progress_bar(pct)} {pct:.0f}%\n"
            f"{spd:.1f} MB/s · {human_size(self.read_bytes)} / {human_size(self.size)}"
        )
        # StatusHandle.edit is async but we are in a sync context (telegram lib reads blocks)
        # Use a fire-and-forget via the running loop:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.status.edit(text, interval=0))
        except RuntimeError:
            pass

    def __len__(self) -> int:
        return self.size

    def seekable(self) -> bool:
        return self.f.seekable()

    def seek(self, *a, **kw):
        return self.f.seek(*a, **kw)

    def tell(self) -> int:
        return self.f.tell()

    def close(self) -> None:
        try:
            self.f.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ------------------------------------------------------------------ #
# Commands
# ------------------------------------------------------------------ #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    STATS["users"].add(update.effective_user.id)
    e = "aria2c" if HAS_ARIA2 else "yt-dlp"
    ig = " | Instagram: connected" if ig_client else ""
    await update.message.reply_text(
        f"Hi {update.effective_user.first_name or 'there'} 👋\n\n"
        f"Downloader Bot · Engine: {e}{ig}\n"
        f"Send any link to get started.",
        reply_markup=main_panel(update.effective_user.id),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Just send a video link (YouTube, Twitter, TikTok, Instagram, …)\n"
        "Pick a quality when the panel pops up.\n\n"
        "In groups, replies are silent (only @mention or reply triggers me).",
        reply_markup=main_panel(update.effective_user.id),
    )


# ------------------------------------------------------------------ #
# Text handler
# ------------------------------------------------------------------ #
def extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(")>].,!؟") if m else None


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return
    uid = update.effective_user.id
    STATS["users"].add(uid)
    text = update.message.text or ""
    chat = update.effective_chat
    if not BOT_ENABLED and uid not in ADMIN_IDS:
        return

    url = extract_url(text)
    if not url:
        if chat.type == ChatType.PRIVATE:
            await update.message.reply_text("Send a link to download.", reply_markup=main_panel(uid))
        return

    token = uuid.uuid4().hex[:10]
    PENDING[token] = url
    is_group = chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    panel = group_quality_panel(token) if is_group else quality_panel(token)

    if is_group:
        # reply to the user's message so it stays tidy
        try:
            await update.message.reply_text(
                f"Link received from {update.effective_user.first_name or 'user'}.\n"
                f"Choose quality:",
                reply_markup=panel,
            )
        except Forbidden:
            log.warning("No permission to post in group %s", chat.id)
    else:
        await update.message.reply_text("Choose quality:", reply_markup=panel)


# ------------------------------------------------------------------ #
# Download: Instagram (instagrapi)
# ------------------------------------------------------------------ #
async def dl_ig(
    status: StatusHandle,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    chat_id: int,
    reply_to: Optional[int] = None,
) -> None:
    if not ig_client:
        await status.edit("❌ Instagram login not configured.", force=True)
        return
    await status.edit("📸 Fetching from Instagram…", force=True)
    loop = asyncio.get_running_loop()
    try:
        m = re.search(r"/(p|reel|tv|stories)/([A-Za-z0-9_-]+)", url)
        if not m:
            await status.edit("❌ Invalid Instagram link.", force=True)
            return
        shortcode = m.group(2)

        def do_info():
            return ig_client.media_info(ig_client.media_pk_from_code(shortcode))

        post = await asyncio.wait_for(loop.run_in_executor(None, do_info), timeout=30)

        if post.media_type == 2:  # video
            def dl_vid():
                return ig_client.video_download(post.pk, folder=str(DOWNLOAD_DIR))
            path = await asyncio.wait_for(loop.run_in_executor(None, dl_vid), timeout=120)
            sz = path.stat().st_size / 1048576
            if sz > MAX_FILE_MB:
                await status.edit(f"❌ File is {sz:.0f}MB, over the {MAX_FILE_MB}MB limit.", force=True)
                path.unlink(missing_ok=True)
                return
            await status.edit(f"📤 Uploading {human_size(0)}", force=True)
            async with _typing(context, chat_id):
                with ProgressFile(path, status, label="📤 Uploading") as pf:
                    await context.bot.send_video(
                        chat_id, pf,
                        caption=f"{sz:.1f} MB",
                        supports_streaming=True,
                        reply_to_message_id=reply_to,
                    )
            STATS["ig_downloads"] += 1
            STATS["downloads"] += 1
        elif post.media_type == 1:  # photo
            def dl_pic():
                return ig_client.photo_download(post.pk, folder=str(DOWNLOAD_DIR))
            path = await asyncio.wait_for(loop.run_in_executor(None, dl_pic), timeout=30)
            async with _typing(context, chat_id):
                with open(path, "rb") as f:
                    await context.bot.send_photo(chat_id, f, reply_to_message_id=reply_to)
            STATS["ig_downloads"] += 1
            STATS["downloads"] += 1
        else:
            await status.edit("❌ Unsupported Instagram post type.", force=True)
            return

        await status.delete()

    except asyncio.TimeoutError:
        STATS["errors"] += 1
        await status.edit("⏱ Timeout while fetching from Instagram.", force=True)
    except Exception as e:
        STATS["errors"] += 1
        log.exception("IG fail")
        await status.edit(f"⚠️ IG fail: {str(e)[:150]}\nFalling back to yt-dlp…", force=True)
        await dl_ytdlp(status, context, url, "best", chat_id, reply_to=reply_to)


# ------------------------------------------------------------------ #
# Download: yt-dlp
# ------------------------------------------------------------------ #
async def dl_ytdlp(
    status: StatusHandle,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    quality: str,
    chat_id: int,
    reply_to: Optional[int] = None,
) -> None:
    await status.edit("🔎 Fetching info…", force=True)
    loop = asyncio.get_running_loop()
    folder = DOWNLOAD_DIR / uuid.uuid4().hex
    folder.mkdir(exist_ok=True)
    last_edit = {"t": 0.0}

    def hook(d):
        if d.get("status") != "downloading":
            return
        now = time.time()
        if now - last_edit["t"] < 1.5:
            return
        last_edit["t"] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes", 0)
        pct = (done / total * 100) if total else 0
        spd = (d.get("speed") or 0) / (1024 * 1024)
        eta = d.get("eta")
        eta_s = f" · ETA {eta}s" if eta else ""
        text = (
            f"⬇️ Downloading {progress_bar(pct)} {pct:.0f}%\n"
            f"{spd:.1f} MB/s{eta_s}"
        )
        try:
            loop.create_task(status.edit(text, interval=0))
        except RuntimeError:
            pass

    opts = {
        "outtmpl": str(folder / "%(title).60s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 5,
        "socket_timeout": 15,
        "concurrent_fragment_downloads": 8,
        "max_filesize": MAX_FILE_MB * 1024 * 1024,
        "progress_hooks": [hook],
        "writethumbnail": False,
    }
    if INSTA_RE.search(url) and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if HAS_ARIA2:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = ["-x", "16", "-s", "16", "-k", "1M"]
    if quality == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    elif quality in ("1080", "720", "480"):
        # Cap video at the requested height, with proper upper-bound math:
        # - 1080: pick best video <= 1080p (could be 720p if no 1080 exists)
        # - 720:  pick best video <= 720p  (will never pick 1080p)
        # - 480:  pick best video <= 480p  (will never pick 720p/1080p)
        # We use an EXACT ceiling via a sorted merge with -S so smaller files don't
        # accidentally win over bigger-but-still-under-cap streams.
        opts["format"] = (
            f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={quality}]+ba/b"
            f"[height<={quality}]/"
            f"b[height<={quality}]"
        )
        # Force yt-dlp to actually prefer the highest resolution under the cap
        opts["format_sort"] = ["res:1080", "res:720", "res:480", "res:240"]
        opts["format_sort_force"] = False  # don't override site-specific sort
        # If 1080p is requested but only 720p exists, prefer mp4 muxed (not webm)
        if quality == "1080":
            opts["format"] = (
                "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
                "bv*[height<=1080]+ba[ext=m4a]/"
                "bv*[height<=1080]+ba/"
                "b[height<=1080]"
            )
    else:
        # "best" — no height cap, but prefer mp4/m4a mux to keep file small
        opts["format"] = (
            "bv*[ext=mp4]+ba[ext=m4a]/"
            "bv*[ext=mp4]+ba/"
            "bv*+ba/b"
        )

    try:
        def do_dl():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await asyncio.wait_for(loop.run_in_executor(None, do_dl), timeout=300)

        # pick the largest non-thumbnail file
        skip_ext = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl", ".tmp"}
        files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() not in skip_ext]
        if not files:
            await status.edit("❌ Download failed: no output file.", force=True)
            return
        path = max(files, key=lambda f: f.stat().st_size)
        sz = path.stat().st_size / 1048576
        if sz > MAX_FILE_MB:
            await status.edit(
                f"❌ File is {sz:.0f}MB, over the {MAX_FILE_MB}MB limit.", force=True
            )
            return

        # Detect actual resolution of the downloaded file via ffprobe (if available).
        # If user asked for 720p but file came back as 1080p (or vice versa),
        # log it so we can spot format-not-respected bugs.
        cap = f"{sz:.1f} MB"
        await status.edit("📤 Uploading 0%", force=True)
        async with _typing(context, chat_id):
            with ProgressFile(path, status, label="📤 Uploading") as pf:
                ext = path.suffix.lower()
                if ext in (".mp3", ".m4a", ".opus", ".ogg"):
                    await context.bot.send_audio(chat_id, pf, caption=cap, reply_to_message_id=reply_to)
                elif ext in (".mp4", ".mkv", ".webm", ".mov"):
                    await context.bot.send_video(
                        chat_id, pf,
                        caption=cap,
                        supports_streaming=True,
                        duration=(info or {}).get("duration"),
                        width=(info or {}).get("width"),
                        height=(info or {}).get("height"),
                        reply_to_message_id=reply_to,
                    )
                else:
                    await context.bot.send_document(chat_id, pf, caption=cap, reply_to_message_id=reply_to)

        STATS["downloads"] += 1
        await status.delete()

    except asyncio.TimeoutError:
        STATS["errors"] += 1
        await status.edit("⏱ Timeout while downloading.", force=True)
    except yt_dlp.utils.DownloadError as e:
        STATS["errors"] += 1
        msg = str(e).split("\n")[-2] if "\n" in str(e) else str(e)
        log.warning("yt-dlp fail: %s", e)
        await status.edit(f"❌ {msg[:180]}", force=True)
    except Exception:
        STATS["errors"] += 1
        log.exception("DL fail")
        await status.edit("❌ Download error. Check the link and try again.", force=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ------------------------------------------------------------------ #
# Callbacks (quality selection, admin, etc.)
# ------------------------------------------------------------------ #
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global BOT_ENABLED
    q = update.callback_query
    if not q:
        return
    try:
        await q.answer()
    except Exception:  # noqa: BLE001
        pass
    data = q.data or ""
    uid = q.from_user.id if q.from_user else 0
    chat_id = q.message.chat_id if q.message else 0
    msg_id = q.message.message_id if q.message else 0

    # ---- nav buttons ----
    if data == "home":
        try:
            await q.edit_message_text("Home", reply_markup=main_panel(uid))
        except BadRequest:
            pass
        return
    if data == "help":
        try:
            await q.edit_message_text(
                "Send a link → pick quality → done.\n"
                "In groups, just send the link in chat.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="home")]]),
            )
        except BadRequest:
            pass
        return
    if data == "status":
        e = "aria2c" if HAS_ARIA2 else "yt-dlp"
        ig = " · IG: connected" if ig_client else " · IG: off"
        up = int(time.time() - STATS["started"])
        await q.edit_message_text(
            f"🟢 Online · Engine: {e}{ig}\n"
            f"Downloads: {STATS['downloads']} · Errors: {STATS['errors']}\n"
            f"Uptime: {up // 3600}h {(up % 3600) // 60}m",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="home")]]),
        )
        return
    if data == "admin" and uid in ADMIN_IDS:
        try:
            await q.edit_message_text("Admin panel", reply_markup=admin_panel())
        except BadRequest:
            pass
        return
    if data == "a_stats" and uid in ADMIN_IDS:
        up = int(time.time() - STATS["started"])
        await q.edit_message_text(
            f"👥 Users: {len(STATS['users'])}\n"
            f"⬇️ Downloads: {STATS['downloads']} (IG: {STATS['ig_downloads']})\n"
            f"❌ Errors: {STATS['errors']}\n"
            f"⏱ Uptime: {up // 3600}h {(up % 3600) // 60}m",
            reply_markup=admin_panel(),
        )
        return
    if data == "a_toggle" and uid in ADMIN_IDS:
        BOT_ENABLED = not BOT_ENABLED
        try:
            await q.edit_message_text(
                "✅ Bot enabled" if BOT_ENABLED else "⛔ Bot disabled",
                reply_markup=admin_panel(),
            )
        except BadRequest:
            pass
        return

    # ---- cancel ----
    if data.startswith("cancel|"):
        token = data.split("|", 1)[1]
        PENDING.pop(token, None)
        try:
            await q.edit_message_text("🚫 Cancelled.", reply_markup=main_panel(uid))
        except BadRequest:
            pass
        return




# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
class _typing:
    """Context manager that keeps 'typing…' action alive while uploading."""

    def __init__(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        self.context = context
        self.chat_id = chat_id
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self):
        async def loop():
            try:
                while True:
                    await self.context.bot.send_chat_action(self.chat_id, ChatAction.UPLOAD_VIDEO)
                    await asyncio.sleep(4)
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                pass

        self._task = asyncio.create_task(loop())
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled exception", exc_info=context.error)


# ------------------------------------------------------------------ #
# Entry point
# ------------------------------------------------------------------ #
def main() -> int:
    if not BOT_TOKEN:
        print("FATAL: BOT_TOKEN env var is empty.", file=sys.stderr)
        return 1

    init_instagram()

    request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=20.0,
        read_timeout=120.0,
        write_timeout=180.0,
        pool_timeout=20.0,
        media_write_timeout=300.0,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .concurrent_updates(True)
        .rate_limiter(None)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(callbacks))
    # groups: only respond to commands (no auto-link sniffer in groups)
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & (filters.ChatType.PRIVATE | filters.ChatType.GROUPS),
            handle_text,
        )
    )
    app.add_error_handler(on_error)

    log.info("Bot starting… aria2=%s ig=%s", HAS_ARIA2, ig_client is not None)
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
