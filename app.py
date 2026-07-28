#!/usr/bin/env python3
"""Vortexa entry point — runs bot-7.py"""
import subprocess
import sys
import os

# Ensure dependencies are installed
subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "yt-dlp", "instagrapi", "python-telegram-bot"])

# Run the bot
exec(open("bot-7.py").read())
