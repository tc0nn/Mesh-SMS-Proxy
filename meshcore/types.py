"""Lightweight types for MeshCore relay (no Room Server CLI dependency)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class CLIMessage:
    """A received message from the device."""

    sender: str = ""
    text: str = ""
    channel: str = ""
    timestamp: str = ""
    msg_type: str = "received"
    snr: Optional[float] = None
    hops: Optional[int] = None
    pubkey_prefix: str = ""


@dataclass
class CLIChannel:
    """A channel discovered from received messages."""

    name: str = ""
    last_message: str = ""
    message_count: int = 0
