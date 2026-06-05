#!/usr/bin/env python3
"""MeshCore companion USB bot for Mesh-SMS-proxy (parallel to Meshtastic service)."""

from __future__ import annotations

import json
import os
import sys
import time

from bot_logic import MOC_MESHCORE, SmsProxyBot
from meshcore.relay_device import MeshCoreRelayDevice
from meshcore.types import CLIMessage


def _load_config() -> dict:
    path = os.environ.get("SMS_PROXY_CONFIG", "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return {}


EMAIL_HOST = os.environ.get("EMAIL_SERVICE_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_SERVICE_PORT", "5000"))
SERIAL_PORT = os.environ.get("MESHCORE_SERIAL", "")
BAUD = int(os.environ.get("MESHCORE_BAUD", "115200"))


class MeshCoreTransport:
    moc = MOC_MESHCORE

    def __init__(self, device: MeshCoreRelayDevice):
        self.device = device

    def send_dm(self, recipient: str, message: str) -> None:
        print(f"Sending DM to {recipient}: {message}")
        self.device.send_direct_message(recipient, message)
        time.sleep(3)

    def send_broadcast(self, message: str) -> None:
        print(f"Broadcasting: {message}")
        self.device.send_public_message(message, channel="public")
        time.sleep(3)

    def get_sender_location(self, sender_id: str) -> tuple[str, str]:
        # MeshCore companion does not cache per-node GPS like Meshtastic; use bot advert.
        return self.device.device_advert_location()

    def request_position(self, sender_id: str) -> None:
        print(f"Position not available for MeshCore sender {sender_id}; use zip code.")


def _dm_recipient(message: CLIMessage) -> str:
    sender = (message.sender or "").strip()
    pk = (message.pubkey_prefix or "").strip()
    if sender and pk and len(pk) >= 4:
        short = pk[:4].upper()
        if short not in sender.upper():
            return f"{sender} {short}"
    if pk and len(pk) >= 12:
        return pk[:12]
    return sender or pk


def _is_direct(message: CLIMessage) -> bool:
    channel = (message.channel or "").strip().lower()
    return channel in ("direct", "dm")


def main() -> int:
    cfg = _load_config()
    port = SERIAL_PORT or cfg.get("meshcore_serial", "") or cfg.get("serial_port", "")
    if not port:
        port = "/dev/ttyACM0"
    email_host = cfg.get("email_service_host", EMAIL_HOST)
    email_port = int(cfg.get("email_service_port", EMAIL_PORT))

    print(f"Initializing MeshCore SMS bot on {port}...")
    device = MeshCoreRelayDevice(port, BAUD)
    if not device.connect():
        print("Failed to connect to MeshCore companion device", file=sys.stderr)
        return 1

    identity = device.node_identity()
    print(f"Connected to node: {identity}")

    transport = MeshCoreTransport(device)

    def device_id_for(sender: str) -> str:
        si = device.state.self_info
        pk = (si.public_key[:12] if si and si.public_key else "")
        if pk:
            return f"{sender} [{pk}]"
        return sender

    bot = SmsProxyBot(
        transport,
        email_host=email_host,
        email_port=email_port,
        device_id_for=device_id_for,
    )

    def on_message(message: CLIMessage):
        if (message.msg_type or "").strip().lower() == "sent":
            return
        text = message.text or ""
        if not text.strip():
            return
        is_dm = _is_direct(message)
        sender = (message.sender or message.pubkey_prefix or "unknown").strip()
        bot.handle_message(
            text=text,
            is_dm=is_dm,
            sender_id=sender,
            dm_recipient=_dm_recipient(message) if is_dm else "",
        )

    device.set_message_callback(on_message)

    print("MeshCore bot is running. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
        device.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
