"""
Android TV Remote Control — Backend Server
==========================================
FastAPI app that:
  1. Accepts Telegram webhook updates
  2. Runs an LLM agent to parse natural-language commands
  3. Maintains a WebSocket hub for connected Android TV clients
  4. Forwards structured JSON commands to the correct TV client
"""

import os
import json
import asyncio
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]      # required
GEMINI_API_KEY    = os.environ["GEMINI_API_KEY"]        # required
TV_CLIENT_SECRET     = os.environ["TV_CLIENT_SECRET"]         # shared secret between server ↔ TV
ALLOWED_TELEGRAM_IDS = set(                                   # comma-separated user IDs
    int(x) for x in os.environ.get("ALLOWED_TELEGRAM_IDS", "").split(",") if x
)

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# ─── App & WebSocket Hub ───────────────────────────────────────────────────────

app = FastAPI(title="TV Remote Bot")

class TVHub:
    """Manages active WebSocket connections from Android TV clients."""

    def __init__(self):
        # device_id → WebSocket
        self._connections: dict[str, WebSocket] = {}

    async def register(self, device_id: str, ws: WebSocket):
        self._connections[device_id] = ws
        logger.info(f"TV client registered: {device_id}")

    def unregister(self, device_id: str):
        self._connections.pop(device_id, None)
        logger.info(f"TV client disconnected: {device_id}")

    @property
    def connected_devices(self) -> list[str]:
        return list(self._connections.keys())

    async def send_command(self, device_id: str, command: dict) -> bool:
        ws = self._connections.get(device_id)
        if not ws:
            return False
        try:
            await ws.send_text(json.dumps(command))
            return True
        except Exception as e:
            logger.error(f"Failed to send command to {device_id}: {e}")
            self.unregister(device_id)
            return False

    async def broadcast(self, command: dict):
        """Send to all connected TVs (useful when there's only one)."""
        for device_id in list(self._connections):
            await self.send_command(device_id, command)

hub = TVHub()

# ─── LLM Agent ────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a TV remote control assistant.
Parse the user's message and return ONLY a valid JSON object — no prose, no markdown fences.

Supported commands and their JSON shape:
  change_channel  → {"command": "change_channel", "value": <integer channel number>}
  channel_up      → {"command": "channel_up"}
  channel_down    → {"command": "channel_down"}
  volume_up       → {"command": "volume_up"}
  volume_down     → {"command": "volume_down"}
  mute            → {"command": "mute"}
  power           → {"command": "power"}
  unknown         → {"command": "unknown", "message": "<brief explanation>"}

Rules:
- If the user says "channel 105", "switch to 105", "go to channel 105" → change_channel with value 105
- "louder", "turn up", "volume up" → volume_up
- "quieter", "turn down", "volume down" → volume_down
- "mute", "silence", "quiet" → mute
- "channel up", "next channel" → channel_up
- "channel down", "previous channel" → channel_down
- "turn off", "turn on", "power" → power
- Anything else → unknown
"""

async def parse_command(user_message: str) -> dict:

    async with httpx.AsyncClient(timeout=15) as client:

        resp = await client.post(

            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash-latest:generateContent?key={GEMINI_API_KEY}",

            headers={"Content-Type": "application/json"},

            json={

                "contents": [{"parts": [{"text": SYSTEM_PROMPT + "\n\nUser: " + user_message}]}]

            },

        )

        resp.raise_for_status()

        raw = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

        if raw.startswith("```"):

            raw = raw.split("```")[1]

            if raw.startswith("json"):

                raw = raw[4:]

        return json.loads(raw)
 
# ─── Telegram Helpers ─────────────────────────────────────────────────────────

async def telegram_send(chat_id: int, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        })

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_TELEGRAM_IDS:
        return True   # no whitelist configured → allow all (dev mode)
    return user_id in ALLOWED_TELEGRAM_IDS

# ─── Telegram Webhook ─────────────────────────────────────────────────────────

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    update = await request.json()
    logger.info(f"Telegram update: {update}")

    message = update.get("message") or update.get("edited_message")
    if not message:
        return JSONResponse({"ok": True})

    chat_id  = message["chat"]["id"]
    user_id  = message["from"]["id"]
    text     = message.get("text", "").strip()

    # ── Access control ──
    if not is_allowed(user_id):
        await telegram_send(chat_id, "⛔ You are not authorised to control this TV.")
        return JSONResponse({"ok": True})

    if not text:
        await telegram_send(chat_id, "Please send a text command.")
        return JSONResponse({"ok": True})

    # ── Special slash commands ──
    if text == "/start":
        await telegram_send(chat_id,
            "👋 <b>TV Remote Bot</b>\n\n"
            "Just tell me what to do in plain English:\n"
            "  • <i>switch to channel 205</i>\n"
            "  • <i>turn up the volume</i>\n"
            "  • <i>mute the TV</i>\n"
            "  • <i>turn off</i>\n\n"
            f"Connected TVs: {len(hub.connected_devices)}"
        )
        return JSONResponse({"ok": True})

    if text == "/devices":
        devices = hub.connected_devices
        msg = ("📺 Connected TVs:\n" + "\n".join(f"  • {d}" for d in devices)
               if devices else "No TVs connected right now.")
        await telegram_send(chat_id, msg)
        return JSONResponse({"ok": True})

    # ── LLM command parsing ──
    await telegram_send(chat_id, "⏳ Processing…")

    try:
        command = await parse_command(text)
    except Exception as e:
        logger.error(f"LLM parse error: {e}")
        await telegram_send(chat_id, "❌ Failed to understand the command. Please try again.")
        return JSONResponse({"ok": True})

    if command.get("command") == "unknown":
        await telegram_send(chat_id,
            f"🤷 I didn't understand that: {command.get('message', 'unknown command')}")
        return JSONResponse({"ok": True})

    # ── Forward to TV ──
    if not hub.connected_devices:
        await telegram_send(chat_id, "📺 No TV is connected right now.")
        return JSONResponse({"ok": True})

    await hub.broadcast(command)

    # Human-readable confirmation
    summaries = {
        "change_channel": f"📺 Switching to channel {command.get('value')}",
        "channel_up":     "📺 Channel ▲",
        "channel_down":   "📺 Channel ▼",
        "volume_up":      "🔊 Volume ▲",
        "volume_down":    "🔉 Volume ▼",
        "mute":           "🔇 Muted / Unmuted",
        "power":          "⏻ Power toggled",
    }
    await telegram_send(chat_id, summaries.get(command["command"], f"✅ Sent: {command}"))
    return JSONResponse({"ok": True})

# ─── WebSocket Endpoint (Android TV client) ───────────────────────────────────

@app.websocket("/ws/tv/{device_id}")
async def tv_websocket(websocket: WebSocket, device_id: str):
    """
    Android TV clients connect here.
    Authentication: first message must be {"auth": "<TV_CLIENT_SECRET>"}
    """
    await websocket.accept()

    # ── Auth handshake ──
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        msg = json.loads(raw)
        if msg.get("auth") != TV_CLIENT_SECRET:
            await websocket.send_text(json.dumps({"error": "unauthorized"}))
            await websocket.close(code=1008)
            return
    except asyncio.TimeoutError:
        await websocket.close(code=1008)
        return

    await websocket.send_text(json.dumps({"status": "authenticated"}))
    await hub.register(device_id, websocket)

    try:
        while True:
            # Keep alive — client may send {"type":"ping"}
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            if msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        hub.unregister(device_id)

# ─── Health Check ─────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "connected_tvs": hub.connected_devices}
