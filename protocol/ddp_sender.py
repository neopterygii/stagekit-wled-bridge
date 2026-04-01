"""DDP (Distributed Display Protocol) sender for WLED.

Sends raw RGB pixel data via UDP to a WLED controller using the DDP protocol.
Port 4048, version 1, RGB24 data type.

DDP packet structure (10-byte header + pixel data):
  byte 0: flags (version | push)
  byte 1: sequence number (lower 4 bits)
  byte 2: data type (0x0B = RGB24)
  byte 3: destination (0x01 = display)
  bytes 4-7: channel offset (big-endian uint32) 
  bytes 8-9: data length (big-endian uint16)
  bytes 10+: RGB pixel data
"""

import socket
import struct

DDP_PORT = 4048
DDP_HEADER_LEN = 10
DDP_MAX_CHANNELS_PER_PACKET = 1440  # 480 LEDs * 3 channels

DDP_FLAGS_VER1 = 0x40
DDP_FLAGS_PUSH = 0x01
DDP_TYPE_RGB24 = 0x0B
DDP_ID_DISPLAY = 0x01


class DDPSender:
    def __init__(self, host: str, port: int = DDP_PORT):
        self.host = host
        self.port = port
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._seq = 0

    def send_pixels(self, pixel_data: bytes) -> None:
        """Send RGB pixel data to WLED via DDP.
        
        pixel_data: flat bytes of R,G,B,R,G,B,... for all LEDs.
        Automatically splits into multiple packets if > 480 LEDs.
        """
        total = len(pixel_data)
        offset = 0

        while offset < total:
            chunk_size = min(DDP_MAX_CHANNELS_PER_PACKET, total - offset)
            is_last = (offset + chunk_size) >= total

            flags = DDP_FLAGS_VER1
            if is_last:
                flags |= DDP_FLAGS_PUSH

            header = struct.pack(
                ">BBBB I H",
                flags,
                self._seq & 0x0F,
                DDP_TYPE_RGB24,
                DDP_ID_DISPLAY,
                offset,       # channel offset (big-endian)
                chunk_size,   # data length (big-endian)
            )

            packet = header + pixel_data[offset:offset + chunk_size]
            self._sock.sendto(packet, (self.host, self.port))

            offset += chunk_size
            self._seq = (self._seq + 1) & 0x0F

    def close(self):
        self._sock.close()
