"""Application configuration settings."""

import os
from pathlib import Path

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Session database path (SQLite stores noise keys, app state & session credentials)
SESSION_DB = os.getenv("WHATSAPP_SESSION_DB", str(DATA_DIR / "session.db"))

# Server configuration
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))
DEBUG = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

# Client display identification
CLIENT_NAME = os.getenv("CLIENT_NAME", "WhatsApp Lite Web")
