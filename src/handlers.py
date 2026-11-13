"""Message parsing, event dispatching, and conversation storage."""

import logging
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from neonize.events import MessageEv
from neonize.utils import extract_text
from neonize.utils.jid import Jid2String

from .config import MEDIA_DIR

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
    media_type: Optional[str] = None       # "image", "sticker", "video", "audio", "document"
    media_path: Optional[str] = None       # Absolute path on disk once downloaded
    media_url: Optional[str] = None        # Relative web URL (e.g. /media/filename)
    media_filename: Optional[str] = None   # File name on disk
    caption: Optional[str] = None          # Image or video caption

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class MessageStore:
    """Thread-safe in-memory store for recent chat messages and contact summaries."""

    def __init__(self, max_per_chat: int = 100):
        self._max_per_chat = max_per_chat
        self._chats: Dict[str, deque] = defaultdict(lambda: deque(maxlen=self._max_per_chat))
        self._contacts: Dict[str, str] = {}  # jid -> name
        self._listeners: List[Callable[[ChatMessage], None]] = []
        self._update_listeners: List[Callable[[ChatMessage], None]] = []
        self._lock = Lock()

    def add_listener(self, callback: Callable[[ChatMessage], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[ChatMessage], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def add_update_listener(self, callback: Callable[[ChatMessage], None]) -> None:
        with self._lock:
            self._update_listeners.append(callback)

    def remove_update_listener(self, callback: Callable[[ChatMessage], None]) -> None:
        with self._lock:
            if callback in self._update_listeners:
                self._update_listeners.remove(callback)

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

    def update_message(self, message: ChatMessage) -> None:
        """Update an existing message (e.g. after media download completes)."""
        with self._lock:
            chat_msgs = self._chats.get(message.chat_jid)
            if chat_msgs:
                for idx, m in enumerate(chat_msgs):
                    if m.id == message.id:
                        chat_msgs[idx] = message
                        break
            listeners = list(self._update_listeners)

        for listener in listeners:
            try:
                listener(message)
            except Exception as e:
                log.exception("Error in message update listener: %s", e)

    def get_messages(self, chat_jid: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [m.to_dict() for m in self._chats.get(chat_jid, [])]

    def get_chat_summaries(self) -> List[Dict[str, Any]]:
        """Get summary list of active chats for the UI."""
        summaries = []
        with self._lock:
            for chat_jid, messages in self._chats.items():
                if not messages:
                    continue
                last_msg = messages[-1]
                contact_name = self._contacts.get(chat_jid, chat_jid.split("@")[0])

                # Determine readable preview text
                preview = last_msg.text
                if not preview and last_msg.caption:
                    preview = last_msg.caption
                elif not preview and last_msg.media_type:
                    preview = f"[{last_msg.media_type.capitalize()}]"

                summaries.append(
                    {
                        "chat_jid": chat_jid,
                        "title": contact_name,
                        "last_message": preview or "Media",
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


def unwrap_message(msg: Any) -> Any:
    """Recursively unpacks wrapper messages (ephemeral, view-once, document-with-caption) to find the payload."""
    curr = msg
    for _ in range(5):
        if hasattr(curr, "ephemeralMessage") and curr.ephemeralMessage.ListFields() and curr.ephemeralMessage.message.ListFields():
            curr = curr.ephemeralMessage.message
            continue
        if hasattr(curr, "viewOnceMessage") and curr.viewOnceMessage.ListFields() and curr.viewOnceMessage.message.ListFields():
            curr = curr.viewOnceMessage.message
            continue
        if hasattr(curr, "viewOnceMessageV2") and curr.viewOnceMessageV2.ListFields() and curr.viewOnceMessageV2.message.ListFields():
            curr = curr.viewOnceMessageV2.message
            continue
        if hasattr(curr, "viewOnceMessageV2Extension") and curr.viewOnceMessageV2Extension.ListFields() and curr.viewOnceMessageV2Extension.message.ListFields():
            curr = curr.viewOnceMessageV2Extension.message
            continue
        if hasattr(curr, "documentWithCaptionMessage") and curr.documentWithCaptionMessage.ListFields() and curr.documentWithCaptionMessage.message.ListFields():
            curr = curr.documentWithCaptionMessage.message
            continue
        break
    return curr


def parse_incoming_message(msg: MessageEv) -> Tuple[Optional[ChatMessage], Optional[Any]]:
    """Parse a neonize MessageEv protobuf into a ChatMessage dataclass and raw media payload if download needed."""
    try:
        raw_msg = unwrap_message(msg.Message)

        # Filter out internal WhatsApp encryption and protocol packets
        if hasattr(raw_msg, "senderKeyDistributionMessage") and raw_msg.senderKeyDistributionMessage.ListFields():
            return None, None
        if hasattr(raw_msg, "fastRatchetKeySenderKeyDistributionMessage") and raw_msg.fastRatchetKeySenderKeyDistributionMessage.ListFields():
            return None, None
        if hasattr(raw_msg, "protocolMessage") and raw_msg.protocolMessage.ListFields():
            return None, None

        media_type: Optional[str] = None
        caption: Optional[str] = None
        extension: Optional[str] = None
        text: str = ""

        # Check media attachments
        if hasattr(raw_msg, "imageMessage") and raw_msg.imageMessage.ListFields():
            media_type = "image"
            caption = raw_msg.imageMessage.caption or ""
            mimetype = raw_msg.imageMessage.mimetype or "image/jpeg"
            extension = ".png" if "png" in mimetype.lower() else ".jpg"
            text = caption if caption else "[Image]"
        elif hasattr(raw_msg, "stickerMessage") and raw_msg.stickerMessage.ListFields():
            media_type = "sticker"
            caption = ""
            extension = ".webp"
            text = "[Sticker]"
        elif hasattr(raw_msg, "videoMessage") and raw_msg.videoMessage.ListFields():
            media_type = "video"
            caption = raw_msg.videoMessage.caption or ""
            extension = ".mp4"
            text = caption if caption else "[Video]"
        elif hasattr(raw_msg, "audioMessage") and raw_msg.audioMessage.ListFields():
            media_type = "audio"
            caption = ""
            mimetype = raw_msg.audioMessage.mimetype or ""
            extension = ".ogg" if "ogg" in mimetype.lower() else ".mp3"
            text = "[Audio Message]"
        elif hasattr(raw_msg, "documentMessage") and raw_msg.documentMessage.ListFields():
            media_type = "document"
            doc_name = raw_msg.documentMessage.fileName or "file"
            caption = raw_msg.documentMessage.caption or ""
            extension = Path(doc_name).suffix or ".bin"
            text = f"[Document: {doc_name}]" if not caption else f"[Document: {doc_name}] {caption}"
        elif hasattr(raw_msg, "reactionMessage") and raw_msg.reactionMessage.ListFields():
            text = f"Reacted {raw_msg.reactionMessage.text}"
        else:
            text = extract_text(raw_msg)

        if not text and not media_type:
            if hasattr(raw_msg, "contactMessage") and raw_msg.contactMessage.ListFields():
                text = f"[Contact: {raw_msg.contactMessage.displayName}]"
            elif hasattr(raw_msg, "locationMessage") and raw_msg.locationMessage.ListFields():
                text = "[Location]"
            elif (hasattr(raw_msg, "pollCreationMessage") and raw_msg.pollCreationMessage.ListFields()) or (
                hasattr(raw_msg, "pollCreationMessageV3") and raw_msg.pollCreationMessageV3.ListFields()
            ):
                text = "[Poll]"
            else:
                return None, None

        info = msg.Info
        source = info.MessageSource
        chat_jid = Jid2String(source.Chat)
        sender_jid = Jid2String(source.Sender)
        sender_name = info.Pushname or source.Sender.User
        timestamp = normalize_timestamp(info.Timestamp)

        media_path = None
        media_url = None
        media_filename = None
        media_to_download = None

        if media_type and extension:
            clean_id = "".join(c for c in info.ID if c.isalnum() or c in ("-", "_"))
            if not clean_id:
                clean_id = f"media_{int(time.time() * 1000)}"
            media_filename = f"{clean_id}{extension}"
            target_path = MEDIA_DIR / media_filename
            media_url = f"/media/{media_filename}"

            if target_path.exists() and target_path.stat().st_size > 0:
                media_path = str(target_path)
            else:
                media_to_download = raw_msg

        chat_msg = ChatMessage(
            id=info.ID,
            chat_jid=chat_jid,
            sender_jid=sender_jid,
            sender_name=sender_name,
            text=text,
            timestamp=timestamp,
            is_from_me=bool(source.IsFromMe),
            is_group=bool(source.IsGroup),
            media_type=media_type,
            media_path=media_path,
            media_url=media_url,
            media_filename=media_filename,
            caption=caption,
        )
        return chat_msg, media_to_download
    except Exception as e:
        log.exception("Failed to parse incoming MessageEv: %s", e)
        return None, None
