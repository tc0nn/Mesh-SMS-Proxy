"""Shared SMS-proxy bot command handling for Meshtastic and MeshCore transports."""

from __future__ import annotations

import time
from typing import Callable, Optional, Protocol

import requests

MOC_MESHTASTIC = "0"
MOC_MESHCORE = "1"

HELP_COMMANDS = (
    "Commands: \n \n - bot: status \n - bot: help \n - bot: discord \n - bot: kofi \n"
    " - bot: weather \n - bot: weather help \n - bot: sms (dm's only) \n"
    " - bot: sms help (dm's only)"
)


class BotTransport(Protocol):
    """Callbacks supplied by each mesh stack adapter."""

    moc: str

    def send_dm(self, recipient: str, message: str) -> None: ...

    def send_broadcast(self, message: str) -> None: ...

    def get_sender_location(self, sender_id: str) -> tuple[str, str]: ...

    def request_position(self, sender_id: str) -> None: ...


class SmsProxyBot:
    """Parse inbound text and drive email-service HTTP calls."""

    def __init__(
        self,
        transport: BotTransport,
        *,
        email_host: str = "localhost",
        email_port: int = 5000,
        device_id_for: Optional[Callable[[str], str]] = None,
    ):
        self.transport = transport
        self.email_host = email_host
        self.email_port = email_port
        self._device_id_for = device_id_for or (lambda sender: sender)

    def _email_url(self, path: str) -> str:
        return f"http://{self.email_host}:{self.email_port}{path}"

    def _reply(self, is_dm: bool, recipient: str, message: str) -> None:
        if is_dm:
            self.transport.send_dm(recipient, message)
        else:
            self.transport.send_broadcast(message)

    def _fetch_weather(
        self, zipcode: int, gps_x: str, gps_y: str
    ) -> Optional[dict]:
        payload = {"zipcode": zipcode, "gps_x": gps_x, "gps_y": gps_y}
        try:
            response = requests.post(
                self._email_url("/get-weather"), json=payload, timeout=15
            )
            if response.status_code != 200:
                return None
            return response.json()
        except requests.RequestException as exc:
            print(f"Weather request failed: {exc}")
            return None

    def _format_weather(self, data: dict) -> str:
        temp_f = round((data["main"]["temp"] - 273.15) * 1.8 + 32)
        return (
            f"Current weather in your area is: {temp_f} degrees F with a humidity of "
            f"{data['main']['humidity']}% and {data['weather'][0]['description']}"
        )

    def _handle_weather(self, text: str, is_dm: bool, recipient: str, sender_id: str) -> None:
        words = text.lower().split()
        if "weather" not in words:
            return
        if "bot: weather help" in text.lower():
            self._reply(
                is_dm,
                recipient,
                "You can both run the command [bot: weather] or [bot: weather [your zip code]] "
                "to get the current weather in your area",
            )
            return
        if "bot: weather" not in text.lower():
            return

        index = words.index("weather") + 1
        if index < len(words) and words[index].isdigit():
            zipcode = int(words[index])
            data = self._fetch_weather(zipcode, "0", "0")
            if data:
                self._reply(is_dm, recipient, self._format_weather(data))
            else:
                self._reply(is_dm, recipient, "Failed to fetch weather data")
            return

        gps_x, gps_y = self.transport.get_sender_location(sender_id)
        if gps_x != "0" or gps_y != "0":
            data = self._fetch_weather(0, gps_x, gps_y)
            if data:
                self._reply(is_dm, recipient, self._format_weather(data))
            else:
                self._reply(is_dm, recipient, "Failed to fetch weather data")
            return

        self.transport.request_position(sender_id)
        self._reply(
            is_dm,
            recipient,
            "Warning: Your current location could not be retrieved in time. "
            "Please provide your Zipcode manually.",
        )

    def _send_sms_email(
        self,
        *,
        phone_number: str,
        message: str,
        sender_id: str,
        gps_x: str,
        gps_y: str,
        cellular_provider: str,
    ) -> tuple[bool, str]:
        data = {
            "phone_number": phone_number,
            "message": message,
            "device_id": self._device_id_for(sender_id),
            "gps_x": gps_x,
            "gps_y": gps_y,
            "moc": self.transport.moc,
            "celluar_provider": cellular_provider,
        }
        try:
            response = requests.post(
                self._email_url("/send-email"), json=data, timeout=30
            )
            if response.status_code == 200:
                return True, f"Message sent successfully to {phone_number}"
            return False, f"Error sending Message to {phone_number}: {response.text}"
        except requests.RequestException as exc:
            return False, f"Error sending Message to {phone_number}: {exc}"

    def handle_message(self, *, text: str, is_dm: bool, sender_id: str, dm_recipient: str) -> None:
        """Process one inbound text message."""
        text_content = text or ""
        lower = text_content.lower()
        recipient = dm_recipient if is_dm else ""

        print(
            f"Received {'DM' if is_dm else 'Broadcast'} from {sender_id}: {text_content}"
        )

        if is_dm:
            if "bot: status" in lower:
                self.transport.send_dm(recipient, "Status: Online and listening :3")

            if "bot: kofi" in lower:
                self.transport.send_dm(
                    recipient,
                    "Support the Lousiana Mesh Community and get some cute stickers here: "
                    "https://ko-fi.com/louisianameshcommunity/shop",
                )

            if "bot: help" in lower:
                time.sleep(3)
                self.transport.send_dm(
                    recipient,
                    "Hello, This bot is provided by the Louisiana Mesh Community",
                )
                time.sleep(3)
                self.transport.send_dm(recipient, HELP_COMMANDS)
                time.sleep(5)
                self.transport.send_dm(
                    recipient,
                    "This bot is still a work in progess, all commands can be sent to either "
                    "DM or main channel, if you have any bugs please send them in the discord >~<",
                )

            if "bot: discord" in lower:
                self.transport.send_dm(
                    recipient,
                    "Join the Louisiana Mesh Community Discord here: https://discord.LouisianaMesh.org",
                )

            self._handle_weather(text_content, True, recipient, sender_id)

            if lower == "bot: sms help":
                self.transport.send_dm(
                    recipient,
                    "Please send the following information structured as seen here: "
                    "Phone number,, Yes/No if you'd like to share you loction,, "
                    "Cellular Provider,, Message",
                )
            elif "bot: sms" in lower:
                idx = lower.index("bot: sms")
                sms_body = text_content[idx + len("bot: sms") :].strip()
                parts = sms_body.split(",, ")
                if len(parts) == 4:
                    phone_number, loc_flag, cellular_provider, message = parts
                    send_location = loc_flag.lower() == "yes"
                    gps_x, gps_y = "0", "0"
                    if send_location:
                        gps_x, gps_y = self.transport.get_sender_location(sender_id)
                    ok, reply = self._send_sms_email(
                        phone_number=phone_number,
                        message=message,
                        sender_id=sender_id,
                        gps_x=gps_x,
                        gps_y=gps_y,
                        cellular_provider=cellular_provider,
                    )
                    print(f" >> SMS result for {sender_id}: {reply}")
                    self.transport.send_dm(recipient, reply)
                else:
                    self.transport.send_dm(
                        recipient,
                        "The message format is incorrect. Please try again or run "
                        "[bot: sms help] for more information.",
                    )

            if "good girl" in lower:
                self.transport.send_dm(recipient, "aw, ty >~<")
            if ":3" in lower:
                self.transport.send_dm(recipient, ":3")
            if lower == "ping":
                self.transport.send_dm(recipient, "pong")
            return

        # Broadcast / public channel
        if "bot: help" in lower:
            time.sleep(3)
            self.transport.send_broadcast(
                "Hello, This bot is provided by the Louisiana Mesh Community"
            )
            time.sleep(3)
            self.transport.send_broadcast(HELP_COMMANDS)
            time.sleep(5)
            self.transport.send_broadcast(
                "This bot is still a work in progess, all commands can be sent to either "
                "DM or main channel, if you have any bugs please send them in the discord >~<",
            )

        if "bot: status" in lower:
            self.transport.send_broadcast("Status: Online and listening :3")

        if "bot: discord" in lower:
            self.transport.send_broadcast(
                "Join the Louisiana Mesh Community Discord here: https://discord.LouisianaMesh.org"
            )

        if "bot: sms help" in lower or "bot: sms" in lower:
            self.transport.send_broadcast("SMS commands can only be used in DM's")

        if "bot: kofi" in lower:
            self.transport.send_broadcast(
                "Support the Lousiana Mesh Community and get some cute stickers here: "
                "https://ko-fi.com/louisianameshcommunity/shop"
            )

        if lower == "good girl ><":
            self.transport.send_broadcast("aw, ty >~<")
        if ":3" in lower:
            self.transport.send_broadcast(":3")

        self._handle_weather(text_content, False, "", sender_id)

        if lower == "ping":
            self.transport.send_broadcast("pong")
