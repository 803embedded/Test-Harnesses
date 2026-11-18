# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Reliable transfer of an arbitrary byte buffer between two XBee radios.

An RF frame carries at most ``ATNP`` bytes, so a buffer is split into chunks.
Every chunk is a unicast frame, so the radio's MAC acknowledgement and retries
apply; on top of that the sender retransmits chunks whose delivery status
failed, and the receiver verifies the reassembled buffer with CRC32 and
reports back which chunks (if any) are still missing.

Wire format (all integers big-endian)::

    START   B1 id total:u32 chunk:u16 crc32:u32
    DATA    B2 id offset:u32 payload...
    END     B3 id
    REPORT  B4 id status:u8 received:u32 crc32:u32 missing:u32 offset:u32...

Packets that do not start with one of these type bytes are ignored, so other
application traffic can share the radio.
"""

from __future__ import annotations

import os
import struct
import time
import zlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .frames import ReceivedPacket
from .xbee import TransmitError, XBee, XBeeError, XBeeTimeout, modem_status_text

MSG_START = 0xB1
MSG_DATA = 0xB2
MSG_END = 0xB3
MSG_REPORT = 0xB4

REPORT_OK = 0
REPORT_MISSING = 1
REPORT_CRC_MISMATCH = 2
REPORT_UNKNOWN_TRANSFER = 3
REPORT_STATUS_TEXT = {
    REPORT_OK: "OK",
    REPORT_MISSING: "chunks missing",
    REPORT_CRC_MISMATCH: "CRC mismatch",
    REPORT_UNKNOWN_TRANSFER: "receiver has no record of this transfer",
}

_START = struct.Struct(">BBIHI")
_DATA_HEADER = struct.Struct(">BBI")
_END = struct.Struct(">BB")
_REPORT_HEADER = struct.Struct(">BBBIII")

DATA_HEADER_SIZE = _DATA_HEADER.size
PAYLOAD_TOO_LARGE = 0x74

ProgressCallback = Callable[[int, int], None]


class TransferError(XBeeError):
    """The buffer could not be delivered or verified."""


class RadioResetError(TransferError):
    """The local radio reset during a transfer (for example a supply brownout)."""


@dataclass(frozen=True)
class Report:
    transfer_id: int
    status: int
    received: int
    crc32: int
    missing_count: int
    missing: tuple[int, ...]

    def encode(self) -> bytes:
        header = _REPORT_HEADER.pack(
            MSG_REPORT, self.transfer_id, self.status, self.received, self.crc32,
            self.missing_count,
        )
        return header + b"".join(struct.pack(">I", offset) for offset in self.missing)

    @classmethod
    def decode(cls, data: bytes) -> Report:
        if len(data) < _REPORT_HEADER.size or (len(data) - _REPORT_HEADER.size) % 4:
            raise ValueError("malformed REPORT")
        _, transfer_id, status, received, crc, missing_count = _REPORT_HEADER.unpack_from(data)
        missing = tuple(
            struct.unpack_from(">I", data, offset)[0]
            for offset in range(_REPORT_HEADER.size, len(data), 4)
        )
        return cls(transfer_id, status, received, crc, missing_count, missing)


@dataclass
class SendResult:
    transfer_id: int
    size: int
    chunk_size: int
    chunks: int
    frames_sent: int = 0
    retransmissions: int = 0
    status_timeouts: int = 0
    rounds: int = 0
    duration_s: float = 0.0
    delivery_failures: dict[int, int] = field(default_factory=dict)
    report: Report | None = None

    @property
    def throughput_bps(self) -> float:
        return self.size / self.duration_s if self.duration_s > 0 else 0.0


@dataclass
class ReceiveResult:
    transfer_id: int
    source64: int | None
    data: bytes
    chunk_size: int
    chunks: int
    packets: int = 0
    duplicates: int = 0
    duration_s: float = 0.0
    last_rssi_dbm: int | None = None

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def throughput_bps(self) -> float:
        return self.size / self.duration_s if self.duration_s > 0 else 0.0


class _ResetWatch:
    """Raises if the radio reports a reset after this object was created."""

    def __init__(self, xbee: XBee) -> None:
        self._xbee = xbee
        self._seen = len(xbee.modem_events)

    def check(self) -> None:
        events = self._xbee.modem_events[self._seen :]
        self._seen = len(self._xbee.modem_events)
        for event in events:
            if event.is_reset:
                raise RadioResetError(
                    f"local radio reported '{modem_status_text(event.status)}' during the "
                    "transfer; volatile settings are lost (check the supply rail)"
                )


class _Throttle:
    def __init__(self, callback: ProgressCallback | None, interval: float) -> None:
        self._callback = callback
        self._interval = interval
        self._next = 0.0

    def __call__(self, done: int, total: int, *, force: bool = False) -> None:
        if self._callback is None:
            return
        now = time.monotonic()
        if force or now >= self._next:
            self._next = now + self._interval
            self._callback(done, total)


def default_chunk_size(xbee: XBee) -> int:
    chunk = xbee.max_payload() - DATA_HEADER_SIZE
    if chunk <= 0:
        raise TransferError(f"radio payload limit NP={xbee.max_payload()} is too small")
    return chunk


def _send_control(xbee: XBee, dest64: int, payload: bytes, retries: int, timeout: float) -> None:
    last: Exception | None = None
    for _ in range(retries + 1):
        try:
            xbee.send(dest64, payload, timeout=timeout)
            return
        except (TransmitError, XBeeTimeout) as exc:
            last = exc
    raise TransferError(f"control message not delivered after {retries + 1} attempts: {last}")


def _from_peer(packet: ReceivedPacket, peer64: int | None) -> bool:
    return peer64 is None or packet.source64 is None or packet.source64 == peer64


def send_buffer(
    xbee: XBee,
    dest64: int,
    data: bytes,
    *,
    chunk_size: int | None = None,
    window: int = 4,
    retries: int = 8,
    status_timeout: float = 5.0,
    report_timeout: float = 5.0,
    max_rounds: int = 10,
    transfer_id: int | None = None,
    on_progress: ProgressCallback | None = None,
    progress_interval: float = 1.0,
) -> SendResult:
    """Send ``data`` to the radio at ``dest64`` and wait for verified receipt.

    ``window`` frames are kept in flight at once. A chunk whose delivery fails
    is retried up to ``retries`` times. Raises :class:`TransferError` if the
    buffer cannot be delivered or the receiver reports a CRC mismatch.
    """
    if not 1 <= window <= 64:
        raise ValueError("window must be between 1 and 64")
    if len(data) > 0xFFFFFFFF:
        raise ValueError("buffer too large (4 GiB limit)")
    chunk = chunk_size or default_chunk_size(xbee)
    if not 1 <= chunk <= 0xFFFF:
        raise ValueError(f"invalid chunk size {chunk}")
    xfer = os.urandom(1)[0] if transfer_id is None else transfer_id & 0xFF
    crc = zlib.crc32(data)
    offsets = list(range(0, len(data), chunk))
    result = SendResult(transfer_id=xfer, size=len(data), chunk_size=chunk, chunks=len(offsets))
    resets = _ResetWatch(xbee)
    progress = _Throttle(on_progress, progress_interval)
    view = memoryview(data)
    started = time.monotonic()

    _send_control(
        xbee, dest64, _START.pack(MSG_START, xfer, len(data), chunk, crc), retries, status_timeout
    )

    pending: deque[int] = deque(offsets)
    attempts: dict[int, int] = {}
    delivered: set[int] = set()
    while True:
        result.rounds += 1
        outstanding: dict[int, tuple[int, float]] = {}
        while pending or outstanding:
            while pending and len(outstanding) < window:
                offset = pending.popleft()
                if offset in delivered:
                    continue
                count = attempts.get(offset, 0) + 1
                if count > retries + 1:
                    raise TransferError(
                        f"chunk at offset {offset} not delivered after {retries + 1} attempts"
                    )
                attempts[offset] = count
                if count > 1:
                    result.retransmissions += 1
                header = _DATA_HEADER.pack(MSG_DATA, xfer, offset)
                frame_id = xbee.transmit(dest64, header + bytes(view[offset : offset + chunk]))
                outstanding[frame_id] = (offset, time.monotonic())
                result.frames_sent += 1

            xbee.poll(0.0 if pending and len(outstanding) < window else 0.05)
            now = time.monotonic()
            for frame_id, (offset, sent_at) in list(outstanding.items()):
                status = xbee.take_status(frame_id)
                if status is None:
                    if now - sent_at > status_timeout:
                        del outstanding[frame_id]
                        result.status_timeouts += 1
                        pending.append(offset)
                    continue
                del outstanding[frame_id]
                if status.ok:
                    delivered.add(offset)
                elif status.status == PAYLOAD_TOO_LARGE:
                    raise TransferError(
                        f"radio rejected a {chunk}-byte chunk as too large; reduce chunk_size"
                    )
                else:
                    result.delivery_failures[status.status] = (
                        result.delivery_failures.get(status.status, 0) + 1
                    )
                    pending.append(offset)
            resets.check()
            progress(min(len(delivered) * chunk, len(data)), len(data))

        report = _finish(xbee, dest64, xfer, retries, status_timeout, report_timeout)
        resets.check()
        result.report = report
        if report.status == REPORT_OK:
            if report.crc32 != crc or report.received != len(data):
                raise TransferError(
                    f"receiver verified {report.received} bytes with CRC 0x{report.crc32:08X}; "
                    f"sent {len(data)} bytes with CRC 0x{crc:08X}"
                )
            break
        if report.status != REPORT_MISSING:
            raise TransferError(
                f"receiver reported {REPORT_STATUS_TEXT.get(report.status, report.status)}"
            )
        if result.rounds >= max_rounds:
            raise TransferError(
                f"{report.missing_count} chunks still missing after {result.rounds} rounds"
            )
        for offset in report.missing:
            delivered.discard(offset)
            attempts[offset] = 0
            pending.append(offset)

    result.duration_s = time.monotonic() - started
    progress(len(data), len(data), force=True)
    return result


def _finish(
    xbee: XBee, dest64: int, xfer: int, retries: int, status_timeout: float, report_timeout: float
) -> Report:
    end = _END.pack(MSG_END, xfer)
    for _ in range(3):
        _send_control(xbee, dest64, end, retries, status_timeout)
        deadline = time.monotonic() + report_timeout
        while (remaining := deadline - time.monotonic()) > 0:
            packet = xbee.receive(remaining)
            if packet is None or not _from_peer(packet, dest64):
                continue
            if len(packet.data) >= 2 and packet.data[0] == MSG_REPORT and packet.data[1] == xfer:
                try:
                    return Report.decode(packet.data)
                except ValueError:
                    continue
    raise TransferError("receiver did not report on the transfer")


@dataclass
class _Incoming:
    transfer_id: int
    source64: int | None
    total: int
    chunk: int
    crc32: int
    buffer: bytearray
    got: bytearray
    started: float
    last_activity: float
    packets: int = 0
    duplicates: int = 0
    last_rssi: int | None = None

    def missing(self) -> list[int]:
        return [index * self.chunk for index, have in enumerate(self.got) if not have]


class BufferReceiver:
    """Receives buffers sent with :func:`send_buffer`.

    Keep one instance per radio for repeated transfers: it remembers the last
    completed transfer so a repeated END (whose REPORT was lost) is answered.
    """

    def __init__(self, xbee: XBee, *, retries: int = 8, status_timeout: float = 5.0) -> None:
        self.xbee = xbee
        self.retries = retries
        self.status_timeout = status_timeout
        self._last_completed: tuple[int | None, int, bytes] | None = None

    def _reply(self, dest64: int | None, report: Report) -> None:
        if dest64 is None:
            raise TransferError(
                "sender has no 64-bit source address; set MY=FFFE on the sending radio"
            )
        _send_control(self.xbee, dest64, report.encode(), self.retries, self.status_timeout)

    def receive(
        self,
        *,
        start_timeout: float | None = None,
        idle_timeout: float = 15.0,
        accept_from: int | None = None,
        on_progress: ProgressCallback | None = None,
        progress_interval: float = 1.0,
    ) -> ReceiveResult:
        """Wait for one complete, CRC-verified buffer and return it.

        ``start_timeout`` (None waits forever) bounds the wait for a START;
        ``idle_timeout`` bounds silence once a transfer is under way.
        """
        xbee = self.xbee
        resets = _ResetWatch(xbee)
        progress = _Throttle(on_progress, progress_interval)
        max_missing = max(1, (xbee.max_payload() - _REPORT_HEADER.size) // 4)
        wait_start = time.monotonic()
        state: _Incoming | None = None

        while True:
            packet = xbee.receive(0.2)
            resets.check()
            now = time.monotonic()
            if state is not None:
                progress(sum(state.got) * state.chunk, state.total)
            if packet is None:
                if state is None:
                    if start_timeout is not None and now - wait_start > start_timeout:
                        raise XBeeTimeout(f"no transfer started within {start_timeout:.0f} s")
                elif now - state.last_activity > idle_timeout:
                    raise TransferError(
                        f"transfer stalled: nothing received for {idle_timeout:.0f} s "
                        f"({len(state.missing())} of {len(state.got)} chunks missing)"
                    )
                continue
            if not packet.data or (accept_from is not None and packet.source64 != accept_from):
                continue
            kind = packet.data[0]

            if kind == MSG_START and len(packet.data) == _START.size:
                _, xfer, total, chunk, crc = _START.unpack(packet.data)
                if chunk == 0:
                    continue
                if (
                    state is not None
                    and state.transfer_id == xfer
                    and state.source64 == packet.source64
                    and state.total == total
                    and state.crc32 == crc
                ):
                    continue  # retransmitted START
                chunks = (total + chunk - 1) // chunk
                state = _Incoming(
                    xfer, packet.source64, total, chunk, crc, bytearray(total),
                    bytearray(chunks), now, now,
                )
                continue

            if kind == MSG_END and len(packet.data) == _END.size:
                xfer = packet.data[1]
                if state is None or state.transfer_id != xfer or (
                    state.source64 != packet.source64
                ):
                    last = self._last_completed
                    if last is not None and last[0] == packet.source64 and last[1] == xfer:
                        self._reply(packet.source64, Report.decode(last[2]))
                    elif state is None:
                        self._reply(
                            packet.source64,
                            Report(xfer, REPORT_UNKNOWN_TRANSFER, 0, 0, 0, ()),
                        )
                    continue
                state.last_activity = now
                missing = state.missing()
                if missing:
                    report = Report(
                        xfer, REPORT_MISSING, state.total - len(missing) * state.chunk,
                        0, len(missing), tuple(missing[:max_missing]),
                    )
                    self._reply(state.source64, report)
                    continue
                data = bytes(state.buffer)
                crc = zlib.crc32(data)
                status = REPORT_OK if crc == state.crc32 else REPORT_CRC_MISMATCH
                report = Report(xfer, status, len(data), crc, 0, ())
                self._last_completed = (state.source64, xfer, report.encode())
                self._reply(state.source64, report)
                if status != REPORT_OK:
                    raise TransferError(
                        f"CRC mismatch: expected 0x{state.crc32:08X}, got 0x{crc:08X}"
                    )
                progress(state.total, state.total, force=True)
                return ReceiveResult(
                    transfer_id=xfer,
                    source64=state.source64,
                    data=data,
                    chunk_size=state.chunk,
                    chunks=len(state.got),
                    packets=state.packets,
                    duplicates=state.duplicates,
                    duration_s=now - state.started,
                    last_rssi_dbm=state.last_rssi,
                )

            if kind == MSG_DATA and len(packet.data) >= DATA_HEADER_SIZE and state is not None:
                _, xfer, offset = _DATA_HEADER.unpack_from(packet.data)
                if xfer != state.transfer_id or packet.source64 != state.source64:
                    continue
                payload = packet.data[DATA_HEADER_SIZE:]
                index, remainder = divmod(offset, state.chunk)
                expected = min(state.chunk, state.total - offset)
                if remainder or index >= len(state.got) or len(payload) != expected:
                    continue
                state.packets += 1
                state.last_activity = now
                if packet.rssi_dbm is not None:
                    state.last_rssi = packet.rssi_dbm
                if state.got[index]:
                    state.duplicates += 1
                else:
                    state.buffer[offset : offset + expected] = payload
                    state.got[index] = 1
