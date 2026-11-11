"""Command-line interface for the WhatsApp Client."""

import argparse
import sys
import time
from src import compat  # noqa: F401 - Python <= 3.10 typing.Self patch
from src.client import WhatsAppClient
from src.config import SESSION_DB
from src.handlers import ChatMessage


def cmd_status(args):
    client = WhatsAppClient(session_path=SESSION_DB)
    print(f"Session Database : {SESSION_DB}")
    print(f"Logged In        : {client.is_logged_in}")
    print(f"Connected        : {client.is_connected}")
    if client.is_logged_in:
        print("\nSession is active. Starting client will reconnect automatically without QR scanning.")
    else:
        print("\nNo active session found. Run 'python cli.py login' or use the Web UI to link a device.")


def cmd_login(args):
    print("[*] Starting WhatsApp client in QR code login mode...")
    print("[*] Press Ctrl+C to cancel.\n")
    client = WhatsAppClient(session_path=SESSION_DB)

    def on_status(status, state):
        print(f"Status update: {status}")
        if status == "connected":
            print(f"\n[+] Successfully authenticated as: {state.get('me_phone') or state.get('me_jid')}")
            print("[+] Session saved to SQLite. Future runs will not require login.")
            sys.exit(0)

    client.add_status_listener(on_status)
    client.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Exiting...")
        client.stop()


def cmd_pair(args):
    phone = args.phone.strip()
    print(f"[*] Requesting 8-digit pairing code for: {phone}")
    client = WhatsAppClient(session_path=SESSION_DB)

    def on_status(status, state):
        if status == "connected":
            print(f"\n[+] Device successfully paired and connected! Phone: {state.get('me_phone')}")
            sys.exit(0)

    client.add_status_listener(on_status)
    client.start()

    time.sleep(2)  # Wait for gateway initialization
    try:
        code = client.request_pairing_code(phone)
        print("\n" + "=" * 45)
        print(f"  PAIRING CODE:  {code}")
        print("=" * 45)
        print("\nOn your phone:")
        print("1. Open WhatsApp -> Settings -> Linked Devices")
        print("2. Tap 'Link a Device'")
        print("3. Tap 'Link with phone number instead'")
        print("4. Enter the 8-character code shown above.")
        print("\nWaiting for authentication...")

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Operation cancelled.")
        client.stop()


def cmd_send(args):
    to = args.to
    text = " ".join(args.message)
    print(f"[*] Sending message to {to}...")

    client = WhatsAppClient(session_path=SESSION_DB)
    if not client.is_logged_in:
        print("[-] Error: Client is not logged in. Link device first using 'python cli.py login' or 'pair'.")
        return

    connected_event = time.time()
    client.start()

    # Wait until connected
    print("[*] Connecting to WhatsApp network...")
    for _ in range(30):
        if client.is_connected:
            break
        time.sleep(0.5)

    if not client.is_connected:
        print("[-] Timed out waiting for connection.")
        client.stop()
        return

    try:
        msg = client.send_text_message(to, text)
        print(f"[+] Message successfully sent! ID: {msg.id}")
    except Exception as e:
        print(f"[-] Failed to send message: {e}")
    finally:
        client.stop()


def cmd_listen(args):
    print("[*] Starting WhatsApp event listener...")
    client = WhatsAppClient(session_path=SESSION_DB)

    def on_msg(msg: ChatMessage):
        direction = "OUT" if msg.is_from_me else "IN"
        print(f"[{direction}] [{msg.sender_name}] {msg.text}")

    client.store.add_listener(on_msg)
    client.start()

    print("[*] Listening for incoming messages. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Stopping listener...")
        client.stop()


def main():
    parser = argparse.ArgumentParser(description="WhatsApp Multi-Device Lightweight Client CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # status
    subparsers.add_parser("status", help="Check session and connection status")

    # login (QR)
    subparsers.add_parser("login", help="Connect and display QR code in terminal")

    # pair (phone number)
    pair_parser = subparsers.add_parser("pair", help="Pair device using an 8-character phone code")
    pair_parser.add_argument("phone", help="Phone number with country code (e.g. +1234567890)")

    # send
    send_parser = subparsers.add_parser("send", help="Send a message to a phone number or JID")
    send_parser.add_argument("to", help="Target phone number or JID")
    send_parser.add_argument("message", nargs="+", help="Message body")

    # listen
    subparsers.add_parser("listen", help="Stream incoming messages to stdout")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "status": cmd_status,
        "login": cmd_login,
        "pair": cmd_pair,
        "send": cmd_send,
        "listen": cmd_listen,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
