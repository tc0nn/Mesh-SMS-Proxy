"""
High-level MeshCore device API.

Provides a simple interface for communicating with MeshCore devices.
"""

import threading
import time
import logging
from typing import Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

from .serial_interface import SerialInterface, list_serial_ports
from .protocol import (
    MeshCoreProtocol, PacketType, Command,
    DeviceInfo, SelfInfo, BatteryInfo, ChannelInfo, Contact, Message,
)

logger = logging.getLogger(__name__)


@dataclass
class DeviceState:
    """Current state of the connected device."""
    connected: bool = False
    device_info: Optional[DeviceInfo] = None
    self_info: Optional[SelfInfo] = None
    battery: Optional[BatteryInfo] = None
    channels: dict[int, ChannelInfo] = field(default_factory=dict)
    contacts: list[Contact] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    last_update: float = 0


class MeshCoreDevice:
    """
    High-level API for MeshCore device communication.
    
    Provides methods for sending/receiving messages, managing channels,
    and querying device information.
    """
    
    def __init__(self, port: str, baudrate: int = 115200):
        """
        Initialize the device interface.
        
        Args:
            port: Serial port path.
            baudrate: Baud rate (default 115200).
        """
        self._serial = SerialInterface(port, baudrate)
        self._state = DeviceState()
        self._message_callback: Optional[Callable[[Message], None]] = None
        self._status_callback: Optional[Callable[[DeviceState], None]] = None
        self._heard_frame_callback: Optional[Callable[[PacketType, bytes], None]] = None
        self._polling_thread: Optional[threading.Thread] = None
        self._polling = False
        self._lock = threading.Lock()
        self._contacts_loading = False
        self._pending_contacts: list[Contact] = []
    
    @property
    def state(self) -> DeviceState:
        """Get current device state."""
        return self._state
    
    @property
    def is_connected(self) -> bool:
        """Check if device is connected."""
        return self._serial.is_connected()
    
    def set_message_callback(self, callback: Optional[Callable[[Message], None]]):
        """Set callback for incoming messages."""
        self._message_callback = callback
    
    def set_status_callback(self, callback: Optional[Callable[[DeviceState], None]]):
        """Set callback for status updates."""
        self._status_callback = callback

    def set_heard_frame_callback(
        self, callback: Optional[Callable[[PacketType, bytes], None]]
    ):
        """Set callback for async frames (e.g. LOG_DATA packet captures)."""
        self._heard_frame_callback = callback
    
    def connect(self) -> bool:
        """
        Connect to the device and initialize.
        
        Returns:
            True if successful, False otherwise.
        """
        if not self._serial.connect():
            return False

        # Companion USB can take several seconds after reset before accepting frames.
        time.sleep(5.0)

        # Set up frame callback for async responses
        self._serial.set_callback(self._on_frame_received)
        
        # Send APP_START command (Companion firmware answers with SELF_INFO, not OK)
        self._serial.clear_queue()
        time.sleep(0.3)
        if not self._send_and_wait(
            MeshCoreProtocol.build_app_start(),
            PacketType.SELF_INFO,
            timeout=15.0,
        ):
            logger.error("APP_START failed (no SELF_INFO response on serial)")
            self._serial.disconnect()
            return False
        
        # Query device info
        self._send_and_wait(MeshCoreProtocol.build_device_query(), PacketType.DEVICE_INFO)
        
        # Set device time
        self._serial.write_frame(MeshCoreProtocol.build_set_device_time())
        time.sleep(0.1)
        
        # Get battery status
        self.get_battery()
        
        # Get all channels
        self.refresh_channels()
        
        # Get contacts
        self.refresh_contacts()
        
        self._state.connected = True
        self._state.last_update = time.time()
        
        # Start polling for messages
        self.start_polling()
        
        logger.info("Device connected and initialized")
        return True
    
    def disconnect(self):
        """Disconnect from the device."""
        self.stop_polling()
        self._serial.disconnect()
        self._state.connected = False
    
    def _send_and_wait(
        self,
        command: bytes,
        expected_type: PacketType,
        timeout: float = 5.0,
        also_accept: tuple[PacketType, ...] = (),
    ) -> Optional[Any]:
        """
        Send a command and wait for a specific response type.
        
        Args:
            command: Command bytes to send.
            expected_type: Expected response packet type.
            timeout: Timeout in seconds.
            also_accept: Additional packet types that satisfy the wait.
            
        Returns:
            Parsed response data, or None if failed/timeout.
        """
        accepted = (expected_type,) + also_accept
        self._serial.clear_queue()
        if not self._serial.write_frame(command):
            return None
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            frame = self._serial.read_frame(timeout=0.5)
            if frame:
                pkt_type, data = MeshCoreProtocol.parse_response(frame)
                
                # Update state based on response
                self._update_state(pkt_type, data)
                
                if pkt_type in accepted:
                    return data
                elif pkt_type == PacketType.ERROR:
                    logger.error(f"Command error: {data}")
                    return None
        
        logger.warning(f"Timeout waiting for {expected_type.name}")
        return None

    def _send_without_error(self, command: bytes, timeout: float = 2.0) -> bool:
        """Send a command; return True unless an ERROR response arrives quickly."""
        self._serial.clear_queue()
        if not self._serial.write_frame(command):
            return False
        start_time = time.time()
        while time.time() - start_time < timeout:
            frame = self._serial.read_frame(timeout=0.25)
            if not frame:
                continue
            pkt_type, data = MeshCoreProtocol.parse_response(frame)
            self._update_state(pkt_type, data)
            if pkt_type == PacketType.ERROR:
                logger.error("Command error: %s", data)
                return False
            if pkt_type in (PacketType.MSG_SENT, PacketType.OK):
                return True
        return True
    
    def _on_frame_received(self, frame: bytes):
        """Callback for asynchronously received frames."""
        pkt_type, data = MeshCoreProtocol.parse_response(frame)
        self._update_state(pkt_type, data)

        if self._heard_frame_callback and pkt_type in (
            PacketType.LOG_DATA,
            PacketType.ADVERTISEMENT,
        ):
            try:
                self._heard_frame_callback(pkt_type, frame)
            except Exception as e:
                logger.exception("Heard frame callback error: %s", e)

        # Handle messages waiting notification
        if pkt_type == PacketType.MESSAGES_WAITING:
            self._fetch_pending_messages()
    
    def _update_state(self, pkt_type: PacketType, data: Any):
        """Update device state based on received packet."""
        with self._lock:
            if pkt_type == PacketType.DEVICE_INFO and isinstance(data, DeviceInfo):
                self._state.device_info = data
            
            elif pkt_type == PacketType.SELF_INFO and isinstance(data, SelfInfo):
                self._state.self_info = data
            
            elif pkt_type == PacketType.BATTERY and isinstance(data, BatteryInfo):
                self._state.battery = data
            
            elif pkt_type == PacketType.CHANNEL_INFO and isinstance(data, ChannelInfo):
                self._state.channels[data.index] = data
            
            elif pkt_type == PacketType.CONTACT_START:
                self._contacts_loading = True
                self._pending_contacts = []
            
            elif pkt_type == PacketType.CONTACT and isinstance(data, Contact):
                if self._contacts_loading:
                    self._pending_contacts.append(data)
                else:
                    # Single contact update
                    self._state.contacts.append(data)
            
            elif pkt_type == PacketType.CONTACT_END:
                self._state.contacts = self._pending_contacts
                self._pending_contacts = []
                self._contacts_loading = False
            
            elif pkt_type in (PacketType.CHANNEL_MSG_RECV, PacketType.CHANNEL_MSG_RECV_V3,
                              PacketType.CONTACT_MSG_RECV, PacketType.CONTACT_MSG_RECV_V3):
                if isinstance(data, Message):
                    # Add sender name if it's a contact message
                    if data.msg_type == "contact" and data.pubkey_prefix:
                        for contact in self._state.contacts:
                            if contact.public_key.startswith(data.pubkey_prefix):
                                data.sender_name = contact.name
                                break
                    
                    # Add to message list (keep last 100)
                    self._state.messages.append(data)
                    if len(self._state.messages) > 100:
                        self._state.messages = self._state.messages[-100:]
                    
                    # Trigger callback
                    if self._message_callback:
                        try:
                            self._message_callback(data)
                        except Exception as e:
                            logger.exception(f"Message callback error: {e}")
            
            self._state.last_update = time.time()
        
        # Trigger status callback
        if self._status_callback and pkt_type in (
            PacketType.DEVICE_INFO, PacketType.BATTERY, PacketType.CONTACT_END
        ):
            try:
                self._status_callback(self._state)
            except Exception as e:
                logger.exception(f"Status callback error: {e}")
    
    def get_battery(self) -> Optional[BatteryInfo]:
        """Get battery status."""
        result = self._send_and_wait(MeshCoreProtocol.build_get_battery(), PacketType.BATTERY)
        return result if isinstance(result, BatteryInfo) else None
    
    def refresh_channels(self):
        """Refresh all channel information."""
        max_channels = 8
        if self._state.device_info:
            max_channels = self._state.device_info.max_channels or 8
        
        for i in range(max_channels):
            self._send_and_wait(MeshCoreProtocol.build_get_channel(i), PacketType.CHANNEL_INFO, timeout=2.0)
    
    def refresh_contacts(self):
        """Refresh contact list."""
        self._serial.write_frame(MeshCoreProtocol.build_get_contacts())
        # Wait for CONTACT_END
        start_time = time.time()
        while time.time() - start_time < 10.0:
            frame = self._serial.read_frame(timeout=0.5)
            if frame:
                pkt_type, data = MeshCoreProtocol.parse_response(frame)
                self._update_state(pkt_type, data)
                if pkt_type == PacketType.CONTACT_END:
                    break
    
    def get_channels(self) -> list[ChannelInfo]:
        """Get list of configured channels."""
        return [ch for ch in self._state.channels.values() if ch.name]
    
    def get_contacts(self) -> list[Contact]:
        """Get list of contacts."""
        return self._state.contacts.copy()
    
    def create_channel(self, index: int, name: str, secret: Optional[bytes] = None, 
                       is_hashtag: bool = False) -> bool:
        """
        Create or update a channel.
        
        Args:
            index: Channel index (1-7 for private, 0 for public).
            name: Channel name.
            secret: 32-byte secret, or None to generate.
            is_hashtag: If True and no secret, generate hashtag secret from name.
            
        Returns:
            True if successful.
        """
        if secret is None:
            if index == 0:
                # Public channel
                secret = bytes(32)
            else:
                secret = MeshCoreProtocol.generate_channel_secret(is_hashtag, name)
        
        command = MeshCoreProtocol.build_set_channel(index, name, secret)
        result = self._send_and_wait(command, PacketType.OK)
        
        if result is not None:
            # Refresh channel info
            self._send_and_wait(MeshCoreProtocol.build_get_channel(index), PacketType.CHANNEL_INFO)
            return True
        return False
    
    def delete_channel(self, index: int) -> bool:
        """Delete a channel."""
        command = MeshCoreProtocol.build_delete_channel(index)
        result = self._send_and_wait(command, PacketType.OK)
        if result is not None:
            with self._lock:
                if index in self._state.channels:
                    self._state.channels[index] = ChannelInfo(index=index, name="")
            return True
        return False

    def set_radio_params(
        self, freq_mhz: float, bw_khz: float, sf: int, cr: int, tx_power: Optional[int] = None
    ) -> bool:
        """
        Set LoRa radio parameters (saved to device prefs).

        Args:
            freq_mhz: Center frequency in MHz.
            bw_khz: Bandwidth in kHz.
            sf: Spreading factor.
            cr: Coding rate.
            tx_power: Optional TX power in dBm.

        Returns:
            True if successful.
        """
        command = MeshCoreProtocol.build_set_radio_params(freq_mhz, bw_khz, sf, cr)
        result = self._send_and_wait(command, PacketType.OK, timeout=5.0)
        if result is None:
            return False
        if tx_power is not None:
            tx_cmd = MeshCoreProtocol.build_set_radio_tx_power(tx_power)
            tx_result = self._send_and_wait(tx_cmd, PacketType.OK, timeout=5.0)
            if tx_result is None:
                return False
        if self._state.self_info:
            self._state.self_info.radio_freq = freq_mhz
            self._state.self_info.radio_bw = bw_khz
            self._state.self_info.radio_sf = sf
            self._state.self_info.radio_cr = cr
            if tx_power is not None:
                self._state.self_info.tx_power = tx_power
        return True

    def send_channel_message(self, channel_idx: int, text: str) -> bool:
        """
        Send a message to a channel.
        
        Args:
            channel_idx: Channel index (0-7).
            text: Message text (max 133 chars).
            
        Returns:
            True if sent successfully.
        """
        if len(text) > 133:
            logger.warning(f"Message truncated from {len(text)} to 133 chars")
            text = text[:133]
        
        command = MeshCoreProtocol.build_send_channel_message(channel_idx, text)
        result = self._send_and_wait(
            command,
            PacketType.MSG_SENT,
            timeout=10.0,
            also_accept=(PacketType.OK,),
        )
        if result is not None:
            return True
        # v1.15+ companions may accept the frame without a timely MSG_SENT/OK.
        if self._send_without_error(command, timeout=2.0):
            logger.info("Channel message queued (no MSG_SENT ack; assuming success)")
            return True
        return False

    def send_direct_message(self, pubkey_prefix: str, text: str) -> bool:
        """
        Send a direct message to a contact.

        Args:
            pubkey_prefix: 6-byte public key prefix (hex string).
            text: Message text (max 133 chars).

        Returns:
            True if sent successfully.
        """
        if len(text) > 133:
            logger.warning(f"Message truncated from {len(text)} to 133 chars")
            text = text[:133]

        pubkey_bytes = bytes.fromhex(pubkey_prefix)[:6]
        command = MeshCoreProtocol.build_send_direct_message(pubkey_bytes, text)
        result = self._send_and_wait(
            command,
            PacketType.MSG_SENT,
            timeout=10.0,
            also_accept=(PacketType.OK,),
        )
        if result is not None:
            return True
        if self._send_without_error(command, timeout=2.0):
            logger.info("Direct message queued (no MSG_SENT ack; assuming success)")
            return True
        return False

    def get_messages(self) -> list[Message]:
        """Get list of received messages."""
        return self._state.messages.copy()
    
    def _fetch_pending_messages(self):
        """Fetch all pending messages from device."""
        max_attempts = 50  # Safety limit
        for _ in range(max_attempts):
            self._serial.write_frame(MeshCoreProtocol.build_get_message())
            frame = self._serial.read_frame(timeout=2.0)
            if not frame:
                break
            
            pkt_type, data = MeshCoreProtocol.parse_response(frame)
            self._update_state(pkt_type, data)
            
            if pkt_type == PacketType.NO_MORE_MSGS:
                break
    
    def start_polling(self, interval: float = 5.0):
        """
        Start background polling for messages.
        
        Args:
            interval: Polling interval in seconds.
        """
        if self._polling:
            return
        
        self._polling = True
        self._polling_thread = threading.Thread(
            target=self._polling_loop, args=(interval,), daemon=True
        )
        self._polling_thread.start()
    
    def stop_polling(self):
        """Stop background polling."""
        self._polling = False
        if self._polling_thread and self._polling_thread.is_alive():
            self._polling_thread.join(timeout=2.0)
    
    def _polling_loop(self, interval: float):
        """Background polling loop."""
        while self._polling and self.is_connected:
            try:
                self._fetch_pending_messages()
            except Exception as e:
                logger.exception(f"Polling error: {e}")
            
            # Sleep in small increments to allow quick shutdown
            for _ in range(int(interval * 10)):
                if not self._polling:
                    break
                time.sleep(0.1)
    
    def to_dict(self) -> dict:
        """Convert device state to dictionary for JSON serialization."""
        return {
            'connected': self._state.connected,
            'device_info': {
                'fw_ver': self._state.device_info.fw_ver,
                'max_contacts': self._state.device_info.max_contacts,
                'max_channels': self._state.device_info.max_channels,
                'fw_build': self._state.device_info.fw_build,
                'model': self._state.device_info.model,
                'version': self._state.device_info.version,
            } if self._state.device_info else None,
            'self_info': {
                'name': self._state.self_info.name,
                'public_key': self._state.self_info.public_key[:12] + '...',
                'radio_freq': self._state.self_info.radio_freq,
                'radio_bw': self._state.self_info.radio_bw,
                'radio_sf': self._state.self_info.radio_sf,
                'tx_power': self._state.self_info.tx_power,
            } if self._state.self_info else None,
            'battery': {
                'level': self._state.battery.level,
                'used_kb': self._state.battery.used_kb,
                'total_kb': self._state.battery.total_kb,
            } if self._state.battery else None,
            'channels': [
                {'index': ch.index, 'name': ch.name}
                for ch in self._state.channels.values() if ch.name
            ],
            'contacts': [
                {'public_key': c.public_key, 'name': c.name}
                for c in self._state.contacts
            ],
            'message_count': len(self._state.messages),
            'last_update': datetime.fromtimestamp(self._state.last_update).isoformat()
            if self._state.last_update else None,
        }


def find_meshcore_device() -> Optional[str]:
    """
    Find a MeshCore device on available serial ports.
    
    Returns:
        Serial port path if found, None otherwise.
    """
    ports = list_serial_ports()
    
    # Common patterns for MeshCore devices
    patterns = ['usbmodem', 'usbserial', 'ttyUSB', 'ttyACM', 'SLAB', 'CP210', 'CH340']
    
    for port in ports:
        port_lower = port.lower()
        for pattern in patterns:
            if pattern.lower() in port_lower:
                logger.info(f"Found potential MeshCore device: {port}")
                return port
    
    # If no match, return first available port
    if ports:
        logger.info(f"Using first available port: {ports[0]}")
        return ports[0]
    
    return None
