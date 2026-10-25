"""A minimal FIT writer for tests and the dev fake: `record` messages with timestamp, power and heart rate."""
from __future__ import annotations

import io
import struct
import zipfile
from datetime import datetime, timezone

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
_CRC = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def crc(data: bytes, c: int = 0) -> int:
    for b in data:
        t = _CRC[c & 0xF]
        c = (c >> 4) & 0x0FFF
        c = c ^ t ^ _CRC[b & 0xF]
        t = _CRC[c & 0xF]
        c = (c >> 4) & 0x0FFF
        c = c ^ t ^ _CRC[(b >> 4) & 0xF]
    return c


def fit_records(start: datetime, watts: list[int], hr: list[int]) -> bytes:
    """One record a second from `start`."""
    # Definition: local 0 → global 20 (record): timestamp (253, uint32), power (7, uint16), heart_rate (3, uint8)
    body = bytes([0x40, 0, 0]) + struct.pack("<HB", 20, 3) + bytes([253, 4, 0x86, 7, 2, 0x84, 3, 1, 0x02])
    t0 = int((start - FIT_EPOCH).total_seconds())
    for i, (w, h) in enumerate(zip(watts, hr)):
        body += bytes([0x00]) + struct.pack("<IHB", t0 + i, w, h)
    header = struct.pack("<BBHI4s", 14, 0x10, 2093, len(body), b".FIT")
    header += struct.pack("<H", crc(header))
    data = header + body
    return data + struct.pack("<H", crc(data))


def zipped(fit: bytes, name: str = "12345_ACTIVITY.fit") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, fit)
    return buf.getvalue()
