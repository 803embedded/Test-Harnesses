# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""XBee radio probing through API frames or transparent AT command mode.

This module is platform-agnostic: it talks to whatever XBee is on the given
serial port. API-mode radios use the digi-xbee device interface. A radio in
transparent mode (AP=0) uses local AT command mode as a fallback.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

_T = TypeVar("_T")
_FLOW_CONTROL_ENABLED = b"\x01"
_TRANSPARENT_MODE_ERROR = "Unsupported operating mode: AT mode (0)"
_COMMAND_MODE_GUARD_S = 1.1
# Time for a driven line to settle before the far end is sensed.
_WIRE_SETTLE_S = 0.3


@dataclass
class RadioInfo:
    """Identity read back from a locally attached XBee."""

    hardware_version: str
    firmware_version: str
    address_64bit: str
    node_id: str
    pan_id: str


@dataclass
class WireObservation:
    """One flow-control wire tested by driving one end and sensing the other.

    ``reading_active`` and ``reading_idle`` are the sensed logic levels
    (True = high) when the driver asserted and released the line; ``expected_*``
    are the levels a correctly wired net should show. ``fault`` is set when the
    measurement could not be trusted (the driven end did not read back as set,
    the sensed end returned nothing, or an exception occurred); it explains
    exactly what went wrong so the caller can report a specific failure rather
    than a broken-wire guess. Classification (open / inverted / ok / fault) is
    the caller's.
    """

    wire: str  # "cts" or "rts"
    net: str  # human-readable net description
    reading_active: bool | None
    reading_idle: bool | None
    expected_active: bool
    expected_idle: bool
    fault: str | None = None


class RadioError(RuntimeError):
    """Raised when the radio cannot be reached or queried."""


def _close_quietly(device: Any) -> None:
    """Best-effort cleanup while preserving the original operation error."""
    try:
        if device.is_open():
            device.close()
    except Exception:
        pass


def _close_serial_quietly(serial: Any) -> None:
    """Best-effort cleanup for a raw XBee serial port."""
    try:
        if serial.is_open:
            serial.close()
    except Exception:
        pass


def _with_open_device(
    device_factory: Callable[..., Any],
    flow_control_value: Any,
    serial_port: str,
    baud_rate: int,
    operation_timeout_s: float,
    phase: str,
    operation: Callable[[Any], _T],
    *,
    write_timeout_s: float | None = None,
) -> _T:
    """Open, use, and close one XBee connection with consistent errors."""
    device: Any | None = None
    try:
        device = device_factory(serial_port, baud_rate, flow_control=flow_control_value)
        device.set_sync_ops_timeout(operation_timeout_s)
        if write_timeout_s is not None:
            # XBeeSerialPort derives from pyserial.Serial. A finite write timeout
            # keeps an asserted/stuck CTS from blocking this verification forever.
            device.comm_iface.write_timeout = write_timeout_s
        device.open()
        result = operation(device)
    except Exception as exc:
        if device is not None:
            _close_quietly(device)
        raise RadioError(f"{phase} failed on {serial_port} @ {baud_rate}: {exc}") from exc

    try:
        if device.is_open():
            device.close()
    except Exception as exc:
        raise RadioError(f"{phase} succeeded but closing {serial_port} failed: {exc}") from exc
    return result


def _read_identity(device: Any) -> RadioInfo:
    """Read identity fields; the AT exchanges also prove serial communication."""
    return RadioInfo(
        hardware_version=str(device.get_hardware_version()),
        firmware_version=device.get_firmware_version().hex(),
        address_64bit=str(device.get_64bit_addr()),
        node_id=device.get_node_id() or "",
        pan_id=device.get_pan_id().hex(),
    )


def _is_transparent_mode_error(exc: RadioError) -> bool:
    """Return whether digi-xbee rejected a radio specifically because AP is 0."""
    return str(exc.__cause__) == _TRANSPARENT_MODE_ERROR


def _write_all(serial: Any, payload: bytes) -> None:
    """Write one complete command or raise on a short write."""
    written = serial.write(payload)
    if written != len(payload):
        raise OSError(f"serial write accepted {written} of {len(payload)} bytes")
    serial.flush()


def _read_command_response(serial: Any, command: str, *, allow_empty: bool = False) -> str:
    """Send one local AT command and return its response without the terminator."""
    serial.reset_input_buffer()
    _write_all(serial, f"AT{command}\r".encode("ascii"))
    response = bytes(serial.read_until(b"\r"))
    if not response.endswith(b"\r"):
        raise TimeoutError(f"AT{command} did not return a complete response")
    try:
        text = response[:-1].decode("ascii").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise ValueError(f"AT{command} returned non-ASCII data: {response!r}") from exc
    if text == "ERROR":
        raise ValueError(f"AT{command} returned ERROR")
    if not text and not allow_empty:
        raise ValueError(f"AT{command} returned an empty response")
    return text


def _enter_command_mode(serial: Any) -> None:
    """Enter transparent AT command mode using the default guard time and character."""
    serial.reset_input_buffer()
    time.sleep(_COMMAND_MODE_GUARD_S)
    _write_all(serial, b"+++")
    response = bytes(serial.read_until(b"\r"))
    if response != b"OK\r":
        raise TimeoutError(
            f"transparent command-mode entry returned {response!r}, expected b'OK\\r'"
        )


def _with_command_mode(
    serial_factory: Callable[..., Any],
    flow_control_value: Any,
    serial_port: str,
    baud_rate: int,
    operation_timeout_s: float,
    phase: str,
    operation: Callable[[Any], _T],
) -> _T:
    """Open a raw serial connection, enter command mode, run an operation, and exit."""
    serial: Any | None = None
    try:
        serial = serial_factory(
            baud_rate,
            serial_port,
            flow_control=flow_control_value,
            timeout=operation_timeout_s,
        )
        serial.write_timeout = operation_timeout_s
        serial.open()
        _enter_command_mode(serial)
        result = operation(serial)
        if _read_command_response(serial, "CN") != "OK":
            raise ValueError("ATCN did not return OK")
    except Exception as exc:
        if serial is not None:
            _close_serial_quietly(serial)
        raise RadioError(f"{phase} failed on {serial_port} @ {baud_rate}: {exc}") from exc

    try:
        if serial.is_open:
            serial.close()
    except Exception as exc:
        raise RadioError(f"{phase} succeeded but closing {serial_port} failed: {exc}") from exc
    return result


def _read_hex_command(serial: Any, command: str) -> str:
    """Read and validate a hexadecimal local AT response."""
    value = _read_command_response(serial, command)
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"AT{command} returned invalid hexadecimal data: {value!r}") from exc
    return value


def _read_transparent_identity(serial: Any) -> RadioInfo:
    """Read radio identity through transparent AT command mode without writing settings."""
    ap = int(_read_hex_command(serial, "AP"), 16)
    if ap != 0:
        raise ValueError(f"transparent fallback expected AP=0, got AP={ap}")

    hardware_version = _read_hex_command(serial, "HV")
    firmware_version = _read_hex_command(serial, "VR")
    address_high = int(_read_hex_command(serial, "SH"), 16)
    address_low = int(_read_hex_command(serial, "SL"), 16)
    node_id = _read_command_response(serial, "NI", allow_empty=True)
    pan_id = _read_hex_command(serial, "ID")
    return RadioInfo(
        hardware_version=f"0x{hardware_version.upper()}",
        firmware_version=firmware_version.lower(),
        address_64bit=f"{address_high:08X}{address_low:08X}",
        node_id=node_id,
        pan_id=pan_id.lower(),
    )


def _enable_transparent_rtscts_volatile(serial: Any) -> None:
    """Queue AP=1 and RTS/CTS settings for volatile application by ATCN."""
    ap = int(_read_hex_command(serial, "AP"), 16)
    if ap != 0:
        raise ValueError(f"transparent fallback expected AP=0, got AP={ap}")

    d6 = int(_read_hex_command(serial, "D6"), 16)
    d7 = int(_read_hex_command(serial, "D7"), 16)
    if d6 != 1 or d7 != 1:
        if _read_command_response(serial, "D61") != "OK":
            raise ValueError("ATD61 did not return OK")
        if _read_command_response(serial, "D71") != "OK":
            raise ValueError("ATD71 did not return OK")
    if _read_command_response(serial, "AP1") != "OK":
        raise ValueError("ATAP1 did not return OK")


def _parameter_byte(device: Any, name: str) -> int:
    value = device.get_parameter(name)
    if not isinstance(value, (bytes, bytearray)) or len(value) != 1:
        raise ValueError(f"AT{name} returned an invalid value: {value!r}")
    return value[0]


def _enable_rtscts_volatile(device: Any) -> None:
    """Enable XBee RTS/CTS for this power session; deliberately do not issue WR."""
    d6 = _parameter_byte(device, "D6")
    d7 = _parameter_byte(device, "D7")
    if d6 != 1 or d7 != 1:
        device.set_parameter("D6", _FLOW_CONTROL_ENABLED, apply=False)
        device.set_parameter("D7", _FLOW_CONTROL_ENABLED, apply=False)
        device.apply_changes()


def _verify_rtscts_and_read_identity(device: Any) -> RadioInfo:
    """Confirm D6/D7 over the reopened CRTSCTS connection, then read identity."""
    d6 = _parameter_byte(device, "D6")
    d7 = _parameter_byte(device, "D7")
    if d6 != 1 or d7 != 1:
        raise ValueError(
            f"XBee flow control did not apply (D6={d6}, D7={d7})"
        )
    return _read_identity(device)


def probe_radio(
    serial_port: str,
    baud_rate: int,
    open_timeout_s: float,
    flow_control: str = "none",
    write_timeout_s: float = 5.0,
) -> RadioInfo:
    """Configure the requested UART mode, probe the local XBee, and return it.

    ``none`` opens in 3-wire mode and never changes radio settings. A radio in
    transparent mode is read through local AT command mode. ``rtscts`` first
    opens in 3-wire mode, sets D6/D7 to 1, selects AP=1 when needed, and applies
    the settings without WR. It then reopens with CRTSCTS and a finite write
    timeout.

    Raises RadioError on any failure (library missing, port not present, radio
    not responding, or an unknown flow-control mode).
    """
    try:
        from digi.xbee.devices import XBeeDevice
        from digi.xbee.serial import FlowControl, XBeeSerialPort
    except ImportError as exc:  # pragma: no cover - depends on install env
        raise RadioError(
            "digi-xbee is not installed; install it with 'pip install digi-xbee'"
        ) from exc

    if flow_control == "none":
        try:
            return _with_open_device(
                XBeeDevice,
                FlowControl.NONE,
                serial_port,
                baud_rate,
                open_timeout_s,
                "3-wire radio probe",
                _read_identity,
            )
        except RadioError as exc:
            if not _is_transparent_mode_error(exc):
                raise
            return _with_command_mode(
                XBeeSerialPort,
                FlowControl.NONE,
                serial_port,
                baud_rate,
                open_timeout_s,
                "3-wire transparent-mode radio probe",
                _read_transparent_identity,
            )

    if flow_control != "rtscts":
        raise RadioError(
            f"unknown flow-control mode {flow_control!r} (expected 'none' or 'rtscts')"
        )

    try:
        _with_open_device(
            XBeeDevice,
            FlowControl.NONE,
            serial_port,
            baud_rate,
            open_timeout_s,
            "3-wire XBee flow-control setup",
            _enable_rtscts_volatile,
        )
    except RadioError as exc:
        if not _is_transparent_mode_error(exc):
            raise
        _with_command_mode(
            XBeeSerialPort,
            FlowControl.NONE,
            serial_port,
            baud_rate,
            open_timeout_s,
            "3-wire transparent-mode XBee flow-control setup",
            _enable_transparent_rtscts_volatile,
        )
    return _with_open_device(
        XBeeDevice,
        FlowControl.HARDWARE_RTS_CTS,
        serial_port,
        baud_rate,
        open_timeout_s,
        "RTS/CTS verification",
        _verify_rtscts_and_read_identity,
        write_timeout_s=write_timeout_s,
    )


def _kernel_rts_asserted(serial: Any) -> bool:
    """Read the RTS line the kernel is actually driving (TIOCM_RTS)."""
    import fcntl
    import struct
    import termios

    # termios/fcntl ioctls are Linux-only; mypy runs on the dev host (Windows).
    packed = fcntl.ioctl(  # type: ignore[attr-defined, unused-ignore]
        serial.fileno(),
        termios.TIOCMGET,  # type: ignore[attr-defined, unused-ignore]
        struct.pack("I", 0),
    )
    return bool(
        struct.unpack("I", packed)[0]
        & termios.TIOCM_RTS  # type: ignore[attr-defined, unused-ignore]
    )


def _sample_dio_high(device: Any, line: Any) -> bool | None:
    """Sample one DIO line; True if high, False if low, None if not reported."""
    from digi.xbee.io import IOValue

    value = device.read_io_sample().get_digital_value(line)
    if value is None:
        return None
    return bool(value == IOValue.HIGH)


def _diagnose_cts_wire(device: Any) -> WireObservation:
    """Drive the XBee CTS# output (D7) and sense the module's CTS input."""
    net = "XBee CTS# -> Colibri UART_B_CTS (SODIMM_32 / X9.39)"
    serial = device.comm_iface
    try:
        device.set_parameter("D7", _FLOW_CONTROL_ENABLED, apply=True)
        readback_active = _parameter_byte(device, "D7")
        time.sleep(_WIRE_SETTLE_S)
        cts_active = bool(serial.cts)
        device.set_parameter("D7", b"\x00", apply=True)
        readback_idle = _parameter_byte(device, "D7")
        time.sleep(_WIRE_SETTLE_S)
        cts_idle = bool(serial.cts)
        device.set_parameter("D7", _FLOW_CONTROL_ENABLED, apply=True)  # restore
    except Exception as exc:
        return WireObservation(
            "cts", net, None, None, True, False, fault=f"error driving XBee D7: {exc}"
        )
    fault = None
    if readback_active != 1 or readback_idle != 0:
        fault = (
            "the XBee did not apply the CTS# drive (requested D7=1 then D7=0, "
            f"read back {readback_active} then {readback_idle}); cannot judge the wire"
        )
    # D7=1 idle radio asserts CTS# (module senses high/True); D7=0 releases it.
    return WireObservation(
        wire="cts",
        net=net,
        reading_active=cts_active,
        reading_idle=cts_idle,
        expected_active=True,
        expected_idle=False,
        fault=fault,
    )


def _restore_rts_flow_control(device: Any, serial: Any) -> None:
    """Re-enable the XBee RTS# input (D6=1) after the RTS wire test.

    Module RTS is asserted first: with a good wire and RTS# flow control back
    on, a deasserted module RTS silences the XBee, so the response to this very
    command would never arrive. Best-effort: a restore failure must not mask a
    valid measurement, and the volatile setting resets on the next power cycle.
    """
    try:
        serial.rts = True
        device.set_parameter("D6", _FLOW_CONTROL_ENABLED, apply=True)
    except Exception:
        pass


def _diagnose_rts_wire(device: Any) -> WireObservation:
    """Drive the module RTS output and sense it at the XBee RTS# pin (DIO6)."""
    net = "Colibri UART_B_RTS (SODIMM_34 / X9.38) -> XBee RTS#"
    serial = device.comm_iface
    try:
        from digi.xbee.io import IOLine, IOMode

        device.set_io_configuration(IOLine.DIO6, IOMode.DIGITAL_IN)
        d6 = _parameter_byte(device, "D6")
        serial.rts = True
        time.sleep(_WIRE_SETTLE_S)
        driver_asserted = _kernel_rts_asserted(serial)
        dio_active = _sample_dio_high(device, IOLine.DIO6)
        serial.rts = False
        time.sleep(_WIRE_SETTLE_S)
        driver_released = not _kernel_rts_asserted(serial)
        dio_idle = _sample_dio_high(device, IOLine.DIO6)
    except Exception as exc:
        return WireObservation(
            "rts", net, None, None, False, True, fault=f"error driving module RTS: {exc}"
        )
    finally:
        # Cleanup only; must run whether or not the measurement raised.
        _restore_rts_flow_control(device, serial)
    fault = None
    if d6 != 3:
        fault = (
            f"the XBee did not configure DIO6 as an input (D6 read back {d6}); "
            "cannot judge the wire"
        )
    elif not (driver_asserted and driver_released):
        fault = (
            "the module did not toggle its RTS output "
            f"(kernel RTS asserted={driver_asserted}, then released={driver_released}); "
            "cannot judge the wire"
        )
    elif dio_active is None or dio_idle is None:
        fault = "the XBee did not report DIO6 in its IO sample; cannot judge the wire"
    # RTS asserted drives the line low (DIO6 low/False); released is high/True.
    return WireObservation(
        wire="rts",
        net=net,
        reading_active=dio_active,
        reading_idle=dio_idle,
        expected_active=False,
        expected_idle=True,
        fault=fault,
    )


def _select_api_mode(serial: Any) -> None:
    """Command-mode operation: switch a transparent radio to API (AP=1)."""
    if _read_command_response(serial, "AP1") != "OK":
        raise ValueError("ATAP1 did not return OK")


def diagnose_flow_control_wiring(
    serial_port: str,
    baud_rate: int,
    open_timeout_s: float,
) -> list[WireObservation]:
    """Isolate which flow-control wire is faulty after an rtscts failure.

    Runs in API mode (the rtscts path already selects AP=1, but a transparent
    radio is switched to AP=1 volatile as a fallback). Each wire is tested by
    driving one end with read-back confirmation and sensing the other, so an
    open or misrouted net is distinguished from a setting that never applied.

    Raises RadioError if the radio cannot be opened for the diagnostic.
    """
    try:
        from digi.xbee.devices import XBeeDevice
        from digi.xbee.serial import FlowControl, XBeeSerialPort
    except ImportError as exc:  # pragma: no cover - depends on install env
        raise RadioError(
            "digi-xbee is not installed; install it with 'pip install digi-xbee'"
        ) from exc

    def _open_api() -> Any:
        device = XBeeDevice(serial_port, baud_rate, flow_control=FlowControl.NONE)
        device.set_sync_ops_timeout(open_timeout_s)
        device.open()
        return device

    try:
        device = _open_api()
    except Exception as exc:
        if str(exc) != _TRANSPARENT_MODE_ERROR:
            raise RadioError(
                f"wiring diagnostic could not open {serial_port} @ {baud_rate}: {exc}"
            ) from exc
        _with_command_mode(
            XBeeSerialPort,
            FlowControl.NONE,
            serial_port,
            baud_rate,
            open_timeout_s,
            "transparent-mode API switch",
            _select_api_mode,
        )
        try:
            device = _open_api()
        except Exception as exc2:
            raise RadioError(
                f"wiring diagnostic could not switch {serial_port} to API mode: {exc2}"
            ) from exc2

    try:
        return [_diagnose_cts_wire(device), _diagnose_rts_wire(device)]
    finally:
        _close_quietly(device)
