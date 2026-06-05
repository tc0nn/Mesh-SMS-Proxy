#!/usr/bin/env python3
"""Meshtastic bot for Mesh-SMS-proxy."""

from __future__ import annotations

import json
import os
import sys
import time

import meshtastic.serial_interface
import meshtastic.tcp_interface
from pubsub import pub

from bot_logic import MOC_MESHTASTIC, SmsProxyBot

USE_SERIAL = os.environ.get("MESHTASTIC_USE_SERIAL", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
TCP_HOST = os.environ.get("MESHTASTIC_TCP_HOST", "localhost")


def _load_config() -> dict:
    path = os.environ.get("SMS_PROXY_CONFIG", "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        return {}


class MeshtasticTransport:
    moc = MOC_MESHTASTIC

    def __init__(self, interface):
        self.interface = interface

    def send_dm(self, recipient: str, message: str) -> None:
        print(f"Sending DM to {recipient}: {message}")
        self.interface.sendText(message, destinationId=recipient)
        time.sleep(3)

    def send_broadcast(self, message: str) -> None:
        print(f"Broadcasting: {message}")
        self.interface.sendText(message)
        time.sleep(3)

    def get_sender_location(self, sender_id: str) -> tuple[str, str]:
        remote_node = self.interface.nodes.get(sender_id)
        if remote_node and "position" in remote_node and remote_node["position"]:
            position_data = remote_node["position"]
            gps_x = str(position_data["latitudeI"] / 10000000.0)
            gps_y = str(position_data["longitudeI"] / 10000000.0)
            print(f"Location found in database: Lat {gps_x}, Lon {gps_y}")
            return gps_x, gps_y
        return "0", "0"

    def request_position(self, sender_id: str) -> None:
        print(f"Location not in database. Sending position request to {sender_id}...")
        self.interface.sendPosition(destinationId=sender_id)


class MeshBot:
    def __init__(self):
        cfg = _load_config()
        email_host = cfg.get("email_service_host", os.environ.get("EMAIL_SERVICE_HOST", "localhost"))
        email_port = int(
            cfg.get("email_service_port", os.environ.get("EMAIL_SERVICE_PORT", "5000"))
        )

        print("Initializing Meshtastic MeshBot...")
        try:
            if USE_SERIAL:
                self.interface = meshtastic.serial_interface.SerialInterface()
            else:
                self.interface = meshtastic.tcp_interface.TCPInterface(hostname=TCP_HOST)
        except Exception as exc:
            print(f"Error connecting to device: {exc}")
            sys.exit(1)

        node = self.interface.getMyNodeInfo()
        print(f"Connected to node: {node['user']['longName']}")
        print(f"Node ID: {node['user']['id']}")

        self.transport = MeshtasticTransport(self.interface)
        self.bot = SmsProxyBot(
            self.transport,
            email_host=email_host,
            email_port=email_port,
        )
        pub.subscribe(self.on_receive, "meshtastic.receive.text")

    def on_receive(self, packet, interface):
        try:
            sender_id = packet.get("fromId")
            text_content = packet.get("decoded", {}).get("text", "")
            to_id = packet.get("toId")
            my_id = interface.getMyNodeInfo()["user"]["id"]
            is_dm = to_id == my_id
            self.bot.handle_message(
                text=text_content,
                is_dm=is_dm,
                sender_id=sender_id,
                dm_recipient=sender_id if is_dm else "",
            )
        except KeyError:
            print("Error parsing packet fields.")

    def run(self):
        print("Bot is running. Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down...")
            self.interface.close()


if __name__ == "__main__":
    MeshBot().run()
