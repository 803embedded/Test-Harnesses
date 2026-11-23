# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Single-threaded XBee API-mode driver.

The driver owns a :class:`~xbee_link.serial_port.Transport` and a frame
parser. Every blocking call (:meth:`XBee.at`, :meth:`XBee.send`,
:meth:`XBee.receive`) services the serial port while it waits, so received
packets and modem status frames are never lost: packets are queued in
:attr:`XBee.received` and modem status frames in :attr:`XBee.modem_events`.

For pipelined transmission use :meth:`XBee.transmit` (returns a frame ID
immediately) together with :meth:`XBee.poll` and :meth:`XBee.take_status`.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

from . import frames
from .frames import AtResponse, ModemStatus, ReceivedPacket, TransmitStatus
from .serial_port import Transport

AT_STATUS_TEXT = {
    0: "OK",
    1: "ERROR",
    2: "invalid command",
    3: "invalid parameter",
    4: "remote transmission failure",
}

DELIVERY_STATUS_TEXT = {
    0x00: "success",
    0x01: "MAC ACK failure",
    0x02: "CCA/LBT failure",
    0x03: "purged / indirect transmission expired",
    0x15: "invalid destination endpoint",
    0x21: "network ACK failure",
    0x22: "not joined to network",
    0x23: "self-addressed",
    0x24: "address not found",
    0x25: "route not found",
    0x2C: "resource error: lack of free buffers",
    0x31: "internal resource error",
    0x32: "resource error",
    0x74: "payload too large",
}

MODEM_STATUS_TEXT = {
    0x00: "hardware reset or power up",
    0x01: "watchdog timer reset",
    0x06: "coordinator started",
    0x0D: "input voltage too high",
}

_READ_CHUNK = 4096


class XBeeError(RuntimeError):
    """Base error for driver failures."""


class XBeeTimeout(XBeeError):
    """The radio did not answer in time."""


class AtCommandError(XBeeError):
    def __init__(self, command: str, status: int) -> None:
        text = AT_STATUS_TEXT.get(status, f"status 0x{status:02X}")
        super().__init__(f"AT{command} failed: {text}")
        self.command = command
        self.status = status


class TransmitError(XBeeError):
    def __init__(self, status: int) -> None:
        super().__init__(f"transmit failed: {delivery_status_text(status)}")
        self.status = status


def delivery_status_text(status: int) -> str:
    return DELIVERY_STATUS_TEXT.get(status, f"delivery status 0x{status:02X}")


def modem_status_text(status: int) -> str:
    return MODEM_STATUS_TEXT.get(status, f"modem status 0x{status:02X}")


class XBee:
    """An XBee in API mode (AP=1 or AP=2) on a serial transport.

    ``legacy_tx`` selects the 802.15.4 legacy 0x00 TX Request instead of the
    0x10 Transmit Request; both report delivery through :class:`TransmitStatus`.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        escaped: bool = False,
        timeout: float = 2.0,
        write_timeout: float = 5.0,
        legacy_tx: bool = False,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.legacy_tx = legacy_tx
        self.received: deque[ReceivedPacket] = deque()
        self.modem_events: list[ModemStatus] = []
        self.on_modem_status: Callable[[ModemStatus], None] | None = None
        self.decode_errors = 0
        self._parser = frames.FrameParser(escaped=escaped)
        self._responses: dict[int, AtResponse | TransmitStatus] = {}
        self._next_id = 1
        self._max_payload: int | None = None

    # -- framing -----------------------------------------------------------------

    @property
    def escaped(self) -> bool:
        return self._parser.escaped

    @escaped.setter
    def escaped(self, value: bool) -> None:
        self._parser.escaped = value
        self._parser.reset()

    @property
    def checksum_errors(self) -> int:
        return self._parser.checksum_errors

    def next_frame_id(self) -> int:
        frame_id = self._next_id
        self._next_id = 1 if frame_id >= 255 else frame_id + 1
        self._responses.pop(frame_id, None)  # drop a stale reply to a reused ID
        return frame_id

    def send_frame(self, frame_data: bytes) -> None:
        self.transport.write(frames.encode(frame_data, escaped=self.escaped), self.write_timeout)

    def reset_input(self) -> None:
        self.transport.reset_input()
        self._parser.reset()

    def poll(self, timeout: float = 0.0) -> int:
        """Read whatever the radio has sent, waiting up to ``timeout``.

        Returns the number of frames processed.
        """
        data = self.transport.read(_READ_CHUNK, timeout)
        if not data:
            return 0
        count = 0
        for frame_data in self._parser.feed(data):
            try:
                frame = frames.decode(frame_data)
            except frames.FrameError:
                self.decode_errors += 1
                continue
            self._dispatch(frame)
            count += 1
        return count

    def _dispatch(self, frame: frames.Frame) -> None:
        if isinstance(frame, (AtResponse, TransmitStatus)):
            if frame.frame_id:
                self._responses[frame.frame_id] = frame
        elif isinstance(frame, ReceivedPacket):
            self.received.append(frame)
        elif isinstance(frame, ModemStatus):
            self.modem_events.append(frame)
            if self.on_modem_status is not None:
                self.on_modem_status(frame)

    def take_status(self, frame_id: int) -> TransmitStatus | None:
        """Return and forget the transmit status for ``frame_id``, if it arrived."""
        response = self._responses.get(frame_id)
        if isinstance(response, TransmitStatus):
            del self._responses[frame_id]
            return response
        return None

    def _wait_response(self, frame_id: int, timeout: float) -> AtResponse | TransmitStatus:
        deadline = time.monotonic() + timeout
        while True:
            response = self._responses.pop(frame_id, None)
            if response is not None:
                return response
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise XBeeTimeout(f"no response to frame {frame_id} within {timeout:.1f} s")
            self.poll(min(remaining, 0.1))

    # -- AT commands -------------------------------------------------------------

    def at(
        self,
        command: str,
        value: int | str | bytes | None = None,
        *,
        queue: bool = False,
        timeout: float | None = None,
    ) -> bytes:
        """Run a local AT command and return its value bytes.

        With ``value`` the parameter is set; ``queue=True`` defers applying it
        until ``AC`` (or a later non-queued command).
        """
        frame_id = self.next_frame_id()
        payload = b"" if value is None else frames.encode_value(value)
        self.send_frame(frames.at_command(frame_id, command, payload, queue=queue))
        response = self._wait_response(frame_id, self.timeout if timeout is None else timeout)
        if not isinstance(response, AtResponse):
            raise XBeeError(f"AT{command}: unexpected reply {response!r}")
        if response.status != 0:
            raise AtCommandError(command, response.status)
        return response.value

    def get_int(self, command: str) -> int:
        value = self.at(command)
        if not value:
            raise XBeeError(f"AT{command} returned no value")
        return int.from_bytes(value, "big")

    def get_str(self, command: str) -> str:
        return self.at(command).decode("ascii", errors="replace")

    def apply_changes(self) -> None:
        self.at("AC")

    def write_settings(self) -> None:
        """Save the current settings to non-volatile memory (ATWR)."""
        self.at("WR")

    def address64(self) -> int:
        return (self.get_int("SH") << 32) | self.get_int("SL")

    def max_payload(self) -> int:
        """Maximum RF payload per frame (ATNP), cached after the first read."""
        if self._max_payload is None:
            self._max_payload = self.get_int("NP")
        return self._max_payload

    def supply_voltage_mv(self) -> int:
        """Module supply voltage in millivolts (AT%V, XBee3)."""
        return self.get_int("%V")

    def last_rssi_dbm(self) -> int | None:
        """RSSI of the last received packet (ATDB); None if nothing received yet."""
        try:
            return -self.get_int("DB")
        except (AtCommandError, XBeeError):
            return None

    # -- data --------------------------------------------------------------------

    def transmit(self, dest64: int, data: bytes, *, options: int = 0) -> int:
        """Queue one RF frame and return its frame ID without waiting."""
        frame_id = self.next_frame_id()
        builder = frames.tx64_request if self.legacy_tx else frames.transmit_request
        self.send_frame(builder(frame_id, dest64, data, options=options))
        return frame_id

    def send(
        self, dest64: int, data: bytes, *, options: int = 0, timeout: float | None = None
    ) -> TransmitStatus:
        """Transmit one RF frame and wait for its delivery status.

        Raises :class:`TransmitError` if the radio reports a failure.
        """
        frame_id = self.transmit(dest64, data, options=options)
        response = self._wait_response(frame_id, self.timeout if timeout is None else timeout)
        if not isinstance(response, TransmitStatus):
            raise XBeeError(f"unexpected reply to transmit: {response!r}")
        if not response.ok:
            raise TransmitError(response.status)
        return response

    def receive(self, timeout: float = 0.0) -> ReceivedPacket | None:
        """Return the next received packet, waiting up to ``timeout``."""
        deadline = time.monotonic() + timeout
        while not self.received:
            remaining = deadline - time.monotonic()
            self.poll(max(0.0, min(remaining, 0.1)))
            if remaining <= 0 and not self.received:
                return None
        return self.received.popleft()

    def close(self) -> None:
        self.transport.close()
