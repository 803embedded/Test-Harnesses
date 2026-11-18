# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Minimal POSIX serial port using only the standard library (termios).

:class:`Transport` is the interface the driver needs. :class:`PosixSerial`
implements it for Linux UARTs; any object with the same methods (for example a
thin wrapper around pyserial) can be used instead.
"""

from __future__ import annotations

import os
import select
import time
from typing import Protocol

FLOW_CONTROL_MODES = ("none", "rtscts")


class Transport(Protocol):
    def configure(self, baud: int, flow_control: str) -> None:
        """Change line settings on the open port."""

    def write(self, data: bytes, timeout: float) -> None:
        """Write all bytes or raise TimeoutError."""

    def read(self, max_bytes: int, timeout: float) -> bytes:
        """Return available bytes, waiting up to ``timeout``; b"" on timeout."""

    def reset_input(self) -> None:
        """Discard unread input."""

    def close(self) -> None: ...


class PosixSerial:
    """Raw 8N1 serial port with optional RTS/CTS hardware flow control."""

    def __init__(self, port: str, baud: int = 9600, flow_control: str = "none") -> None:
        import termios

        self.port = port
        self.baud = baud
        self.flow_control = flow_control
        self._fd: int | None = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self.configure(baud, flow_control)
            termios.tcflush(self._fd, termios.TCIOFLUSH)
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> PosixSerial:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise OSError(f"serial port {self.port} is closed")
        return self._fd

    def configure(self, baud: int, flow_control: str) -> None:
        import termios

        if flow_control not in FLOW_CONTROL_MODES:
            raise ValueError(f"unknown flow control {flow_control!r}")
        speed = getattr(termios, f"B{baud}", None)
        if speed is None:
            raise ValueError(f"baud rate {baud} is not supported by termios")

        iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = termios.tcgetattr(self.fd)
        iflag &= ~(
            termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
            | termios.INLCR | termios.IGNCR | termios.ICRNL
            | termios.IXON | termios.IXOFF | termios.IXANY
        )
        oflag &= ~termios.OPOST
        lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
        cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
        cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
        if flow_control == "rtscts":
            cflag |= termios.CRTSCTS
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(
            self.fd, termios.TCSADRAIN, [iflag, oflag, cflag, lflag, speed, speed, cc]
        )
        self.baud = baud
        self.flow_control = flow_control

    def write(self, data: bytes, timeout: float) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + timeout
        while view:
            try:
                written = os.write(self.fd, view)
                view = view[written:]
                continue
            except BlockingIOError:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                hint = " (CTS held off by the radio?)" if self.flow_control == "rtscts" else ""
                raise TimeoutError(
                    f"serial write to {self.port} timed out with {len(view)} bytes left{hint}"
                )
            select.select([], [self.fd], [], remaining)

    def read(self, max_bytes: int, timeout: float) -> bytes:
        readable, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not readable:
            return b""
        try:
            return os.read(self.fd, max_bytes)
        except BlockingIOError:
            return b""

    def reset_input(self) -> None:
        import termios

        termios.tcflush(self.fd, termios.TCIFLUSH)

    def close(self) -> None:
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)
