import os

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

NEWS_CHANNEL_ID = int(os.getenv("NEWS_CHANNEL_ID", 0))
SRT_CHANNEL_ID = int(os.getenv("SRT_CHANNEL_ID", 0))
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID", 0))
CHAT_CHANNEL_ID = int(os.getenv("CHAT_CHANNEL_ID", 0))
NOTI_CHANNEL_ID = CHAT_CHANNEL_ID

AVAILABLE_MODELS = ["gemini-3.1-flash-lite-preview","gemini-3-flash-preview","gemini-2.5-pro"]

WORKSPACE_CONTEXT = f"""
ENVIRONMENT_INFO:
- Device: Samsung Galaxy S9 (Android)
- Terminal: Termux (Linux)
- Bot Framework: discord.py
- Local API (For standalone scripts): http://localhost:5000/send (POST, JSON: {{"channel_id": int, "content": "str"}})
- Key Channels:
  - CHAT_CHANNEL_ID: {CHAT_CHANNEL_ID}
  - STATUS_CHANNEL_ID: {STATUS_CHANNEL_ID}
- Available Tools: termux-api, crontab, python3
- CRITICAL: DO NOT use 'discord_webhook' or other external libs. Use 'requests' to call Local API.
- CRITICAL: For scheduling, provide the 'crontab -l' modification logic.
"""
