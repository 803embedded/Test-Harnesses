# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""In-memory XBee simulator: a Transport that answers API frames like a radio.

Two FakeRadio instances can be linked so that transmit requests on one arrive
as legacy 0x80 receive frames on the other, with configurable packet loss.
"""

from __future__ import annotations

import random
import threading
import time

from xbee_link import frames


class FakeRadio:
    def __init__(
        self,
        address64: int,
        *,
        np: int = 102,
        api_mode: int = 1,
        baud: int = 9600,
        loss: float = 0.0,
        seed: int = 0,
    ) -> None:
        self.address64 = address64
        self.params: dict[str, bytes] = {
            "AP": bytes((api_mode,)),
            "NP": bytes((np,)),
            "SH": (address64 >> 32).to_bytes(4, "big"),
            "SL": (address64 & 0xFFFFFFFF).to_bytes(4, "big"),
            "BD": (3).to_bytes(4, "big"),
            "%V": (3300).to_bytes(2, "big"),
            "TP": (25).to_bytes(2, "big"),
            "DB": bytes((40,)),
            "MY": (0xFFFE).to_bytes(2, "big"),
        }
        self.saved = dict(self.params)
        self.saved_baud = baud
        self.software_resets = 0
        self.queued: dict[str, bytes] = {}
        self.radio_baud = baud
        self.host_baud = baud
        self.peer: FakeRadio | None = None
        self.loss = loss
        self.drop_next_rx = 0
        self.rng = random.Random(seed)
        self.sent_payloads: list[bytes] = []
        self._out = bytearray()
        self._cond = threading.Condition()
        self._parser = frames.FrameParser(escaped=api_mode == 2)
        self.closed = False

    @property
    def escaped(self) -> bool:
        return self.params["AP"] == b"\x02"

    def link(self, other: FakeRadio) -> None:
        self.peer, other.peer = other, self

    # -- Transport ---------------------------------------------------------------

    def configure(self, baud: int, flow_control: str) -> None:
        self.host_baud = baud

    def write(self, data: bytes, timeout: float) -> None:
        if self.host_baud != self.radio_baud:
            return  # garbage at the wrong baud rate
        self._parser.escaped = self.escaped
        for frame_data in self._parser.feed(data):
            self._handle(frame_data)

    def read(self, max_bytes: int, timeout: float) -> bytes:
        with self._cond:
            if not self._out:
                self._cond.wait(timeout)
            data = bytes(self._out[:max_bytes])
            del self._out[: len(data)]
            return data

    def reset_input(self) -> None:
        with self._cond:
            self._out.clear()

    def close(self) -> None:
        self.closed = True

    # -- radio behaviour ---------------------------------------------------------

    def emit(self, frame_data: bytes) -> None:
        with self._cond:
            self._out += frames.encode(frame_data, escaped=self.escaped)
            self._cond.notify_all()

    def _handle(self, frame_data: bytes) -> None:
        kind = frame_data[0]
        if kind in (frames.FrameType.AT_COMMAND, frames.FrameType.AT_COMMAND_QUEUE):
            self._at(frame_data, queue=kind == frames.FrameType.AT_COMMAND_QUEUE)
        elif kind == frames.FrameType.TRANSMIT_REQUEST:
            frame_id, dest = frame_data[1], int.from_bytes(frame_data[2:10], "big")
            self._transmit(frame_id, dest, frame_data[14:])

    def _at(self, frame_data: bytes, *, queue: bool) -> None:
        frame_id = frame_data[1]
        command = frame_data[2:4].decode()
        value = frame_data[4:]
        status, reply = 0, b""
        if command == "AC":
            self._apply()
        elif command == "WR":
            self.saved = dict(self.params)
            self.saved_baud = self.radio_baud
        elif command == "FR":
            response = bytes((frames.FrameType.AT_RESPONSE, frame_id)) + command.encode()
            self.emit(response + bytes((0,)))
            self.software_resets += 1
            self.params = dict(self.saved)
            self.radio_baud = self.saved_baud
            self.queued.clear()
            return
        elif value:
            self.queued[command] = value
            if not queue:
                self._apply()
        elif command in self.params:
            reply = self.params[command]
        else:
            status = 2
        response = bytes((frames.FrameType.AT_RESPONSE, frame_id)) + command.encode()
        self.emit(response + bytes((status,)) + reply)

    def _apply(self) -> None:
        self.params.update(self.queued)
        if "BD" in self.queued:
            code = int.from_bytes(self.queued["BD"], "big")
            self.radio_baud = {3: 9600, 7: 115200, 8: 230400}.get(code, code)
        self.queued.clear()

    def _transmit(self, frame_id: int, dest: int, data: bytes) -> None:
        np = self.params["NP"][0]
        if len(data) > np:
            self._status(frame_id, 0x74)
            return
        peer = self.peer
        if peer is None or peer.address64 != dest or self.rng.random() < self.loss:
            self._status(frame_id, 0x01)
            return
        self.sent_payloads.append(data)
        self._status(frame_id, 0x00)
        if peer.drop_next_rx:
            peer.drop_next_rx -= 1  # ACKed at the MAC but lost before the host
            return
        rx = bytes((frames.FrameType.RX_64,)) + self.address64.to_bytes(8, "big") + bytes((40, 0))
        peer.emit(rx + data)

    def _status(self, frame_id: int, status: int) -> None:
        self.emit(bytes((frames.FrameType.TRANSMIT_STATUS, frame_id, 0xFF, 0xFE, 0, status, 0)))


def wait_until(predicate: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met")
