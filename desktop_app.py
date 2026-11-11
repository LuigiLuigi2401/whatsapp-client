"""WhatsApp Lite - Lightweight Native Desktop Client for PC.

Runs directly on Windows/Linux/macOS with a native CustomTkinter GUI.
Connects directly to WhatsApp Multi-Device as an independent companion client.
Your phone does NOT need to remain online or awake after initial pairing.
"""

from src import compat  # noqa: F401 - Python <= 3.10 typing.Self patch

import io
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

import customtkinter as ctk
import segno
from PIL import Image

from src.client import WhatsAppClient
from src.config import SESSION_DB
from src.handlers import ChatMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("whatsapp_desktop")

# CustomTkinter styling
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")


class PairDialog(ctk.CTkToplevel):
    """Dialog for pairing PC client with phone via QR code or 8-digit code."""

    def __init__(self, parent, client: WhatsAppClient):
        super().__init__(parent)
        self.title("Link WhatsApp Device")
        self.geometry("450x520")
        self.resizable(False, False)
        self.client = client

        self.tabview = ctk.CTkTabview(self, width=410, height=470)
        self.tabview.pack(padx=20, pady=20, fill="both", expand=True)

        self.tab_qr = self.tabview.add("QR Code")
        self.tab_code = self.tabview.add("Pairing Code")

        self._setup_qr_tab()
        self._setup_code_tab()

    def _setup_qr_tab(self):
        lbl = ctk.CTkLabel(
            self.tab_qr,
            text="Open WhatsApp on your phone\nSettings -> Linked Devices -> Link a Device",
            font=("Segoe UI", 12),
            text_color="gray75",
        )
        lbl.pack(pady=10)

        self.qr_label = ctk.CTkLabel(
            self.tab_qr,
            text="Waiting for QR Code from WhatsApp...",
            width=240,
            height=240,
            fg_color="#1a1a1a",
            corner_radius=8,
        )
        self.qr_label.pack(pady=10)

        self.update_qr_from_client()

    def _setup_code_tab(self):
        lbl = ctk.CTkLabel(
            self.tab_code,
            text="Pair using your phone number without camera scan:\n(WhatsApp -> Linked Devices -> Link with phone number)",
            font=("Segoe UI", 12),
            text_color="gray75",
        )
        lbl.pack(pady=10)

        self.phone_entry = ctk.CTkEntry(
            self.tab_code,
            placeholder_text="e.g. +1234567890",
            width=280,
            height=36,
        )
        self.phone_entry.pack(pady=10)

        self.btn_gen_code = ctk.CTkButton(
            self.tab_code,
            text="Generate Pairing Code",
            command=self._generate_code,
            height=36,
        )
        self.btn_gen_code.pack(pady=5)

        self.code_display = ctk.CTkLabel(
            self.tab_code,
            text="",
            font=("Consolas", 24, "bold"),
            text_color="#25d366",
        )
        self.code_display.pack(pady=15)

    def _generate_code(self):
        phone = self.phone_entry.get().strip()
        if not phone:
            return

        self.btn_gen_code.configure(state="disabled", text="Generating...")

        def _do():
            try:
                code = self.client.request_pairing_code(phone)
                self.after(0, lambda: self._on_code_received(code))
            except Exception as e:
                self.after(0, lambda: self._on_code_error(str(e)))

        threading.Thread(target=_do, daemon=True).start()

    def _on_code_received(self, code: str):
        self.code_display.configure(text=code)
        self.btn_gen_code.configure(state="normal", text="Generate Pairing Code")

    def _on_code_error(self, err: str):
        self.code_display.configure(text="Error generating code", text_color="red")
        self.btn_gen_code.configure(state="normal", text="Generate Pairing Code")

    def update_qr_from_client(self):
        if self.client._current_qr_raw:
            self.display_qr_bytes(self.client._current_qr_raw)

    def display_qr_bytes(self, raw_bytes: bytes):
        try:
            qr = segno.make_qr(raw_bytes)
            buf = io.BytesIO()
            qr.save(buf, kind="png", scale=6)
            buf.seek(0)
            pil_img = Image.open(buf)
            ctk_img = ctk.CTkImage(light_image=pil_img, dark_image=pil_img, size=(220, 220))
            self.qr_label.configure(image=ctk_img, text="")
        except Exception as e:
            log.warning("Failed to render QR on GUI: %s", e)


class NewChatDialog(ctk.CTkToplevel):
    """Dialog to start a conversation with a new phone number."""

    def __init__(self, parent, on_start_chat):
        super().__init__(parent)
        self.title("New Chat")
        self.geometry("380x180")
        self.resizable(False, False)
        self.on_start_chat = on_start_chat

        lbl = ctk.CTkLabel(self, text="Enter recipient phone number with country code:")
        lbl.pack(pady=(20, 5))

        self.entry = ctk.CTkEntry(self, placeholder_text="e.g. 15551234567", width=260)
        self.entry.pack(pady=5)
        self.entry.focus()
        self.entry.bind("<Return>", lambda e: self._submit())

        btn = ctk.CTkButton(self, text="Start Chat", command=self._submit, width=160)
        btn.pack(pady=10)

    def _submit(self):
        phone = self.entry.get().strip().replace("+", "").replace("-", "").replace(" ", "")
        if phone:
            jid = f"{phone}@s.whatsapp.net"
            self.on_start_chat(jid, f"+{phone}")
            self.destroy()


class WhatsAppDesktopApp(ctk.CTk):
    """Main WhatsApp Desktop Application Window."""

    def __init__(self):
        super().__init__()

        self.title("WhatsApp Lite - Desktop")
        self.geometry("960x650")
        self.minsize(720, 500)

        self.client = WhatsAppClient(session_path=SESSION_DB)
        self.active_chat_jid: Optional[str] = None
        self.active_chat_title: str = ""
        self.chat_buttons: Dict[str, ctk.CTkButton] = {}
        self.pair_dialog: Optional[PairDialog] = None

        self._setup_layout()
        self._setup_client_events()

        # Start client connection in background
        self.client.start()

    def _setup_layout(self):
        # Configure grid 1x2 (Left sidebar, Right chat area)
        self.grid_columnconfigure(0, weight=0, minsize=300)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # --- Left Sidebar ---
        self.sidebar_frame = ctk.CTkFrame(self, width=300, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(2, weight=1)
        self.sidebar_frame.grid_columnconfigure(0, weight=1)

        # Header / Status
        self.brand_label = ctk.CTkLabel(
            self.sidebar_frame,
            text="WhatsApp Lite",
            font=("Segoe UI", 18, "bold"),
            anchor="w",
        )
        self.brand_label.grid(row=0, column=0, padx=16, pady=(16, 4), sticky="w")

        self.status_label = ctk.CTkLabel(
            self.sidebar_frame,
            text="Status: Connecting...",
            font=("Segoe UI", 12),
            text_color="#8696a0",
            anchor="w",
        )
        self.status_label.grid(row=1, column=0, padx=16, pady=(0, 10), sticky="w")

        # Action Buttons
        self.btn_frame = ctk.CTkFrame(self.sidebar_frame, fg_color="transparent")
        self.btn_frame.grid(row=2, column=0, padx=12, pady=(0, 10), sticky="ew")
        self.btn_frame.grid_columnconfigure(0, weight=1)
        self.btn_frame.grid_columnconfigure(1, weight=1)

        self.btn_new_chat = ctk.CTkButton(
            self.btn_frame,
            text="+ New Chat",
            command=self._open_new_chat_dialog,
            height=32,
            fg_color="#202c33",
            hover_color="#374248",
        )
        self.btn_new_chat.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self.btn_pair = ctk.CTkButton(
            self.btn_frame,
            text="🔗 Link Device",
            command=self._open_pair_dialog,
            height=32,
            fg_color="#00a884",
            hover_color="#008f6f",
        )
        self.btn_pair.grid(row=0, column=1, padx=(4, 0), sticky="ew")

        # Scrollable Chat List
        self.chat_list_frame = ctk.CTkScrollableFrame(
            self.sidebar_frame,
            label_text="Conversations",
            label_font=("Segoe UI", 12, "bold"),
        )
        self.chat_list_frame.grid(row=3, column=0, padx=8, pady=8, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(3, weight=1)

        # --- Right Main Chat Area ---
        self.main_chat_frame = ctk.CTkFrame(self, fg_color="#0b141a", corner_radius=0)
        self.main_chat_frame.grid(row=0, column=1, sticky="nsew")
        self.main_chat_frame.grid_rowconfigure(1, weight=1)
        self.main_chat_frame.grid_columnconfigure(0, weight=1)

        # Chat Header
        self.chat_header = ctk.CTkFrame(self.main_chat_frame, height=54, fg_color="#202c33", corner_radius=0)
        self.chat_header.grid(row=0, column=0, sticky="ew")
        self.chat_title_label = ctk.CTkLabel(
            self.chat_header,
            text="Select a conversation to start messaging",
            font=("Segoe UI", 15, "bold"),
        )
        self.chat_title_label.pack(side="left", padx=16, pady=12)

        # Message History Frame
        self.messages_frame = ctk.CTkScrollableFrame(self.main_chat_frame, fg_color="#0b141a")
        self.messages_frame.grid(row=1, column=0, padx=12, pady=8, sticky="nsew")

        # Chat Input Bar
        self.input_frame = ctk.CTkFrame(self.main_chat_frame, height=56, fg_color="#202c33", corner_radius=0)
        self.input_frame.grid(row=2, column=0, sticky="ew")
        self.input_frame.grid_columnconfigure(0, weight=1)

        self.message_entry = ctk.CTkEntry(
            self.input_frame,
            placeholder_text="Type a message...",
            fg_color="#2a3942",
            border_width=0,
            height=38,
            font=("Segoe UI", 13),
        )
        self.message_entry.grid(row=0, column=0, padx=(12, 8), pady=10, sticky="ew")
        self.message_entry.bind("<Return>", lambda e: self._send_message())

        self.send_btn = ctk.CTkButton(
            self.input_frame,
            text="Send",
            command=self._send_message,
            width=70,
            height=38,
            fg_color="#00a884",
            hover_color="#008f6f",
        )
        self.send_btn.grid(row=0, column=1, padx=(0, 12), pady=10)

    def _setup_client_events(self):
        self.client.add_status_listener(lambda status, state: self.after(0, self._on_status_change, status, state))
        self.client.add_qr_listener(lambda qr_b64: self.after(0, self._on_qr_received))
        self.client.store.add_listener(lambda msg: self.after(0, self._on_message_received, msg))

    def _on_status_change(self, status: str, state: Dict[str, Any]):
        if state.get("me_phone"):
            self.status_label.configure(text=f"Online (+{state['me_phone']})", text_color="#25d366")
            self.btn_pair.configure(state="disabled", text="✓ Linked")
        elif status == "connected":
            self.status_label.configure(text="Connected", text_color="#25d366")
        elif status in ("scan_qr", "pairing_code"):
            self.status_label.configure(text="Awaiting Link...", text_color="#e5a93b")
            self._open_pair_dialog()
        else:
            self.status_label.configure(text=f"Status: {status.capitalize()}", text_color="#8696a0")

    def _on_qr_received(self):
        if self.pair_dialog and self.pair_dialog.winfo_exists():
            self.pair_dialog.update_qr_from_client()

    def _on_message_received(self, msg: ChatMessage):
        # Update or create button in sidebar
        self._update_chat_item(msg.chat_jid, msg.sender_name, msg.text, msg.timestamp)

        # If currently open, render bubble
        if self.active_chat_jid == msg.chat_jid:
            self._append_message_bubble(msg)

    def _update_chat_item(self, chat_jid: str, title: str, preview: str, timestamp: int):
        clean_title = title or chat_jid.split("@")[0]
        time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M") if timestamp else ""
        btn_text = f"{clean_title}  ({time_str})\n{preview[:28]}..."

        if chat_jid in self.chat_buttons:
            self.chat_buttons[chat_jid].configure(text=btn_text)
        else:
            btn = ctk.CTkButton(
                self.chat_list_frame,
                text=btn_text,
                anchor="w",
                font=("Segoe UI", 12),
                fg_color="#182229",
                hover_color="#202c33",
                height=52,
                command=lambda j=chat_jid, t=clean_title: self._select_chat(j, t),
            )
            btn.pack(fill="x", pady=2, padx=4)
            self.chat_buttons[chat_jid] = btn

    def _select_chat(self, chat_jid: str, title: str):
        self.active_chat_jid = chat_jid
        self.active_chat_title = title
        self.chat_title_label.configure(text=title)

        # Clear existing messages
        for widget in self.messages_frame.winfo_children():
            widget.destroy()

        # Load history from store
        messages = self.client.store.get_messages(chat_jid)
        for msg_dict in messages:
            msg = ChatMessage(**msg_dict)
            self._append_message_bubble(msg)

        self.message_entry.focus()

    def _append_message_bubble(self, msg: ChatMessage):
        is_out = msg.is_from_me
        time_str = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M") if msg.timestamp else ""

        bubble_fg = "#005c4b" if is_out else "#202c33"
        align = "e" if is_out else "w"

        container = ctk.CTkFrame(self.messages_frame, fg_color="transparent")
        container.pack(fill="x", pady=4, padx=6)

        bubble = ctk.CTkFrame(container, fg_color=bubble_fg, corner_radius=10)
        bubble.pack(side="right" if is_out else "left", padx=4)

        if not is_out and msg.is_group:
            sender_lbl = ctk.CTkLabel(
                bubble,
                text=msg.sender_name,
                font=("Segoe UI", 10, "bold"),
                text_color="#00a884",
            )
            sender_lbl.pack(anchor="w", padx=10, pady=(4, 0))

        text_lbl = ctk.CTkLabel(
            bubble,
            text=msg.text,
            font=("Segoe UI", 12),
            wraplength=480,
            justify="left",
            anchor="w",
        )
        text_lbl.pack(padx=10, pady=(4, 2), anchor="w")

        time_lbl = ctk.CTkLabel(
            bubble,
            text=time_str,
            font=("Segoe UI", 9),
            text_color="gray70",
        )
        time_lbl.pack(padx=10, pady=(0, 4), anchor="e")

        # Scroll to bottom
        self.messages_frame._parent_canvas.yview_moveto(1.0)

    def _send_message(self):
        text = self.message_entry.get().strip()
        if not text or not self.active_chat_jid:
            return

        self.message_entry.delete(0, "end")
        target_jid = self.active_chat_jid

        def _do():
            try:
                self.client.send_text_message(target_jid, text)
            except Exception as e:
                log.exception("Error sending message from PC GUI: %s", e)

        threading.Thread(target=_do, daemon=True).start()

    def _open_new_chat_dialog(self):
        NewChatDialog(self, self._on_new_chat_started)

    def _on_new_chat_started(self, jid: str, title: str):
        self._update_chat_item(jid, title, "New chat", int(time.time()))
        self._select_chat(jid, title)

    def _open_pair_dialog(self):
        if self.pair_dialog is None or not self.pair_dialog.winfo_exists():
            self.pair_dialog = PairDialog(self, self.client)
        self.pair_dialog.focus()

    def destroy(self):
        log.info("Closing desktop application...")
        try:
            self.client.stop()
        except Exception:
            pass
        super().destroy()


if __name__ == "__main__":
    app = WhatsAppDesktopApp()
    app.mainloop()
