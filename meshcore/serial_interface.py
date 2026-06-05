"""
Serial interface for MeshCore USB communication.

Frame format:
- Host to Device: '<' [len_lsb] [len_msb] [data...]
- Device to Host: '>' [len_lsb] [len_msb] [data...]
"""

import serial
import threading
import queue
import time
import logging
from typing import Optional, Callable

logger = logging.getLogger(__name__)

MAX_FRAME_SIZE = 172


class SerialInterface:
    """Handles USB serial communication with MeshCore devices using the framing protocol."""
    
    # Receive state machine states
    STATE_IDLE = 0
    STATE_HDR_FOUND = 1
    STATE_LEN1_FOUND = 2
    STATE_RECEIVING = 3
    
    def __init__(self, port: str, baudrate: int = 115200):
        """
        Initialize the serial interface.
        
        Args:
            port: Serial port path (e.g., '/dev/ttyUSB0' or '/dev/cu.usbmodem*')
            baudrate: Baud rate (default 115200)
        """
        self.port = port
        self.baudrate = baudrate
        self._serial: Optional[serial.Serial] = None
        self._running = False
        self._recv_thread: Optional[threading.Thread] = None
        self._recv_queue: queue.Queue = queue.Queue()
        self._state = self.STATE_IDLE
        self._frame_len = 0
        self._rx_buf = bytearray()
        self._callback: Optional[Callable[[bytes], None]] = None
        self._lock = threading.Lock()
    
    def connect(self) -> bool:
        """
        Open the serial connection and start the receive thread.
        
        Returns:
            True if connection successful, False otherwise.
        """
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0.1,
                write_timeout=5.0,
            )
            self._running = True
            self._recv_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self._recv_thread.start()
            logger.info(f"Connected to {self.port} at {self.baudrate} baud")
            return True
        except serial.SerialException as e:
            logger.error(f"Failed to connect to {self.port}: {e}")
            return False
    
    def disconnect(self):
        """Close the serial connection and stop the receive thread."""
        self._running = False
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=2.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
            logger.info(f"Disconnected from {self.port}")
        self._serial = None
    
    def is_connected(self) -> bool:
        """Check if the serial port is connected."""
        return self._serial is not None and self._serial.is_open
    
    def set_callback(self, callback: Optional[Callable[[bytes], None]]):
        """
        Set a callback function to be called when a frame is received.
        
        Args:
            callback: Function that takes bytes as argument, or None to disable.
        """
        self._callback = callback
    
    def write_frame(self, data: bytes) -> bool:
        """
        Send a frame to the device.
        
        Args:
            data: The payload data to send (without framing).
            
        Returns:
            True if sent successfully, False otherwise.
        """
        if not self.is_connected():
            logger.error("Cannot write: not connected")
            return False
        
        if len(data) > MAX_FRAME_SIZE:
            logger.error(f"Frame too large: {len(data)} > {MAX_FRAME_SIZE}")
            return False
        
        # Build frame: '<' + length (16-bit little-endian) + data
        frame = bytearray()
        frame.append(ord('<'))
        frame.append(len(data) & 0xFF)  # LSB
        frame.append((len(data) >> 8) & 0xFF)  # MSB
        frame.extend(data)
        
        try:
            with self._lock:
                self._serial.write(frame)
                self._serial.flush()
            logger.debug(f"Sent frame: {data.hex()}")
            return True
        except serial.SerialException as e:
            logger.error(f"Write error: {e}")
            return False
    
    def read_frame(self, timeout: float = 5.0) -> Optional[bytes]:
        """
        Read a frame from the receive queue.
        
        Args:
            timeout: Maximum time to wait for a frame (seconds).
            
        Returns:
            The frame data, or None if timeout.
        """
        try:
            return self._recv_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def _receive_loop(self):
        """Background thread that receives and parses frames."""
        while self._running:
            try:
                if not self._serial or not self._serial.is_open:
                    time.sleep(0.1)
                    continue
                
                # Read available bytes
                if self._serial.in_waiting > 0:
                    data = self._serial.read(self._serial.in_waiting)
                    self._process_bytes(data)
                else:
                    time.sleep(0.01)
                    
            except serial.SerialException as e:
                logger.error(f"Receive error: {e}")
                time.sleep(0.1)
            except Exception as e:
                logger.exception(f"Unexpected error in receive loop: {e}")
                time.sleep(0.1)
    
    def _process_bytes(self, data: bytes):
        """
        Process received bytes through the state machine.
        
        Frame format: '>' [len_lsb] [len_msb] [payload...]
        """
        for byte in data:
            if self._state == self.STATE_IDLE:
                if byte == ord('>'):
                    self._state = self.STATE_HDR_FOUND
                    
            elif self._state == self.STATE_HDR_FOUND:
                self._frame_len = byte  # LSB
                self._state = self.STATE_LEN1_FOUND
                
            elif self._state == self.STATE_LEN1_FOUND:
                self._frame_len |= (byte << 8)  # MSB
                self._rx_buf = bytearray()
                if self._frame_len > 0:
                    self._state = self.STATE_RECEIVING
                else:
                    self._state = self.STATE_IDLE
                    
            elif self._state == self.STATE_RECEIVING:
                if len(self._rx_buf) < MAX_FRAME_SIZE:
                    self._rx_buf.append(byte)
                
                if len(self._rx_buf) >= self._frame_len:
                    # Complete frame received
                    frame_data = bytes(self._rx_buf[:min(self._frame_len, MAX_FRAME_SIZE)])
                    logger.debug(f"Received frame: {frame_data.hex()}")
                    
                    # Put in queue and call callback
                    self._recv_queue.put(frame_data)
                    if self._callback:
                        try:
                            self._callback(frame_data)
                        except Exception as e:
                            logger.exception(f"Callback error: {e}")
                    
                    self._state = self.STATE_IDLE
    
    def clear_queue(self):
        """Clear any pending frames from the receive queue."""
        while not self._recv_queue.empty():
            try:
                self._recv_queue.get_nowait()
            except queue.Empty:
                break


def list_serial_ports() -> list[str]:
    """
    List available serial ports.
    
    Returns:
        List of serial port paths.
    """
    import serial.tools.list_ports
    ports = serial.tools.list_ports.comports()
    return [port.device for port in ports]
