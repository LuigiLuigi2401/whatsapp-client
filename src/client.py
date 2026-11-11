"""Core WhatsApp Multi-Device client manager."""

from . import compat  # noqa: F401 - Python 3.10 typing.Self compatibility

import base64
import io
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import segno
from neonize.client import NewClient
from neonize.events import (
    ConnectedEv,
    DisconnectedEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
)
from neonize.utils.jid import Jid2String, build_jid

from .handlers import ChatMessage, MessageStore, parse_incoming_message

log = logging.getLogger("whatsapp_client.core")


class WhatsAppClient:
    """High-level WhatsApp Multi-Device client.

    Maintains a persistent session using SQLite, manages companion device pairing
    via QR code or 8-digit phone pairing code, and provides thread-safe APIs for
    sending and receiving messages.
    """

    def __init__(self, session_path: str = "whatsapp_session.db"):
        self.session_path = session_path
        self.store = MessageStore()

        self._status: str = "disconnected"
        self._current_qr_b64: Optional[str] = None
        self._current_qr_raw: Optional[bytes] = None
        self._pairing_code: Optional[str] = None
        self._me_jid: Optional[str] = None
        self._me_phone: Optional[str] = None

        self._status_listeners: List[Callable[[str, Dict[str, Any]], None]] = []
        self._qr_listeners: List[Callable[[str], None]] = []
        self._state_lock = threading.Lock()
        self._worker_thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

        # Initialize underlying whatsmeow/neonize client
        self._client: NewClient = NewClient(self.session_path)
        self._register_event_handlers()

    @property
    def is_logged_in(self) -> bool:
        """Returns True if this client has established persistent session credentials."""
        try:
            return bool(self._client.is_logged_in)
        except Exception:
            return False

    @property
    def is_connected(self) -> bool:
        """Returns True if the client has an active socket connection to WhatsApp servers."""
        try:
            return bool(self._client.is_connected)
        except Exception:
            return False

    @property
    def status(self) -> str:
        with self._state_lock:
            return self._status

    @property
    def current_qr_b64(self) -> Optional[str]:
        with self._state_lock:
            return self._current_qr_b64

    @property
    def pairing_code(self) -> Optional[str]:
        with self._state_lock:
            return self._pairing_code

    @property
    def me_jid(self) -> Optional[str]:
        with self._state_lock:
            return self._me_jid

    def add_status_listener(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        with self._state_lock:
            self._status_listeners.append(callback)

    def add_qr_listener(self, callback: Callable[[str], None]) -> None:
        with self._state_lock:
            self._qr_listeners.append(callback)

    def _notify_status(self, new_status: str) -> None:
        with self._state_lock:
            self._status = new_status
            payload = self.get_state_dict()
            listeners = list(self._status_listeners)

        for listener in listeners:
            try:
                listener(new_status, payload)
            except Exception as e:
                log.exception("Error in status listener: %s", e)

    def _notify_qr(self, qr_b64: str) -> None:
        with self._state_lock:
            listeners = list(self._qr_listeners)

        for listener in listeners:
            try:
                listener(qr_b64)
            except Exception as e:
                log.exception("Error in QR listener: %s", e)

    def get_state_dict(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "status": self._status,
                "is_logged_in": self.is_logged_in,
                "is_connected": self.is_connected,
                "has_qr": self._current_qr_b64 is not None,
                "pairing_code": self._pairing_code,
                "me_jid": self._me_jid,
                "me_phone": self._me_phone,
            }

    def _register_event_handlers(self) -> None:
        """Attach WhatsApp protocol callbacks."""

        @self._client.qr
        def on_qr(client: NewClient, data_qr: bytes):
            log.info("New WhatsApp QR code received.")
            try:
                # Render terminal QR code for CLI visibility
                qr_obj = segno.make_qr(data_qr)
                qr_obj.terminal(compact=True)

                # Generate base64 PNG data URL for Web/PWA clients
                buf = io.BytesIO()
                qr_obj.save(buf, kind="png", scale=5)
                b64_str = f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"

                with self._state_lock:
                    self._current_qr_raw = data_qr
                    self._current_qr_b64 = b64_str
                    self._status = "scan_qr"

                self._notify_status("scan_qr")
                self._notify_qr(b64_str)
            except Exception as e:
                log.exception("Error processing QR code: %s", e)

        @self._client.event(ConnectedEv)
        def on_connected(client: NewClient, event: ConnectedEv):
            log.info("Successfully connected to WhatsApp multi-device gateway.")
            jid_str = None
            phone_str = None
            try:
                me = client.me or client.get_me()
                if me:
                    jid_str = Jid2String(me.JID) if hasattr(me, "JID") else str(me)
                    phone_str = me.JID.User if hasattr(me, "JID") and hasattr(me.JID, "User") else None
            except Exception:
                pass

            with self._state_lock:
                self._status = "connected"
                self._current_qr_b64 = None
                self._current_qr_raw = None
                self._pairing_code = None
                self._me_jid = jid_str
                self._me_phone = phone_str

            self._notify_status("connected")

        @self._client.event(PairStatusEv)
        def on_pair_status(client: NewClient, event: PairStatusEv):
            log.info("Pair status update received: %s", event)
            with self._state_lock:
                self._status = "paired"
                self._current_qr_b64 = None
            self._notify_status("paired")

        @self._client.event(DisconnectedEv)
        def on_disconnected(client: NewClient, event: DisconnectedEv):
            log.warning("Disconnected from WhatsApp: %s", event)
            with self._state_lock:
                self._status = "disconnected"
            self._notify_status("disconnected")

        @self._client.event(LoggedOutEv)
        def on_logged_out(client: NewClient, event: LoggedOutEv):
            log.warning("Session was logged out: %s", event)
            with self._state_lock:
                self._status = "logged_out"
                self._current_qr_b64 = None
                self._pairing_code = None
                self._me_jid = None
                self._me_phone = None
            self._notify_status("logged_out")

        @self._client.event(MessageEv)
        def on_message(client: NewClient, event: MessageEv):
            parsed = parse_incoming_message(event)
            if parsed:
                log.info("Incoming message from %s: %s", parsed.sender_name or parsed.sender_jid, parsed.text)
                self.store.add_message(parsed)

    def request_pairing_code(self, phone_number: str) -> str:
        """Request an 8-character pairing code for login without scanning QR."""
        clean_phone = phone_number.strip().replace("+", "").replace("-", "").replace(" ", "")
        log.info("Requesting pairing code for phone: %s", clean_phone)
        code = self._client.PairPhone(clean_phone, show_push_notification=True)
        with self._state_lock:
            self._pairing_code = code
            self._status = "pairing_code"
        self._notify_status("pairing_code")
        return code

    def send_text_message(self, to: str, text: str) -> ChatMessage:
        """Send a plain text message to a phone number or JID."""
        clean_to = to.strip().replace("+", "").replace("-", "").replace(" ", "")
        log.info("Sending message to %s: %s", clean_to, text)
        self._client.send_message(clean_to, text)

        target_jid = clean_to if "@" in clean_to else f"{clean_to}@s.whatsapp.net"
        msg = ChatMessage(
            id=f"out_{int(time.time() * 1000)}",
            chat_jid=target_jid,
            sender_jid=self._me_jid or "me",
            sender_name="Me",
            text=text,
            timestamp=int(time.time()),
            is_from_me=True,
            is_group="@g.us" in target_jid,
        )
        self.store.add_message(msg)
        return msg

    def start(self) -> None:
        """Start the client connection in a background thread."""
        if self._worker_thread and self._worker_thread.is_alive():
            log.warning("Client connection worker is already running.")
            return

        self._stopping.clear()
        self._notify_status("connecting")

        def _runner():
            log.info("WhatsApp client connection worker started.")
            try:
                self._client.connect()
            except Exception as e:
                log.exception("WhatsApp connection error: %s", e)
            finally:
                log.info("WhatsApp connection loop terminated.")
                with self._state_lock:
                    self._status = "disconnected"
                self._notify_status("disconnected")

        self._worker_thread = threading.Thread(target=_runner, name="WhatsAppClientWorker", daemon=True)
        self._worker_thread.start()

    def stop(self) -> None:
        """Gracefully disconnect and stop the client worker."""
        self._stopping.set()
        log.info("Stopping WhatsApp client...")
        try:
            self._client.disconnect()
        except Exception:
            pass
        try:
            self._client.stop()
        except Exception:
            pass
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=3.0)
        with self._state_lock:
            self._status = "disconnected"
        self._notify_status("disconnected")
