"""
MeshCore Companion Protocol implementation.

This module implements the binary protocol for communicating with MeshCore devices.
"""

import struct
import time
import hashlib
import logging
from enum import IntEnum
from dataclasses import dataclass, field
from typing import Optional, Any

logger = logging.getLogger(__name__)


class Command(IntEnum):
    """MeshCore protocol commands (sent to device)."""
    APP_START = 0x01
    SEND_DIRECT_MESSAGE = 0x02
    SEND_CHANNEL_MESSAGE = 0x03
    SEND_SELF_ADVERT = 0x07
    SET_ADVERT_NAME = 0x08
    GET_CONTACTS = 0x06
    GET_MESSAGE = 0x0A
    GET_BATTERY = 0x14
    DEVICE_QUERY = 0x16
    GET_CHANNEL = 0x1F
    SET_CHANNEL = 0x20
    SET_DEVICE_TIME = 0x21
    SET_RADIO_PARAMS = 0x0B
    SET_RADIO_TX_POWER = 0x0C


class PacketType(IntEnum):
    """MeshCore response packet types (received from device)."""
    OK = 0x00
    ERROR = 0x01
    CONTACT_START = 0x02
    CONTACT = 0x03
    CONTACT_END = 0x04
    SELF_INFO = 0x05
    MSG_SENT = 0x06
    CONTACT_MSG_RECV = 0x07
    CHANNEL_MSG_RECV = 0x08
    CURRENT_TIME = 0x09
    NO_MORE_MSGS = 0x0A
    BATTERY = 0x0C
    DEVICE_INFO = 0x0D
    CONTACT_MSG_RECV_V3 = 0x10
    CHANNEL_MSG_RECV_V3 = 0x11
    CHANNEL_INFO = 0x12
    ADVERTISEMENT = 0x80
    ACK = 0x82
    MESSAGES_WAITING = 0x83
    LOG_DATA = 0x88


@dataclass
class DeviceInfo:
    """Device information."""
    fw_ver: int = 0
    max_contacts: int = 0
    max_channels: int = 8
    ble_pin: int = 0
    fw_build: str = ""
    model: str = ""
    version: str = ""


@dataclass
class SelfInfo:
    """Device self information including radio settings."""
    adv_type: int = 0
    tx_power: int = 0
    max_tx_power: int = 0
    public_key: str = ""
    adv_lat: float = 0.0
    adv_lon: float = 0.0
    name: str = ""
    radio_freq: float = 0.0
    radio_bw: float = 0.0
    radio_sf: int = 0
    radio_cr: int = 0


@dataclass
class BatteryInfo:
    """Battery status information."""
    level: int = 0
    used_kb: int = 0
    total_kb: int = 0


@dataclass
class ChannelInfo:
    """Channel information."""
    index: int = 0
    name: str = ""
    has_secret: bool = False


@dataclass
class Contact:
    """Contact information."""
    public_key: str = ""
    name: str = ""
    last_seen: int = 0
    out_path_len: int = 0
    adv_type: int = 0
    adv_name: str = ""


@dataclass
class Message:
    """Received message."""
    msg_type: str = ""  # "channel" or "contact"
    channel_idx: Optional[int] = None
    pubkey_prefix: Optional[str] = None
    path_len: int = 0
    txt_type: int = 0
    timestamp: int = 0
    text: str = ""
    snr: Optional[float] = None
    sender_name: Optional[str] = None


class MeshCoreProtocol:
    """Protocol encoder/decoder for MeshCore companion protocol."""
    
    # Public channel key (well-known)
    PUBLIC_CHANNEL_KEY = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")
    
    @staticmethod
    def build_app_start(app_name: str = "mcweb") -> bytes:
        """
        Build APP_START command.
        
        Args:
            app_name: Application name (max 9 chars).
            
        Returns:
            Command bytes.
        """
        name_bytes = app_name.encode('utf-8')[:9].ljust(9, b'\x00')
        return bytes([Command.APP_START, 0x03]) + name_bytes
    
    @staticmethod
    def build_device_query() -> bytes:
        """Build DEVICE_QUERY command."""
        return bytes([Command.DEVICE_QUERY, 0x03])
    
    @staticmethod
    def build_get_battery() -> bytes:
        """Build GET_BATTERY command."""
        return bytes([Command.GET_BATTERY])
    
    @staticmethod
    def build_get_contacts() -> bytes:
        """Build GET_CONTACTS command."""
        return bytes([Command.GET_CONTACTS])
    
    @staticmethod
    def build_get_channel(index: int) -> bytes:
        """
        Build GET_CHANNEL command.
        
        Args:
            index: Channel index (0-7).
        """
        return bytes([Command.GET_CHANNEL, index & 0x07])
    
    @staticmethod
    def build_set_channel(index: int, name: str, secret: bytes) -> bytes:
        """
        Build SET_CHANNEL command.
        
        Args:
            index: Channel index (0-7, 0 is reserved for public).
            name: Channel name (max 32 chars).
            secret: 32-byte secret (use all zeros for public channel).
            
        Returns:
            Command bytes (66 bytes total).
        """
        name_bytes = name.encode('utf-8')[:32].ljust(32, b'\x00')
        secret_bytes = secret[:32].ljust(32, b'\x00')
        return bytes([Command.SET_CHANNEL, index & 0x07]) + name_bytes + secret_bytes
    
    @staticmethod
    def build_delete_channel(index: int) -> bytes:
        """
        Build command to delete a channel (set with empty name and zero secret).
        
        Args:
            index: Channel index to delete.
        """
        return MeshCoreProtocol.build_set_channel(index, "", bytes(32))
    
    @staticmethod
    def build_send_channel_message(channel_idx: int, text: str, timestamp: Optional[int] = None) -> bytes:
        """
        Build SEND_CHANNEL_MESSAGE command.
        
        Args:
            channel_idx: Channel index (0-7).
            text: Message text (max 133 chars).
            timestamp: Unix timestamp (uses current time if not provided).
            
        Returns:
            Command bytes.
        """
        if timestamp is None:
            timestamp = int(time.time())
        
        text_bytes = text.encode('utf-8')[:133]
        
        cmd = bytearray([Command.SEND_CHANNEL_MESSAGE, 0x00, channel_idx & 0x07])
        cmd.extend(struct.pack('<I', timestamp))
        cmd.extend(text_bytes)
        return bytes(cmd)
    
    @staticmethod
    def build_send_direct_message(pubkey_prefix: bytes, text: str, timestamp: Optional[int] = None) -> bytes:
        """
        Build SEND_DIRECT_MESSAGE command.
        
        Args:
            pubkey_prefix: 6-byte public key prefix of the recipient.
            text: Message text (max 133 chars).
            timestamp: Unix timestamp (uses current time if not provided).
            
        Returns:
            Command bytes.
        """
        if timestamp is None:
            timestamp = int(time.time())
        
        text_bytes = text.encode('utf-8')[:133]
        
        cmd = bytearray([Command.SEND_DIRECT_MESSAGE, 0x00])
        cmd.extend(pubkey_prefix[:6].ljust(6, b'\x00'))
        cmd.extend(struct.pack('<I', timestamp))
        cmd.extend(text_bytes)
        return bytes(cmd)
    
    @staticmethod
    def build_get_message() -> bytes:
        """Build GET_MESSAGE command."""
        return bytes([Command.GET_MESSAGE])
    
    @staticmethod
    def build_set_device_time(timestamp: Optional[int] = None) -> bytes:
        """
        Build SET_DEVICE_TIME command.
        
        Args:
            timestamp: Unix timestamp (uses current time if not provided).
        """
        if timestamp is None:
            timestamp = int(time.time())
        
        cmd = bytearray([Command.SET_DEVICE_TIME])
        cmd.extend(struct.pack('<I', timestamp))
        return bytes(cmd)

    @staticmethod
    def build_set_advert_name(name: str) -> bytes:
        """
        Build SET_ADVERT_NAME command (companion firmware CMD 8).

        Args:
            name: Node advertisement name (max 31 UTF-8 bytes).
        """
        name_bytes = name.encode("utf-8")
        if len(name_bytes) > 31:
            raise ValueError(f"name too long ({len(name_bytes)} bytes, max 31)")
        if not name_bytes:
            raise ValueError("name must not be empty")
        return bytes([Command.SET_ADVERT_NAME]) + name_bytes

    @staticmethod
    def build_send_self_advert() -> bytes:
        """Build SEND_SELF_ADVERT command (companion firmware CMD 7)."""
        return bytes([Command.SEND_SELF_ADVERT])

    @staticmethod
    def build_set_radio_params(
        freq_mhz: float, bw_khz: float, sf: int, cr: int
    ) -> bytes:
        """
        Build SET_RADIO_PARAMS command.

        Args:
            freq_mhz: Center frequency in MHz (e.g. 910.525).
            bw_khz: Bandwidth in kHz (e.g. 62.5).
            sf: Spreading factor (5-12).
            cr: Coding rate (5-8).
        """
        freq_khz = int(round(freq_mhz * 1000))
        bw_hz = int(round(bw_khz * 1000))
        cmd = bytearray([Command.SET_RADIO_PARAMS])
        cmd.extend(struct.pack('<I', freq_khz))
        cmd.extend(struct.pack('<I', bw_hz))
        cmd.append(sf & 0xFF)
        cmd.append(cr & 0xFF)
        return bytes(cmd)

    @staticmethod
    def build_set_radio_tx_power(dbm: int) -> bytes:
        """Build SET_RADIO_TX_POWER command."""
        return bytes([Command.SET_RADIO_TX_POWER, dbm & 0xFF])

    @staticmethod
    def generate_channel_secret(is_hashtag: bool = False, hashtag_name: str = "") -> bytes:
        """
        Generate a channel secret.
        
        Args:
            is_hashtag: If True, generate hashtag channel secret from name.
            hashtag_name: Hashtag name (without #) if is_hashtag is True.
            
        Returns:
            32-byte secret for SET_CHANNEL command.
        """
        if is_hashtag:
            # Hashtag channel: SHA256 of "#name", take first 16 bytes, then expand with SHA512
            hash_input = f"#{hashtag_name}".encode('utf-8')
            secret_16 = hashlib.sha256(hash_input).digest()[:16]
        else:
            # Private channel: random 16 bytes
            import secrets
            secret_16 = secrets.token_bytes(16)
        
        # Expand to 32 bytes using SHA512
        return hashlib.sha512(secret_16).digest()[:32]
    
    @staticmethod
    def parse_response(data: bytes) -> tuple[PacketType, Any]:
        """
        Parse a response packet from the device.
        
        Args:
            data: Raw response bytes.
            
        Returns:
            Tuple of (packet_type, parsed_data).
        """
        if not data:
            return PacketType.ERROR, None
        
        packet_type = PacketType(data[0]) if data[0] in PacketType._value2member_map_ else data[0]
        
        try:
            if packet_type == PacketType.OK:
                return packet_type, MeshCoreProtocol._parse_ok(data)
            elif packet_type == PacketType.ERROR:
                return packet_type, MeshCoreProtocol._parse_error(data)
            elif packet_type == PacketType.DEVICE_INFO:
                return packet_type, MeshCoreProtocol._parse_device_info(data)
            elif packet_type == PacketType.SELF_INFO:
                return packet_type, MeshCoreProtocol._parse_self_info(data)
            elif packet_type == PacketType.BATTERY:
                return packet_type, MeshCoreProtocol._parse_battery(data)
            elif packet_type == PacketType.CHANNEL_INFO:
                return packet_type, MeshCoreProtocol._parse_channel_info(data)
            elif packet_type in (PacketType.CHANNEL_MSG_RECV, PacketType.CHANNEL_MSG_RECV_V3):
                return packet_type, MeshCoreProtocol._parse_channel_message(data)
            elif packet_type in (PacketType.CONTACT_MSG_RECV, PacketType.CONTACT_MSG_RECV_V3):
                return packet_type, MeshCoreProtocol._parse_contact_message(data)
            elif packet_type == PacketType.MSG_SENT:
                return packet_type, MeshCoreProtocol._parse_msg_sent(data)
            elif packet_type == PacketType.CONTACT:
                return packet_type, MeshCoreProtocol._parse_contact(data)
            elif packet_type == PacketType.NO_MORE_MSGS:
                return packet_type, None
            elif packet_type == PacketType.MESSAGES_WAITING:
                return packet_type, None
            elif packet_type == PacketType.ACK:
                return packet_type, MeshCoreProtocol._parse_ack(data)
            else:
                logger.debug(f"Unknown packet type: 0x{data[0]:02x}")
                return packet_type, data[1:] if len(data) > 1 else None
        except Exception as e:
            logger.exception(f"Error parsing packet type 0x{data[0]:02x}: {e}")
            return packet_type, None
    
    @staticmethod
    def _parse_ok(data: bytes) -> Optional[int]:
        """Parse PACKET_OK response."""
        if len(data) >= 5:
            return struct.unpack('<I', data[1:5])[0]
        return None
    
    @staticmethod
    def _parse_error(data: bytes) -> Optional[int]:
        """Parse PACKET_ERROR response."""
        if len(data) >= 2:
            return data[1]
        return None
    
    @staticmethod
    def _parse_device_info(data: bytes) -> DeviceInfo:
        """Parse PACKET_DEVICE_INFO response."""
        info = DeviceInfo()
        if len(data) < 2:
            return info
        
        info.fw_ver = data[1]
        
        if info.fw_ver >= 3 and len(data) >= 80:
            info.max_contacts = data[2] * 2
            info.max_channels = data[3]
            info.ble_pin = struct.unpack('<I', data[4:8])[0]
            info.fw_build = data[8:20].decode('utf-8', errors='ignore').rstrip('\x00').strip()
            info.model = data[20:60].decode('utf-8', errors='ignore').rstrip('\x00').strip()
            info.version = data[60:80].decode('utf-8', errors='ignore').rstrip('\x00').strip()
        
        return info
    
    @staticmethod
    def _parse_self_info(data: bytes) -> SelfInfo:
        """Parse PACKET_SELF_INFO response."""
        info = SelfInfo()
        if len(data) < 36:
            return info
        
        offset = 1
        info.adv_type = data[offset]
        info.tx_power = data[offset + 1]
        info.max_tx_power = data[offset + 2]
        info.public_key = data[offset + 3:offset + 35].hex()
        offset += 35
        
        if len(data) >= offset + 8:
            lat_raw = struct.unpack('<i', data[offset:offset + 4])[0]
            lon_raw = struct.unpack('<i', data[offset + 4:offset + 8])[0]
            info.adv_lat = lat_raw / 1e6
            info.adv_lon = lon_raw / 1e6
            offset += 8
        
        if len(data) >= offset + 10:
            offset += 4  # Skip multi_acks, adv_loc_policy, telemetry_mode, manual_add
            freq_raw = struct.unpack('<I', data[offset:offset + 4])[0]
            bw_raw = struct.unpack('<I', data[offset + 4:offset + 8])[0]
            info.radio_freq = freq_raw / 1000.0
            info.radio_bw = bw_raw / 1000.0
            info.radio_sf = data[offset + 8]
            info.radio_cr = data[offset + 9]
            offset += 10
        
        if offset < len(data):
            info.name = data[offset:].decode('utf-8', errors='ignore').rstrip('\x00').strip()
        
        return info
    
    @staticmethod
    def _parse_battery(data: bytes) -> BatteryInfo:
        """Parse PACKET_BATTERY response."""
        info = BatteryInfo()
        if len(data) >= 3:
            info.level = struct.unpack('<H', data[1:3])[0]
        if len(data) >= 11:
            info.used_kb = struct.unpack('<I', data[3:7])[0]
            info.total_kb = struct.unpack('<I', data[7:11])[0]
        return info
    
    @staticmethod
    def _parse_channel_info(data: bytes) -> ChannelInfo:
        """Parse PACKET_CHANNEL_INFO response."""
        info = ChannelInfo()
        if len(data) >= 2:
            info.index = data[1]
        if len(data) >= 34:
            info.name = data[2:34].decode('utf-8', errors='ignore').rstrip('\x00').strip()
            # Check if there's a non-zero secret (device typically doesn't return it)
            if len(data) >= 66:
                secret = data[34:66]
                info.has_secret = any(b != 0 for b in secret)
        return info
    
    @staticmethod
    def _parse_channel_message(data: bytes) -> Message:
        """Parse PACKET_CHANNEL_MSG_RECV or PACKET_CHANNEL_MSG_RECV_V3."""
        msg = Message(msg_type="channel")
        packet_type = data[0]
        offset = 1
        
        # V3 format has SNR
        if packet_type == PacketType.CHANNEL_MSG_RECV_V3:
            snr_byte = data[offset]
            msg.snr = (snr_byte if snr_byte < 128 else snr_byte - 256) / 4.0
            offset += 3  # SNR + 2 reserved bytes
        
        if len(data) > offset + 7:
            msg.channel_idx = data[offset]
            msg.path_len = data[offset + 1]
            msg.txt_type = data[offset + 2]
            msg.timestamp = struct.unpack('<I', data[offset + 3:offset + 7])[0]
            msg.text = data[offset + 7:].decode('utf-8', errors='ignore')
        
        return msg
    
    @staticmethod
    def _parse_contact_message(data: bytes) -> Message:
        """Parse PACKET_CONTACT_MSG_RECV or PACKET_CONTACT_MSG_RECV_V3."""
        msg = Message(msg_type="contact")
        packet_type = data[0]
        offset = 1
        
        # V3 format has SNR
        if packet_type == PacketType.CONTACT_MSG_RECV_V3:
            snr_byte = data[offset]
            msg.snr = (snr_byte if snr_byte < 128 else snr_byte - 256) / 4.0
            offset += 3  # SNR + 2 reserved bytes
        
        if len(data) > offset + 12:
            msg.pubkey_prefix = data[offset:offset + 6].hex()
            msg.path_len = data[offset + 6]
            msg.txt_type = data[offset + 7]
            msg.timestamp = struct.unpack('<I', data[offset + 8:offset + 12])[0]
            offset += 12
            
            # If txt_type == 2, skip 4-byte signature
            if msg.txt_type == 2 and len(data) > offset + 4:
                offset += 4
            
            msg.text = data[offset:].decode('utf-8', errors='ignore')
        
        return msg
    
    @staticmethod
    def _parse_msg_sent(data: bytes) -> dict:
        """Parse PACKET_MSG_SENT response."""
        result = {}
        if len(data) >= 10:
            result['msg_type'] = data[1]
            result['expected_ack'] = data[2:6].hex()
            result['suggested_timeout'] = struct.unpack('<I', data[6:10])[0]
        return result
    
    @staticmethod
    def _parse_contact(data: bytes) -> Contact:
        """Parse PACKET_CONTACT response."""
        contact = Contact()
        if len(data) < 10:
            return contact
        
        offset = 1
        contact.public_key = data[offset:offset + 6].hex()
        offset += 6
        
        # Parse remaining contact fields based on available data
        if len(data) > offset:
            contact.out_path_len = data[offset]
            offset += 1
        
        if len(data) > offset:
            contact.adv_type = data[offset]
            offset += 1
        
        # Name is variable length at the end
        if len(data) > offset:
            contact.name = data[offset:].decode('utf-8', errors='ignore').rstrip('\x00').strip()
        
        return contact
    
    @staticmethod
    def _parse_ack(data: bytes) -> str:
        """Parse PACKET_ACK response."""
        if len(data) >= 7:
            return data[1:7].hex()
        return ""
