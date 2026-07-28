"""FastAPI entry point for Railway – exposes Telegram webhook at /webhook"""
import os
import asyncio
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import Application

# Load bot-7.py and retrieve the Telegram Application instance.
import importlib.util
import sys
import pathlib

bot_path = pathlib.Path(__file__).parent / "bot-7.py"
spec = importlib.util.spec_from_file_location("bot7", str(bot_path))
if spec is None or spec.loader is None:
    raise RuntimeError("Failed to load bot-7.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["bot7"] = mod
spec.loader.exec_module(mod)
# bot-7.py should expose either `app` or `application` (the telegram Application).
telegram_app: Application = getattr(mod, "application", getattr(mod, "app", None))
if telegram_app is None:
    raise RuntimeError("bot-7.py did not expose a Telegram Application instance")

fastapi_app = FastAPI()

@fastapi_app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    # Process the update asynchronously – no need to await.
    asyncio.create_task(telegram_app.process_update(update))
    return {"ok": True}

@fastapi_app.get("/health")
def health():
    return {"status": "ready"}
