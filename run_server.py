"""Entry point to run the WhatsApp Lite Mobile Web Client."""

from src import compat  # noqa: F401 - Patch typing.Self for Python <= 3.10
import logging
import uvicorn
from src.config import HOST, PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

if __name__ == "__main__":
    print(f"[*] Starting WhatsApp Lite Web Client on http://{HOST}:{PORT}")
    print(f"[*] To use on phone, open http://<YOUR_LOCAL_IP>:{PORT} in mobile browser")
    print("[*] Or install as PWA by choosing 'Add to Home Screen'")
    uvicorn.run("src.server:app", host=HOST, port=PORT, reload=False)
