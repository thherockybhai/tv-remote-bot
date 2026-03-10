#!/usr/bin/env python3
"""
Register the Telegram webhook.
Run once after deploying the server:
  python register_webhook.py
"""

import os, sys, httpx

TOKEN      = os.environ["TELEGRAM_BOT_TOKEN"]
SERVER_URL = os.environ["SERVER_URL"]   # e.g. https://your-server.com

url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
r = httpx.post(url, json={"url": f"{SERVER_URL}/webhook/telegram"})
print(r.json())
