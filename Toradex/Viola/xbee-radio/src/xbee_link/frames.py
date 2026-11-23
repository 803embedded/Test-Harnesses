# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""XBee API frame encoding and decoding.

Supports API mode 1 (``AP=1``, unescaped) and API mode 2 (``AP=2``, escaped).
Only the frame types needed for point-to-point data links are decoded; any
other frame is returned as :class:`UnknownFrame` so callers can ignore it.

This module has no I/O and no dependencies outside the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

START = 0x7E
ESCAPE = 0x7D
XON = 0x11
XOFF = 0x13
_ESCAPED_BYTES = frozenset({START, ESCAPE, XON, XOFF})

BROADCAST_64 = 0x000000000000FFFF
UNKNOWN_16 = 0xFFFE
# A 64-bit source of all ones means the sender used a 16-bit source address.
UNKNOWN_64 = 0xFFFFFFFFFFFFFFFF

MAX_FRAME_DATA = 0xFFFF


class FrameType(IntEnum):
    TX_REQUEST_64 = 0x00  # legacy 802.15.4 transmit, 64-bit destination
    AT_COMMAND = 0x08
    AT_COMMAND_QUEUE = 0x09
    TRANSMIT_REQUEST = 0x10
    RX_64 = 0x80  # legacy 802.15.4 receive, 64-bit source
    RX_16 = 0x81  # legacy 802.15.4 receive, 16-bit source
    AT_RESPONSE = 0x88
    TX_STATUS = 0x89  # legacy 802.15.4 transmit status
    MODEM_STATUS = 0x8A
    TRANSMIT_STATUS = 0x8B
    RECEIVE_PACKET = 0x90
    EXPLICIT_RX = 0x91


class FrameError(ValueError):
    """Raised when frame data is malformed."""


@dataclass(frozen=True)
class AtResponse:
    frame_id: int
    command: str
    status: int
    value: bytes


@dataclass(frozen=True)
class TransmitStatus:
    """Delivery result for a transmitted frame (0x8B, or legacy 0x89).

    ``status`` is the 0x8B delivery status. Legacy 0x89 codes are mapped onto
    the same meanings: 0 success, 1 no MAC ACK, 2 CCA failure, 3 purged.
    """

    frame_id: int
    status: int
    retries: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == 0


@dataclass(frozen=True)
class ReceivedPacket:
    data: bytes
    source64: int | None
    source16: int | None
    options: int
    rssi_dbm: int | None = None

    @property
    def broadcast(self) -> bool:
        # Bit 1 is "packet was a broadcast" for 0x80/0x81/0x90/0x91.
        return bool(self.options & 0x02)


@dataclass(frozen=True)
class ModemStatus:
    status: int

    HARDWARE_RESET = 0x00
    WATCHDOG_RESET = 0x01

    @property
    def is_reset(self) -> bool:
        return self.status in (self.HARDWARE_RESET, self.WATCHDOG_RESET)


@dataclass(frozen=True)
class UnknownFrame:
    frame_type: int
    data: bytes


Frame = AtResponse | TransmitStatus | ReceivedPacket | ModemStatus | UnknownFrame


def checksum(frame_data: bytes) -> int:
    return 0xFF - (sum(frame_data) & 0xFF)


def _escape(data: bytes) -> bytes:
    out = bytearray()
    for byte in data:
        if byte in _ESCAPED_BYTES:
            out += bytes((ESCAPE, byte ^ 0x20))
        else:
            out.append(byte)
    return bytes(out)


def _unescape(data: bytes | bytearray) -> bytes:
    if ESCAPE not in data:
        return bytes(data)
    out = bytearray()
    it = iter(data)
    for byte in it:
        if byte == ESCAPE:
            following = next(it, None)
            if following is None:
                break  # incomplete escape at the end of a partial frame
            out.append(following ^ 0x20)
        else:
            out.append(byte)
    return bytes(out)


def encode(frame_data: bytes, *, escaped: bool = False) -> bytes:
    """Wrap frame data (type byte onward) with delimiter, length and checksum."""
    if not frame_data:
        raise FrameError("frame data must not be empty")
    if len(frame_data) > MAX_FRAME_DATA:
        raise FrameError(f"frame data too long ({len(frame_data)} bytes)")
    body = len(frame_data).to_bytes(2, "big") + frame_data + bytes((checksum(frame_data),))
    if escaped:
        body = _escape(body)
    return bytes((START,)) + body


class FrameParser:
    """Incremental API frame parser.

    Feed raw serial bytes with :meth:`feed`; it returns the frame data (type
    byte onward) of every complete, checksum-valid frame. Bytes outside frames
    are discarded and frames with a bad checksum are dropped; both are counted
    so the caller can report line noise.
    """

    def __init__(self, *, escaped: bool = False) -> None:
        self.escaped = escaped
        self.checksum_errors = 0
        self.discarded_bytes = 0
        self._buf = bytearray()

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        return self._parse_escaped() if self.escaped else self._parse_unescaped()

    def _drop_until_start(self) -> bool:
        index = self._buf.find(START)
        if index < 0:
            self.discarded_bytes += len(self._buf)
            self._buf.clear()
            return False
        if index:
            self.discarded_bytes += index
            del self._buf[:index]
        return True

    def _parse_unescaped(self) -> list[bytes]:
        frames: list[bytes] = []
        buf = self._buf
        while self._drop_until_start():
            if len(buf) < 3:
                break
            length = (buf[1] << 8) | buf[2]
            if length == 0:
                self.discarded_bytes += 1
                del buf[:1]
                continue
            needed = 3 + length + 1
            if len(buf) < needed:
                break
            data = bytes(buf[3 : 3 + length])
            if (sum(data) + buf[3 + length]) & 0xFF != 0xFF:
                # Probably a 0x7E inside data; resynchronize on the next one.
                self.checksum_errors += 1
                self.discarded_bytes += 1
                del buf[:1]
                continue
            frames.append(data)
            del buf[:needed]
        return frames

    def _parse_escaped(self) -> list[bytes]:
        # In AP=2 a raw 0x7E only ever appears as a start delimiter, so each
        # frame is the escaped run between one delimiter and the next.
        frames: list[bytes] = []
        buf = self._buf
        while self._drop_until_start():
            next_start = buf.find(START, 1)
            segment = buf[1:next_start] if next_start >= 0 else buf[1:]
            raw = _unescape(segment)
            if len(raw) >= 2:
                length = (raw[0] << 8) | raw[1]
                needed = 2 + length + 1
            else:
                length, needed = 0, 3
            if len(raw) < needed:
                if next_start < 0:
                    break  # wait for more bytes
                self.discarded_bytes += next_start  # truncated frame
                del buf[:next_start]
                continue
            data = raw[2 : 2 + length]
            consumed = next_start if next_start >= 0 else len(buf)
            if length == 0 or (sum(data) + raw[2 + length]) & 0xFF != 0xFF:
                self.checksum_errors += 1
                self.discarded_bytes += consumed
            else:
                frames.append(data)
            del buf[:consumed]
        return frames


def _check_length(frame_data: bytes, minimum: int, name: str) -> None:
    if len(frame_data) < minimum:
        raise FrameError(f"{name} frame too short ({len(frame_data)} bytes)")


def _legacy_tx_status(code: int) -> int:
    # 0x89: 0 success, 1 no ACK, 2 CCA failure, 3 purged -> 0x8B equivalents.
    return {0: 0x00, 1: 0x01, 2: 0x02, 3: 0x03}.get(code, code)


def decode(frame_data: bytes) -> Frame:
    """Decode frame data (type byte onward) into a typed frame."""
    _check_length(frame_data, 1, "API")
    frame_type = frame_data[0]
    if frame_type == FrameType.AT_RESPONSE:
        _check_length(frame_data, 5, "AT response")
        return AtResponse(
            frame_id=frame_data[1],
            command=frame_data[2:4].decode("ascii", errors="replace"),
            status=frame_data[4],
            value=frame_data[5:],
        )
    if frame_type == FrameType.TRANSMIT_STATUS:
        _check_length(frame_data, 7, "transmit status")
        return TransmitStatus(frame_id=frame_data[1], status=frame_data[5], retries=frame_data[4])
    if frame_type == FrameType.TX_STATUS:
        _check_length(frame_data, 3, "TX status")
        return TransmitStatus(frame_id=frame_data[1], status=_legacy_tx_status(frame_data[2]))
    if frame_type == FrameType.MODEM_STATUS:
        _check_length(frame_data, 2, "modem status")
        return ModemStatus(status=frame_data[1])
    if frame_type == FrameType.RECEIVE_PACKET:
        _check_length(frame_data, 12, "receive packet")
        return ReceivedPacket(
            data=frame_data[12:],
            source64=_source64(frame_data[1:9]),
            source16=int.from_bytes(frame_data[9:11], "big"),
            options=frame_data[11],
        )
    if frame_type == FrameType.EXPLICIT_RX:
        _check_length(frame_data, 18, "explicit receive")
        return ReceivedPacket(
            data=frame_data[18:],
            source64=_source64(frame_data[1:9]),
            source16=int.from_bytes(frame_data[9:11], "big"),
            options=frame_data[17],
        )
    if frame_type == FrameType.RX_64:
        _check_length(frame_data, 11, "RX 64-bit")
        return ReceivedPacket(
            data=frame_data[11:],
            source64=_source64(frame_data[1:9]),
            source16=None,
            options=frame_data[10],
            rssi_dbm=-frame_data[9],
        )
    if frame_type == FrameType.RX_16:
        _check_length(frame_data, 5, "RX 16-bit")
        return ReceivedPacket(
            data=frame_data[5:],
            source64=None,
            source16=int.from_bytes(frame_data[1:3], "big"),
            options=frame_data[4],
            rssi_dbm=-frame_data[3],
        )
    return UnknownFrame(frame_type=frame_type, data=frame_data[1:])


def _source64(raw: bytes) -> int | None:
    value = int.from_bytes(raw, "big")
    return None if value == UNKNOWN_64 else value


def at_command(frame_id: int, command: str, value: bytes = b"", *, queue: bool = False) -> bytes:
    """Build a local AT command (0x08) or queued AT command (0x09) frame."""
    if len(command) != 2:
        raise FrameError(f"AT command must be two characters, got {command!r}")
    frame_type = FrameType.AT_COMMAND_QUEUE if queue else FrameType.AT_COMMAND
    return bytes((frame_type, frame_id & 0xFF)) + command.encode("ascii") + value


def transmit_request(frame_id: int, dest64: int, data: bytes, *, options: int = 0) -> bytes:
    """Build a 0x10 Transmit Request to a 64-bit destination."""
    return (
        bytes((FrameType.TRANSMIT_REQUEST, frame_id & 0xFF))
        + dest64.to_bytes(8, "big")
        + UNKNOWN_16.to_bytes(2, "big")
        + bytes((0, options))  # broadcast radius, transmit options
        + data
    )


def tx64_request(frame_id: int, dest64: int, data: bytes, *, options: int = 0) -> bytes:
    """Build a legacy 802.15.4 0x00 TX Request with a 64-bit destination."""
    return (
        bytes((FrameType.TX_REQUEST_64, frame_id & 0xFF))
        + dest64.to_bytes(8, "big")
        + bytes((options,))
        + data
    )


def encode_value(value: int | str | bytes) -> bytes:
    """Encode an AT parameter: ints big-endian minimal width, strings ASCII."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("ascii")
    if isinstance(value, bool) or value < 0:
        raise FrameError(f"AT parameter must be a non-negative integer, got {value!r}")
    return value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
