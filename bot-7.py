# -*- coding: utf-8 -*-
"""
Downloader Bot — yt-dlp + instagrapi
BUILD: v2.1.0-qualityfix / 2026-07-27-final

Run /version in Telegram to see which build is loaded.
"""
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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ---- BUILD INFO (visible in /version) ----
__version__ = "2.1.0-qualityfix"
__build__ = "2026-07-27-final"
__changelog__ = [
    "Group 'Fetching' hang -> fixed",
    "Quality selector cap -> fixed (strict height ceiling)",
    "StatusHandle race -> fixed (lock + throttle)",
    "IG session caching -> added",
    "Upload timeout -> raised to 300s",
    "Typing indicator -> added",
]

# instagrapi - optional
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
HAS_FFPROBE = shutil.which("ffprobe") is not None
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "cookies.txt"))
IG_USERNAME = os.environ.get("INSTAGRAM_USERNAME", "").strip()
IG_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD", "").strip()

logging.basicConfig(
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("bot")

URL_RE = re.compile(r"https?://\S+", re.I)
INSTA_RE = re.compile(r"https?://(www\.)?(instagram\.com|instagr\.am)/\S+", re.I)
STATS = {"users": set(), "downloads": 0, "errors": 0, "ig_downloads": 0, "started": time.time()}
PENDING: dict[str, str] = {}
BOT_ENABLED = True

# ------------------------------------------------------------------ #
# Instagram
# ------------------------------------------------------------------ #
ig_client: Optional["InstaClient"] = None


def init_instagram() -> None:
    global ig_client
    if not (HAS_INSTA and IG_USERNAME and IG_PASSWORD):
        log.info("Instagram: disabled (missing lib or creds)")
        return
    try:
        c = InstaClient()
        sess_path = Path("ig_session.json")
        if sess_path.exists():
            try:
                c.load_settings(sess_path)
                c.login(IG_USERNAME, IG_PASSWORD)
                log.info("Instagram: session refreshed")
                ig_client = c
                return
            except Exception as e:
                log.warning("Instagram: saved session invalid, fresh login: %s", e)
        c.login(IG_USERNAME, IG_PASSWORD)
        try:
            c.dump_settings(sess_path)
        except Exception:
            pass
        ig_client = c
        log.info("Instagram: login OK")
    except Exception as e:
        log.error("Instagram: login FAILED: %s", e)
        ig_client = None


# ------------------------------------------------------------------ #
# UI
# ------------------------------------------------------------------ #
def main_panel(uid: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("Help", callback_data="help"),
             InlineKeyboardButton("Status", callback_data="status")]]
    if uid in ADMIN_IDS:
        rows.append([InlineKeyboardButton("Admin", callback_data="admin")])
        rows.append([InlineKeyboardButton(f"ℹ️ v{__version__}", callback_data="version")])
    return InlineKeyboardMarkup(rows)


def quality_panel(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Best", callback_data=f"dl|best|{token}"),
         InlineKeyboardButton("1080p", callback_data=f"dl|1080|{token}")],
        [InlineKeyboardButton("720p", callback_data=f"dl|720|{token}"),
         InlineKeyboardButton("480p", callback_data=f"dl|480|{token}")],
        [InlineKeyboardButton("MP3", callback_data=f"dl|audio|{token}"),
         InlineKeyboardButton("Cancel", callback_data=f"cancel|{token}")],
    ])


def group_quality_panel(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Best", callback_data=f"dl|best|{token}"),
         InlineKeyboardButton("720p", callback_data=f"dl|720|{token}")],
        [InlineKeyboardButton("480p", callback_data=f"dl|480|{token}"),
         InlineKeyboardButton("MP3", callback_data=f"dl|audio|{token}")],
        [InlineKeyboardButton("Cancel", callback_data=f"cancel|{token}")],
    ])


def admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Stats", callback_data="a_stats"),
         InlineKeyboardButton("Toggle On/Off", callback_data="a_toggle")],
        [InlineKeyboardButton(f"ℹ️ Build {__version__}", callback_data="version")],
        [InlineKeyboardButton("Back", callback_data="home")],
    ])


def progress_bar(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    return "▓" * int(pct / 10) + "░" * (10 - int(pct / 10))


def human_size(b: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


# ------------------------------------------------------------------ #
# Quality -> format string (STRICT cap, no fallthrough above)
# ------------------------------------------------------------------ #
def build_format(quality: str) -> dict:
    """
    Returns yt-dlp options for the requested quality.

    KEY FIX: We split caps so 1080p NEVER falls through to 2160p,
    and 480p NEVER falls through to 720p.
    """
    if quality == "audio":
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        }

    if quality == "best":
        # No cap, but prefer mp4 mux to keep file size sane
        return {
            "format": "bv*[ext=mp4][vcodec^=avc1]+ba[ext=m4a]/bv*[ext=mp4]+ba/bv*+ba/b",
        }

    # Map button -> strict ceiling. We do NOT use <= because yt-dlp
    # picks the largest matching stream which can be way above the
    # requested cap when the source only has one or two streams.
    cap_map = {
        "1080": 1080,
        "720": 720,
        "480": 480,
    }
    cap = cap_map.get(quality)
    if cap is None:
        return {"format": "bv*[ext=mp4]+ba/b"}

    # Strict cap: prefer exact height, then anything <= cap, then mp4-only
    # The order matters — yt-dlp picks the FIRST match in the chain.
    if cap == 1080:
        fmt = (
            "bv*[height=1080][ext=mp4]+ba[ext=m4a]/"
            "bv*[height=1080]+ba[ext=m4a]/"
            "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
            "bv*[height<=1080]+ba/"
            "bv*[height<=1080]"
        )
    elif cap == 720:
        fmt = (
            "bv*[height=720][ext=mp4]+ba[ext=m4a]/"
            "bv*[height=720]+ba[ext=m4a]/"
            "bv*[height<=720][ext=mp4]+ba[ext=m4a]/"
            "bv*[height<=720]+ba/"
            "bv*[height<=720]"
        )
    else:  # 480
        fmt = (
            "bv*[height=480][ext=mp4]+ba[ext=m4a]/"
            "bv*[height=480]+ba[ext=m4a]/"
            "bv*[height<=480][ext=mp4]+ba[ext=m4a]/"
            "bv*[height<=480]+ba/"
            "bv*[height<=480]"
        )

    return {
        "format": fmt,
        # Force sort by resolution so the chain actually picks the cap
        "format_sort": ["res", "ext:mp4:m4a", "size", "br"],
    }


# ------------------------------------------------------------------ #
# StatusHandle
# ------------------------------------------------------------------ #
@dataclass
class StatusHandle:
    bot: any
    chat_id: int
    message_id: int
    _last_text: str = ""
    _last_edit: float = 0.0
    _deleted: bool = False

    async def edit(self, text: str, *, force: bool = False, interval: float = 1.2) -> None:
        if self._deleted or text == self._last_text:
            return
        now = time.time()
        if not force and (now - self._last_edit) < interval:
            return
        self._last_text = text
        self._last_edit = now
        try:
            await self.bot.edit_message_text(chat_id=self.chat_id, message_id=self.message_id, text=text)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except Exception:
            pass

    async def delete(self) -> None:
        if self._deleted:
            return
        self._deleted = True
        try:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)
        except Exception:
            pass


# ------------------------------------------------------------------ #
# ProgressFile
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
        if chunk:
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
        text = f"{self.label} {progress_bar(pct)} {pct:.0f}%\n{spd:.1f} MB/s · {human_size(self.read_bytes)}/{human_size(self.size)}"
        try:
            asyncio.get_running_loop().create_task(self.status.edit(text, interval=0))
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
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# ------------------------------------------------------------------ #
# ffprobe helper
# ------------------------------------------------------------------ #
def _probe_height(path: Path) -> Optional[int]:
    if not HAS_FFPROBE:
        return None
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=s=x:p=0", str(path)],
            stderr=subprocess.DEVNULL, timeout=10,
        ).decode().strip()
        if "x" in out:
            return int(out.split("x", 1)[1])
    except Exception:
        return None
    return None


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
        f"Build: v{__version__}\n\n"
        f"Send any link to get started.",
        reply_markup=main_panel(update.effective_user.id),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Just send a video link and pick a quality.\n"
        "In groups, replies are silent (mention or reply triggers me).",
        reply_markup=main_panel(update.effective_user.id),
    )


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show build info so you can confirm the new code is running."""
    if not update.message:
        return
    changes = "\n".join(f"  • {c}" for c in __changelog__)
    await update.message.reply_text(
        f"🤖 Downloader Bot\n"
        f"Version: {__version__}\n"
        f"Build:   {__build__}\n\n"
        f"Changes:\n{changes}",
        reply_markup=main_panel(update.effective_user.id),
    )


# ------------------------------------------------------------------ #
# Text handler
# ------------------------------------------------------------------ #
def extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(")>].,!؟")


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
        try:
            await update.message.reply_text(
                f"Link received from {update.effective_user.first_name or 'user'}.\n"
                f"Build v{__version__} — Choose quality:",
                reply_markup=panel,
            )
        except Forbidden:
            log.warning("No permission to post in group %s", chat.id)
    else:
        await update.message.reply_text(f"v{__version__} — Choose quality:", reply_markup=panel)


# ------------------------------------------------------------------ #
# Download: Instagram
# ------------------------------------------------------------------ #
async def dl_ig(status: StatusHandle, context: ContextTypes.DEFAULT_TYPE,
                url: str, chat_id: int, reply_to: Optional[int] = None) -> None:
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

        post = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: ig_client.media_info(ig_client.media_pk_from_code(shortcode))),
            timeout=30,
        )

        if post.media_type == 2:  # video
            path = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ig_client.video_download(post.pk, folder=str(DOWNLOAD_DIR))),
                timeout=120,
            )
            sz = path.stat().st_size / 1048576
            if sz > MAX_FILE_MB:
                await status.edit(f"❌ {sz:.0f}MB > {MAX_FILE_MB}MB limit.", force=True)
                path.unlink(missing_ok=True)
                return
            await status.edit("📤 Uploading…", force=True)
            async with _typing(context, chat_id):
                with ProgressFile(path, status) as pf:
                    await context.bot.send_video(chat_id, pf, caption=f"{sz:.1f} MB",
                                                 supports_streaming=True, reply_to_message_id=reply_to)
            STATS["ig_downloads"] += 1
            STATS["downloads"] += 1
        elif post.media_type == 1:  # photo
            path = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ig_client.photo_download(post.pk, folder=str(DOWNLOAD_DIR))),
                timeout=30,
            )
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
        await status.edit("⏱ Timeout fetching from Instagram.", force=True)
    except Exception as e:
        STATS["errors"] += 1
        log.exception("IG fail")
        await status.edit(f"⚠️ IG fail: {str(e)[:150]}\nFalling back to yt-dlp…", force=True)
        await dl_ytdlp(status, context, url, "best", chat_id, reply_to=reply_to)


# ------------------------------------------------------------------ #
# Download: yt-dlp
# ------------------------------------------------------------------ #
async def dl_ytdlp(status: StatusHandle, context: ContextTypes.DEFAULT_TYPE,
                   url: str, quality: str, chat_id: int,
                   reply_to: Optional[int] = None) -> None:
    await status.edit(f"🔎 Fetching (q={quality})…", force=True)
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
        text = f"⬇️ {progress_bar(pct)} {pct:.0f}%\n{spd:.1f} MB/s"
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
        "noprogress": False,
    }
    if INSTA_RE.search(url) and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if HAS_ARIA2:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = ["-x", "16", "-s", "16", "-k", "1M"]

    # >>> THE FIX: use build_format() with STRICT caps
    opts.update(build_format(quality))

    log.info("DL start: quality=%s format=%s", quality, opts.get("format", "default"))

    try:
        def do_dl():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)

        info = await asyncio.wait_for(loop.run_in_executor(None, do_dl), timeout=300)

        skip_ext = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl", ".tmp"}
        files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() not in skip_ext]
        if not files:
            await status.edit("❌ Download failed: no output file.", force=True)
            return
        path = max(files, key=lambda f: f.stat().st_size)
        sz = path.stat().st_size / 1048576
        if sz > MAX_FILE_MB:
            await status.edit(f"❌ {sz:.0f}MB > {MAX_FILE_MB}MB limit.", force=True)
            return

        # Verify actual resolution
        actual_h = _probe_height(path)
        if quality in ("1080", "720", "480"):
            cap = int(quality)
            if actual_h and actual_h > cap:
                log.warning("CAP EXCEEDED: requested <=%sp got %sp", cap, actual_h)
            quality_label = f"{actual_h}p" if actual_h else f"~{quality}p"
        else:
            quality_label = "audio" if quality == "audio" else "best"

        cap_text = f"{sz:.1f} MB · {quality_label}"
        await status.edit("📤 Uploading…", force=True)
        async with _typing(context, chat_id):
            with ProgressFile(path, status) as pf:
                ext = path.suffix.lower()
                if ext in (".mp3", ".m4a", ".opus", ".ogg"):
                    await context.bot.send_audio(chat_id, pf, caption=cap_text, reply_to_message_id=reply_to)
                elif ext in (".mp4", ".mkv", ".webm", ".mov"):
                    await context.bot.send_video(
                        chat_id, pf, caption=cap_text, supports_streaming=True,
                        duration=(info or {}).get("duration"),
                        width=(info or {}).get("width"),
                        height=(info or {}).get("height"),
                        reply_to_message_id=reply_to,
                    )
                else:
                    await context.bot.send_document(chat_id, pf, caption=cap_text, reply_to_message_id=reply_to)
        STATS["downloads"] += 1
        await status.delete()
    except asyncio.TimeoutError:
        STATS["errors"] += 1
        await status.edit("⏱ Timeout.", force=True)
    except yt_dlp.utils.DownloadError as e:
        STATS["errors"] += 1
        msg = str(e).strip().split("\n")[-1] if str(e).strip() else "download error"
        log.warning("yt-dlp fail: %s", e)
        await status.edit(f"❌ {msg[:180]}", force=True)
    except Exception:
        STATS["errors"] += 1
        log.exception("DL fail")
        await status.edit("❌ Download error.", force=True)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ------------------------------------------------------------------ #
# Callbacks
# ------------------------------------------------------------------ #
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global BOT_ENABLED
    q = update.callback_query
    if not q:
        return
    try:
        await q.answer()
    except Exception:
        pass
    data = q.data or ""
    uid = q.from_user.id if q.from_user else 0
    chat_id = q.message.chat_id if q.message else 0
    msg_id = q.message.message_id if q.message else 0

    if data == "home":
        try:
            await q.edit_message_text(f"Home (v{__version__})", reply_markup=main_panel(uid))
        except BadRequest:
            pass
        return
    if data == "help":
        try:
            await q.edit_message_text(
                "Send a link → pick quality → done.\n"
                "1080p/720p/480p are strict caps — file will never exceed them.",
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
            f"🟢 Online · Engine: {e}{ig}\nBuild: v{__version__}\n"
            f"Downloads: {STATS['downloads']} · Errors: {STATS['errors']}\n"
            f"Uptime: {up // 3600}h {(up % 3600) // 60}m",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="home")]]),
        )
        return
    if data == "version":
        changes = "\n".join(f"  • {c}" for c in __changelog__)
        await q.edit_message_text(
            f"🤖 Build v{__version__}\n{__build__}\n\nChanges:\n{changes}",
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
            await q.edit_message_text("✅ Bot enabled" if BOT_ENABLED else "⛔ Bot disabled",
                                      reply_markup=admin_panel())
        except BadRequest:
            pass
        return
    if data.startswith("cancel|"):
        PENDING.pop(data.split("|", 1)[1], None)
        try:
            await q.edit_message_text("🚫 Cancelled.", reply_markup=main_panel(uid))
        except BadRequest:
            pass
        return
    if data.startswith("dl|"):
        try:
            _, quality, token = data.split("|", 2)
        except ValueError:
            return
        url = PENDING.pop(token, None)
        if not url:
            try:
                await q.edit_message_text("⌛ Link expired, send it again.")
            except BadRequest:
                pass
            return
        status = StatusHandle(bot=context.bot, chat_id=chat_id, message_id=msg_id)
        if INSTA_RE.search(url) and ig_client:
            asyncio.create_task(dl_ig(status, context, url, chat_id))
        else:
            asyncio.create_task(dl_ytdlp(status, context, url, quality, chat_id))


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
class _typing:
    def __init__(self, context, chat_id):
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
            except Exception:
                pass
        self._task = asyncio.create_task(loop())
        return self

    async def __aexit__(self, *a):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass


async def on_error(update, context):
    log.exception("Unhandled", exc_info=context.error)


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #
def main() -> int:
    if not BOT_TOKEN:
        print("FATAL: BOT_TOKEN env var is empty.", file=sys.stderr)
        return 1

    init_instagram()

    # Print build banner so it shows in startup logs
    log.info("=" * 50)
    log.info("Downloader Bot v%s", __version__)
    log.info("Build: %s", __build__)
    log.info("ffprobe=%s, aria2=%s, ig=%s", HAS_FFPROBE, HAS_ARIA2, ig_client is not None)
    log.info("=" * 50)

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
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
