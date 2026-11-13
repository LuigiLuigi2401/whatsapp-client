"""Message parsing, event dispatching, and conversation storage."""

import logging
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

from neonize.events import MessageEv
from neonize.utils import extract_text
from neonize.utils.jid import Jid2String

log = logging.getLogger("whatsapp_client.handlers")


@dataclass
class ChatMessage:
    id: str
    chat_jid: str
    sender_jid: str
    sender_name: str
    text: str
    timestamp: int
    is_from_me: bool
    is_group: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MessageStore:
    """Thread-safe in-memory store for recent chat messages and contact summaries."""

    def __init__(self, max_per_chat: int = 100):
        self._max_per_chat = max_per_chat
        self._chats: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self._max_per_chat))
        self._contacts: Dict[str, str] = {}  # jid -> name
        self._listeners: List[Callable[[ChatMessage], None]] = []
        self._lock = Lock()

    def add_listener(self, callback: Callable[[ChatMessage], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[ChatMessage], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def add_message(self, message: ChatMessage) -> None:
        with self._lock:
            self._chats[message.chat_jid].append(message)
            if message.sender_name:
                self._contacts[message.sender_jid] = message.sender_name
            listeners = list(self._listeners)

        # Notify outside lock to avoid deadlocks
        for listener in listeners:
            try:
                listener(message)
            except Exception as e:
                log.exception("Error in message listener: %s", e)

    def get_messages(self, chat_jid: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [m.to_dict() for m in self._chats.get(chat_jid, [])]

    def get_chat_summaries(self) -> List[Dict[str, Any]]:
        """Get summary list of active chats for the mobile UI."""
        summaries = []
        with self._lock:
            for chat_jid, messages in self._chats.items():
                if not messages:
                    continue
                last_msg = messages[-1]
                contact_name = self._contacts.get(chat_jid, chat_jid.split("@")[0])
                summaries.append(
                    {
                        "chat_jid": chat_jid,
                        "title": contact_name,
                        "last_message": last_msg.text,
                        "last_timestamp": last_msg.timestamp,
                        "unread_count": 0,
                        "is_group": last_msg.is_group,
                    }
                )

        summaries.sort(key=lambda x: x["last_timestamp"], reverse=True)
        return summaries


def normalize_timestamp(raw_ts: Any) -> int:
    """Safely convert any WhatsApp timestamp (seconds, milliseconds, or microseconds) to unix seconds."""
    try:
        ts = int(raw_ts)
        if ts > 1e14:  # microseconds / nanoseconds
            ts = int(ts / 1e6)
        elif ts > 1e11:  # milliseconds (13-digits, standard in WA history sync)
            ts = int(ts / 1e3)
        if ts <= 0 or ts > 253402300799:  # sanity check (year 9999 limit)
            return int(time.time())
        return ts
    except Exception:
        return int(time.time())


def parse_incoming_message(msg: MessageEv) -> Optional[ChatMessage]:
    """Parse a neonize MessageEv protobuf into a ChatMessage dataclass."""
    try:
        raw_msg = msg.Message

        # Filter out internal WhatsApp encryption and protocol packets
        if raw_msg.senderKeyDistributionMessage.ListFields():
            return None
        if raw_msg.fastRatchetKeySenderKeyDistributionMessage.ListFields():
            return None
        if raw_msg.protocolMessage.ListFields():
            return None

        # Handle reactions
        if raw_msg.reactionMessage.ListFields():
            text = f"Reacted {raw_msg.reactionMessage.text}"
        else:
            text = extract_text(raw_msg)

        if not text:
            # Check if it is an attachment without caption
            if raw_msg.imageMessage.ListFields():
                text = "[Image]"
            elif raw_msg.videoMessage.ListFields():
                text = "[Video]"
            elif raw_msg.audioMessage.ListFields():
                text = "[Audio Message]"
            elif raw_msg.documentMessage.ListFields():
                text = f"[Document: {raw_msg.documentMessage.fileName or 'file'}]"
            elif raw_msg.stickerMessage.ListFields():
                text = "[Sticker]"
            elif raw_msg.contactMessage.ListFields():
                text = f"[Contact: {raw_msg.contactMessage.displayName}]"
            elif raw_msg.locationMessage.ListFields():
                text = "[Location]"
            elif raw_msg.pollCreationMessage.ListFields() or raw_msg.pollCreationMessageV3.ListFields():
                text = "[Poll]"
            else:
                # Ignore unknown internal protocol frames during initial history sync
                return None

        info = msg.Info
        source = info.MessageSource
        chat_jid = Jid2String(source.Chat)
        sender_jid = Jid2String(source.Sender)
        sender_name = info.Pushname or source.Sender.User
        timestamp = normalize_timestamp(info.Timestamp)

        return ChatMessage(
            id=info.ID,
            chat_jid=chat_jid,
            sender_jid=sender_jid,
            sender_name=sender_name,
            text=text,
            timestamp=timestamp,
            is_from_me=bool(source.IsFromMe),
            is_group=bool(source.IsGroup),
        )
    except Exception as e:
        log.exception("Failed to parse incoming MessageEv: %s", e)
        return None
