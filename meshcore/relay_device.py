"""Companion USB MeshCore device with send/list helpers for SMS proxy bot."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Callable, Optional

from .device import MeshCoreDevice
from .protocol import Message
from .types import CLIChannel, CLIMessage

logger = logging.getLogger(__name__)


class RelayContact:
    __slots__ = ("name", "prefix")

    def __init__(self, name: str, prefix: str):
        self.name = name
        self.prefix = prefix


class MeshCoreRelayDevice(MeshCoreDevice):
    """Binary Companion device with relay conveniences."""

    companion_relay = True

    def __init__(self, port: str, baudrate: int = 115200):
        super().__init__(port, baudrate)
        self._channel_callback: Optional[Callable[[CLIChannel], None]] = None
        self._user_message_callback: Optional[Callable[[CLIMessage], None]] = None
        self._channel_stats: dict[str, CLIChannel] = {}
        self._startup_channels_emitted = False

    def set_channel_callback(self, callback: Optional[Callable[[CLIChannel], None]]):
        self._channel_callback = callback

    def set_message_callback(self, callback: Optional[Callable[[CLIMessage], None]]):
        self._user_message_callback = callback
        super().set_message_callback(self._on_protocol_message)

    def _on_protocol_message(self, m: Message):
        self._touch_channel_stats(m)
        if self._user_message_callback:
            try:
                self._user_message_callback(self._to_cli_message(m))
            except Exception as exc:
                logger.exception("Relay message callback error: %s", exc)

    def _channel_stats_key(self, display_name: str) -> str:
        d = (display_name or "").strip().lower()
        return "public" if d in ("", "public", "default") else d

    def _touch_channel_stats(self, m: Message):
        display = self._public_channel_label(m)
        if not display:
            return
        key = self._channel_stats_key(display)
        ch = self._channel_stats.get(key)
        if ch:
            ch.message_count += 1
            ch.last_message = m.text or ""
        else:
            self._channel_stats[key] = CLIChannel(
                name=display,
                last_message=m.text or "",
                message_count=1,
            )
            if self._channel_callback and self._startup_channels_emitted:
                try:
                    self._channel_callback(self._channel_stats[key])
                except Exception as exc:
                    logger.exception("Channel callback error: %s", exc)

    def _public_channel_label(self, m: Message) -> str:
        if m.msg_type != "channel":
            return ""
        idx = m.channel_idx
        if idx is None:
            return "public"
        ch = self.state.channels.get(idx)
        raw = (ch.name if ch else "").strip().lower()
        if idx == 0 or raw in ("", "public", "default"):
            return "public"
        return (ch.name if ch else "").strip() or "public"

    def _to_cli_message(self, m: Message) -> CLIMessage:
        ts = ""
        if m.timestamp:
            try:
                ts = datetime.fromtimestamp(int(m.timestamp)).strftime("%H:%M:%S")
            except (OSError, ValueError, OverflowError):
                ts = str(m.timestamp)

        if m.msg_type == "contact":
            channel = "direct"
            sender = (m.sender_name or (m.pubkey_prefix or "")[:12] or "").strip()
        else:
            channel = self._channel_stats_key(self._public_channel_label(m))
            sender = (m.sender_name or "").strip()

        pk = (m.pubkey_prefix or "").strip().lower()
        return CLIMessage(
            sender=sender,
            text=m.text or "",
            channel=channel,
            timestamp=ts,
            msg_type="received",
            snr=m.snr,
            hops=m.path_len if m.path_len else None,
            pubkey_prefix=pk,
        )

    def connect(self) -> bool:
        ok = super().connect()
        if not ok:
            return False
        self._rebuild_channel_stats_from_state()
        self._startup_channels_emitted = True
        return True

    def _rebuild_channel_stats_from_state(self):
        self._channel_stats.clear()
        self._channel_stats["public"] = CLIChannel(name="public", last_message="", message_count=0)
        for ch in self.get_channels():
            name = (ch.name or "").strip() or f"ch{ch.index}"
            key = self._channel_stats_key(name)
            self._channel_stats[key] = CLIChannel(name=name, last_message="", message_count=0)

    def send_public_message(self, text: str, channel: Optional[str] = None) -> bool:
        if not text or not str(text).strip():
            return False
        text = str(text).strip()
        raw = (channel or "").strip()
        key = raw.lower()
        if key in ("", "public", "default"):
            return self.send_channel_message(0, text)

        name = raw.lstrip("#").strip()
        if not name:
            return False
        idx = self._channel_index_by_name(name)
        if idx is None:
            logger.warning("send_public_message: unknown channel name %r", name)
            return False
        return self.send_channel_message(idx, text)

    def _channel_index_by_name(self, name: str) -> Optional[int]:
        want = name.strip().lower()
        for ch in self.get_channels():
            if ch.name.strip().lower() == want:
                return ch.index
        return None

    def send_direct_message(self, recipient: str, text: str) -> bool:
        prefix = self._resolve_pubkey_prefix(recipient)
        if not prefix:
            return False
        return super().send_direct_message(prefix, text)

    def _resolve_pubkey_prefix(self, recipient: str) -> Optional[str]:
        r = (recipient or "").strip()
        if not r:
            return None
        hexish = "".join(r.split()).lower()
        if all(c in "0123456789abcdef" for c in hexish) and len(hexish) >= 12:
            return hexish[:12]
        label = re.match(r"^(.+?)\s+([0-9a-fA-F]{4,12})$", r)
        if label:
            pk = re.sub(r"[^0-9a-f]", "", label.group(2).lower())
            if len(pk) >= 4:
                if len(pk) < 12:
                    pk = pk.ljust(12, "0")
                return pk[:12]
        rl = r.lower()
        for c in self.state.contacts:
            if not c.name:
                continue
            if c.name.strip().lower() == rl:
                pk = (c.public_key or "").lower()
                if len(pk) >= 12:
                    return pk[:12]
                if len(pk) >= 6:
                    return pk.ljust(12, "0")[:12]
        logger.warning("Could not resolve DM recipient %r to a 6-byte pubkey prefix", recipient)
        return None

    def device_advert_location(self) -> tuple[str, str]:
        """Return this node's advertised lat/lon as strings, or 0,0."""
        si = self.state.self_info
        if si and si.adv_lat and si.adv_lon:
            return str(si.adv_lat), str(si.adv_lon)
        return "0", "0"

    def node_identity(self) -> str:
        si = self.state.self_info
        if si and si.name:
            return si.name
        if si and si.public_key:
            return si.public_key[:12]
        return "meshcore"
