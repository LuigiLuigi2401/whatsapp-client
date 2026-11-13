"""FastAPI web server providing REST endpoints and real-time WebSockets for mobile clients."""

from . import compat  # noqa: F401

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .client import WhatsAppClient
from .config import BASE_DIR, CLIENT_NAME, MEDIA_DIR, SESSION_DB
from .handlers import ChatMessage

log = logging.getLogger("whatsapp_client.server")

# Active WebSocket connections
active_connections: Set[WebSocket] = set()
event_loop: asyncio.AbstractEventLoop = None  # Captured during startup

# Global client singleton
client = WhatsAppClient(session_path=SESSION_DB)


class PairRequest(BaseModel):
    phone: str


class SendMessageRequest(BaseModel):
    to: str
    text: str


async def broadcast_ws(message: Dict[str, Any]):
    """Broadcast JSON message to all connected web clients."""
    disconnected = set()
    for ws in list(active_connections):
        try:
            await ws.send_json(message)
        except Exception:
            disconnected.add(ws)

    for dead in disconnected:
        active_connections.discard(dead)


def on_client_message(msg: ChatMessage):
    """Callback triggered from WhatsApp worker thread when a message arrives."""
    if event_loop and event_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            broadcast_ws({"type": "new_message", "message": msg.to_dict()}),
            event_loop,
        )


def on_client_message_updated(msg: ChatMessage):
    """Callback triggered when a message is updated (e.g. media downloaded)."""
    if event_loop and event_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            broadcast_ws({"type": "message_updated", "message": msg.to_dict()}),
            event_loop,
        )


def on_client_status(new_status: str, state: Dict[str, Any]):
    """Callback triggered from WhatsApp worker thread when connection status changes."""
    if event_loop and event_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            broadcast_ws({"type": "status", "status": new_status, "state": state}),
            event_loop,
        )


def on_client_qr(qr_b64: str):
    """Callback triggered when a new QR code is generated."""
    if event_loop and event_loop.is_running():
        asyncio.run_coroutine_threadsafe(
            broadcast_ws({"type": "qr", "qr": qr_b64}),
            event_loop,
        )


# Hook up callbacks
client.store.add_listener(on_client_message)
client.store.add_update_listener(on_client_message_updated)
client.add_status_listener(on_client_status)
client.add_qr_listener(on_client_qr)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global event_loop
    event_loop = asyncio.get_running_loop()
    log.info("Starting WhatsApp client daemon...")
    client.start()
    yield
    log.info("Stopping WhatsApp client daemon...")
    client.stop()


app = FastAPI(title="WhatsApp Lite Client API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# REST Endpoints
@app.get("/api/status")
async def get_status():
    """Retrieve current client connectivity and authorization state."""
    return client.get_state_dict()


@app.get("/api/qr")
async def get_qr():
    """Retrieve the latest base64 QR code if currently awaiting scan."""
    qr_b64 = client.current_qr_b64
    if not qr_b64:
        return JSONResponse(status_code=404, content={"message": "No active QR code available"})
    return {"qr": qr_b64}


@app.post("/api/pair")
async def pair_phone(req: PairRequest):
    """Generate an 8-character pairing code using phone number."""
    try:
        code = client.request_pairing_code(req.phone)
        return {"success": True, "pairing_code": code, "phone": req.phone}
    except Exception as e:
        log.exception("Pairing code error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/chats")
async def get_chats():
    """List recent conversation summaries."""
    return client.store.get_chat_summaries()


@app.get("/api/messages/{chat_jid:path}")
async def get_messages(chat_jid: str):
    """Retrieve message history for a specific chat."""
    return client.store.get_messages(chat_jid)


@app.post("/api/send")
async def send_message(req: SendMessageRequest):
    """Send a plain text message to a contact or group."""
    if not req.to or not req.text:
        raise HTTPException(status_code=400, detail="Missing 'to' or 'text' field.")
    try:
        msg = client.send_text_message(req.to, req.text)
        return {"success": True, "message": msg.to_dict()}
    except Exception as e:
        log.exception("Failed to send message: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/connect")
async def trigger_connect():
    """Trigger manual reconnection."""
    client.start()
    return {"success": True, "status": client.status}


# WebSocket Endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    try:
        # Push initial snapshot upon connection
        await websocket.send_json(
            {
                "type": "init",
                "client_name": CLIENT_NAME,
                "state": client.get_state_dict(),
                "chats": client.store.get_chat_summaries(),
                "qr": client.current_qr_b64,
            }
        )

        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "send_message":
                to = data.get("to")
                text = data.get("text")
                if to and text:
                    client.send_text_message(to, text)
            elif action == "request_pairing_code":
                phone = data.get("phone")
                if phone:
                    code = client.request_pairing_code(phone)
                    await websocket.send_json({"type": "pairing_code", "code": code})
    except WebSocketDisconnect:
        active_connections.discard(websocket)
    except Exception as e:
        log.warning("WebSocket error: %s", e)
        active_connections.discard(websocket)


# Mount media directory for attachments and stickers
if MEDIA_DIR.exists():
    app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

# Mount static assets for mobile PWA
web_dir = BASE_DIR / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

    @app.get("/")
    async def serve_index():
        return FileResponse(web_dir / "index.html")

    @app.get("/manifest.json")
    async def serve_manifest():
        return FileResponse(web_dir / "manifest.json", media_type="application/manifest+json")

    @app.get("/sw.js")
    async def serve_sw():
        return FileResponse(web_dir / "sw.js", media_type="application/javascript")
