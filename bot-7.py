# -*- coding: utf-8 -*-
"""
Downloader Bot — Simple v4.0.0
================================
Ultra-simplified: send link → get file (1080p best, 49MB cap).
No panels, no choice, just download and send.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

try:
    from instagrapi import Client as InstaClient
    from instagrapi.exceptions import MediaNotFound, LoginRequired, ChallengeRequired, PrivateError
    HAS_INSTA = True
except ImportError:
    HAS_INSTA = False
    MediaNotFound = LoginRequired = ChallengeRequired = PrivateError = Exception  # type: ignore

# ============== CONFIG ============== #
__version__ = "4.0.0-simple"
__build__ = "2026-07-27-ultra"

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "7438138322").split(",") if x.strip().isdigit()}
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "downloads"))
DOWNLOAD_DIR.mkdir(exist_ok=True)
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "49"))
HAS_ARIA2 = shutil.which("aria2c") is not None
HAS_FFMPEG = shutil.which("ffmpeg") is not None
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

# ============== INSTAGRAM ============== #
ig_client: Optional["InstaClient"] = None


def init_instagram() -> None:
    global ig_client
    if not (HAS_INSTA and IG_USERNAME and IG_PASSWORD):
        return
    try:
        c = InstaClient()
        sess = Path("ig_session.json")
        if sess.exists():
            try:
                c.load_settings(sess)
                c.login(IG_USERNAME, IG_PASSWORD)
                ig_client = c
                return
            except Exception:
                pass
        c.login(IG_USERNAME, IG_PASSWORD)
        try:
            c.dump_settings(sess)
        except Exception:
            pass
        ig_client = c
    except Exception as e:
        log.error("IG login failed: %s", e)


# ============== HELPERS ============== #
def human_size(b: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {u}"
        b /= 1024
    return f"{b:.1f} TB"


def extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    m = URL_RE.search(text)
    return m.group(0).rstrip(")>].,!؟") if m else None


async def safe_delete(context, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def send_typing(context, chat_id: int) -> None:
    try:
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        pass


# ============== DOWNLOAD: YT-DLP ============== #
# Format chain: pick the best video up to 1080p, with audio, fallback to any.
# The chain is designed to NEVER fail — every step is progressively more permissive.
YDL_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "retries": 3,
    "fragment_retries": 5,
    "socket_timeout": 20,
    "concurrent_fragment_downloads": 8,
    "max_filesize": MAX_FILE_MB * 1024 * 1024,
    "writethumbnail": False,
    "geo_bypass": True,
}


def build_opts(url: str, is_ig: bool) -> dict:
    """Build yt-dlp options. Always 1080p cap, with robust fallback chain."""
    opts = dict(YDL_OPTS_BASE)
    if is_ig and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if HAS_ARIA2:
        opts["external_downloader"] = "aria2c"
        opts["external_downloader_args"] = ["-x", "16", "-s", "16", "-k", "1M"]
    if HAS_FFMPEG:
        opts["merge_output_format"] = "mp4"

    # Format: prefer 1080p with audio, fall through to any <=1080p,
    # then muxed (Twitter/IG), then anything (size cap is the real limit)
    opts["format"] = (
        # YouTube-style: separate video + audio streams
        "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"  # mp4 + m4a
        "bv*[height<=1080]+ba[ext=m4a]/"            # any video + m4a
        "bv*[height<=1080][ext=mp4]+ba/"            # mp4 + any audio
        "bv*[height<=1080]+ba/"                     # any + any
        "bv*[height<=1080]/"                         # video only
        # Muxed fallback (Twitter/IG/TikTok): single stream w/ audio
        "b[height<=1080][ext=mp4]/"
        "b[height<=1080]/"
        # Last resort
        "b"
    )
    return opts


async def download_ytdlp(url: str, folder: Path) -> tuple[Optional[Path], Optional[dict]]:
    """Download via yt-dlp. Returns (file_path, info) or (None, None) on error."""
    loop = asyncio.get_running_loop()
    is_ig = bool(INSTA_RE.search(url))
    opts = build_opts(url, is_ig)
    opts["outtmpl"] = str(folder / "%(title).60s.%(ext)s")

    def do_dl() -> Optional[dict]:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        except Exception as e:
            log.warning("yt-dlp error: %s", str(e)[:200])
            return None

    info = await asyncio.wait_for(
        loop.run_in_executor(None, do_dl), timeout=300
    )

    # Pick the largest non-thumbnail file
    skip = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl", ".tmp", ".json"}
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() not in skip]
    if not files:
        return None, None
    path = max(files, key=lambda f: f.stat().st_size)
    return path, info


# ============== DOWNLOAD: INSTAGRAM ============== #
async def download_ig(url: str) -> Optional[Path]:
    if not ig_client:
        return None
    loop = asyncio.get_running_loop()
    try:
        m = re.search(r"/(p|reel|tv|stories)/([A-Za-z0-9_-]+)", url)
        if not m:
            return None
        shortcode = m.group(2)
        post = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: ig_client.media_info(ig_client.media_pk_from_code(shortcode))),
            timeout=30,
        )
        if post.media_type == 2:
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ig_client.video_download(post.pk, folder=str(DOWNLOAD_DIR))),
                timeout=120,
            )
        elif post.media_type == 1:
            return await asyncio.wait_for(
                loop.run_in_executor(None, lambda: ig_client.photo_download(post.pk, folder=str(DOWNLOAD_DIR))),
                timeout=30,
            )
    except Exception as e:
        log.warning("IG download failed: %s", e)
    return None


# ============== MAIN HANDLER ============== #
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user or not update.effective_chat:
        return

    url = extract_url(update.message.text or "")
    if not url:
        return

    chat_id = update.effective_chat.id
    user_msg_id = update.message.message_id
    user = update.effective_user.first_name or "user"

    # Send one minimal "fetching" message
    try:
        status = await update.message.reply_text("⏳")
    except Forbidden:
        return
    status_id = status.message_id

    await send_typing(context, chat_id)

    # === Decide: Instagram or yt-dlp? ===
    is_ig = bool(INSTA_RE.search(url))
    path: Optional[Path] = None
    info: Optional[dict] = None
    folder: Optional[Path] = None

    if is_ig and ig_client:
        path = await download_ig(url)
        if path is None:
            # fallback to yt-dlp
            folder = DOWNLOAD_DIR / uuid.uuid4().hex
            folder.mkdir(exist_ok=True)
            path, info = await download_ytdlp(url, folder)
    else:
        folder = DOWNLOAD_DIR / uuid.uuid4().hex
        folder.mkdir(exist_ok=True)
        path, info = await download_ytdlp(url, folder)

    if path is None or not path.exists():
        await safe_delete(context, chat_id, status_id)
        try:
            await update.message.reply_text("❌ Failed to download.")
        except Forbidden:
            pass
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        return

    sz = path.stat().st_size / 1048576
    if sz > MAX_FILE_MB:
        await safe_delete(context, chat_id, status_id)
        try:
            await update.message.reply_text(f"❌ File {sz:.0f}MB > {MAX_FILE_MB}MB limit.")
        except Forbidden:
            pass
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        return

    cap = f"{sz:.1f} MB"
    ext = path.suffix.lower()
    is_video = ext in (".mp4", ".mkv", ".webm", ".mov")
    is_audio = ext in (".mp3", ".m4a", ".opus", ".ogg")

    # === Send the file ===
    try:
        await send_typing(context, chat_id)
        with open(path, "rb") as f:
            if is_video:
                await context.bot.send_video(
                    chat_id, f, caption=cap, supports_streaming=True,
                    duration=(info or {}).get("duration"),
                    width=(info or {}).get("width"),
                    height=(info or {}).get("height"),
                )
            elif is_audio:
                await context.bot.send_audio(chat_id, f, caption=cap)
            else:
                await context.bot.send_photo(chat_id, f, caption=cap) if ext in (".jpg", ".jpeg", ".png", ".webp") \
                    else await context.bot.send_document(chat_id, f, caption=cap)
    except Exception as e:
        log.warning("Send failed: %s", e)
        try:
            await update.message.reply_text(f"❌ Send failed: {str(e)[:100]}")
        except Forbidden:
            pass
    finally:
        # Clean up
        await safe_delete(context, chat_id, status_id)
        # Delete user's original link message
        if update.effective_chat.type != "private":
            await safe_delete(context, chat_id, user_msg_id)
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


# ============== COMMANDS ============== #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        f"👋 Hi {update.effective_user.first_name or 'there'}\n\n"
        f"Send me any video link, I'll send back the file. (1080p, max {MAX_FILE_MB}MB)\n"
        f"Build: v{__version__}",
    )


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(f"🤖 v{__version__} · {__build__}")


# ============== MAIN ============== #
def main() -> int:
    if not BOT_TOKEN:
        print("FATAL: BOT_TOKEN is empty", file=sys.stderr)
        return 1

    init_instagram()

    log.info("=" * 50)
    log.info("Downloader Bot v%s", __version__)
    log.info("Build: %s", __build__)
    log.info("ffmpeg=%s aria2=%s ig=%s", HAS_FFMPEG, HAS_ARIA2, ig_client is not None)
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
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(drop_pending_updates=True, allowed_updates=["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
