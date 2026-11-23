# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Bring a radio from an unknown state to the configured one.

:func:`open_radio` finds the radio (any common baud rate, API or transparent
mode), queues the configured AT settings, applies them, switches the host to
the configured baud rate and flow control, and reads every setting back.
Settings are volatile unless ``[radio] write = true``. :func:`restore_radio`
discards them when the work is done, returning the radio to its saved
configuration, including its saved baud rate.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from .config import AtValue, LinkConfig
from .serial_port import PosixSerial, Transport
from .xbee import XBee, XBeeError, XBeeTimeout

STANDARD_BAUDS = (9600, 115200, 230400, 57600, 38400, 19200, 460800, 921600)
WRITE_ONLY_AT = frozenset({"KY"})
_PROBE_TIMEOUT_S = 0.5
_GUARD_TIME_S = 1.1
_BAUD_SETTLE_S = 0.1
_RESTART_SETTLE_S = 3.0  # ATFR answers first and restarts the radio about 2 s later

Log = Callable[[str], None]


class SetupError(XBeeError):
    """The radio could not be found or configured."""


@dataclass(frozen=True)
class RadioInfo:
    address64: int
    hardware_version: int
    firmware_version: int
    node_id: str
    channel: int
    pan_id: int
    address16: int
    power_level: int
    max_payload: int
    api_mode: int
    baud: int
    supply_mv: int | None
    temperature_c: int | None


def _noop(_message: str) -> None:
    pass


def _probe_api(xbee: XBee) -> int | None:
    """Return the radio's AP value if it answers an API frame, else None."""
    xbee.reset_input()
    try:
        value = xbee.at("AP", timeout=_PROBE_TIMEOUT_S)
    except XBeeError:
        return None
    return value[0] if len(value) == 1 and value[0] in (1, 2) else None


def _read_line(transport: Transport, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    line = bytearray()
    while (remaining := deadline - time.monotonic()) > 0:
        line += transport.read(64, remaining)
        if b"\r" in line:
            return bytes(line[: line.index(b"\r")]).strip()
    return bytes(line).strip()


def _transparent_to_api(transport: Transport, api_mode: int) -> bool:
    """Enter AT command mode with +++ and select API mode (volatile)."""
    transport.reset_input()
    time.sleep(_GUARD_TIME_S)
    transport.write(b"+++", 2.0)
    if _read_line(transport, 2.0) != b"OK":
        return False
    for command in (f"ATAP{api_mode}", "ATCN"):
        transport.write(command.encode("ascii") + b"\r", 2.0)
        if _read_line(transport, 2.0) != b"OK":
            return False
    return True


def _transparent_answers(transport: Transport) -> bool:
    """Enter and leave AT command mode without changing any setting."""
    transport.reset_input()
    time.sleep(_GUARD_TIME_S)
    transport.write(b"+++", 2.0)
    if _read_line(transport, 2.0) != b"OK":
        return False
    transport.write(b"ATCN\r", 2.0)
    return _read_line(transport, 2.0) == b"OK"


def find_radio(
    xbee: XBee, transport: Transport, bauds: list[int], api_mode: int, log: Log = _noop
) -> tuple[int, int]:
    """Locate the radio. Returns (baud, AP). Raises SetupError if not found."""
    for baud in bauds:
        transport.configure(baud, "none")
        xbee.escaped = False  # an AP query/response contains no escaped bytes
        ap = _probe_api(xbee)
        if ap is not None:
            return baud, ap
    log("no API-mode answer; trying transparent (AT command) mode")
    for baud in bauds:
        transport.configure(baud, "none")
        if _transparent_to_api(transport, api_mode):
            log(f"transparent-mode radio found at {baud} baud; selected AP={api_mode}")
            ap = _probe_api(xbee)
            if ap is not None:
                return baud, ap
    tried = ", ".join(str(b) for b in bauds)
    raise SetupError(f"no XBee answered at {tried} baud (API or transparent mode)")


def _matches(expected: AtValue, actual: bytes) -> bool:
    if isinstance(expected, str):
        return actual.decode("ascii", errors="replace").strip() == expected.strip()
    return int.from_bytes(actual, "big") == expected


def configure_radio(xbee: XBee, transport: Transport, config: LinkConfig, log: Log = _noop) -> None:
    """Find the radio, apply ``config`` and verify it. ``xbee`` must wrap ``transport``."""
    target_baud = config.target_baud
    bauds: list[int] = []
    for baud in (config.serial.baud, target_baud, *STANDARD_BAUDS):
        if baud is not None and baud not in bauds:
            bauds.append(baud)

    found_baud, found_ap = find_radio(xbee, transport, bauds, config.radio.api_mode, log)
    log(f"radio answered at {found_baud} baud in API mode AP={found_ap}")
    try:
        _apply_and_verify(xbee, transport, config, found_baud, found_ap, log)
    except BaseException:
        # A failed setup must not leave the radio at a half-applied baud rate.
        try:
            restore_radio(xbee, config, log)
        except (OSError, XBeeError) as exc:
            log(f"WARNING: could not restore the radio's saved settings: {exc}")
        raise


def _apply_and_verify(
    xbee: XBee, transport: Transport, config: LinkConfig, found_baud: int, found_ap: int, log: Log
) -> None:
    """Apply the configured settings, switch the host UART, and read them back."""
    target_baud = config.target_baud
    xbee.escaped = found_ap == 2

    settings: dict[str, AtValue] = dict(config.radio.at)
    if found_ap != config.radio.api_mode:
        settings["AP"] = config.radio.api_mode
    final_baud = target_baud or found_baud
    for name, value in settings.items():
        xbee.at(name, value, queue=True)
    if settings:
        try:
            xbee.at("AC")
        except XBeeTimeout:
            # The reply to a BD change can be lost while the radio switches rate.
            if final_baud == found_baud:
                raise
        log("applied " + ", ".join(f"{k}={_show(v)}" for k, v in settings.items()))

    time.sleep(_BAUD_SETTLE_S)
    transport.configure(final_baud, config.serial.flow_control)
    xbee.escaped = config.radio.api_mode == 2
    xbee.reset_input()

    try:
        ap = xbee.get_int("AP")
    except XBeeTimeout as exc:
        hint = (
            " with RTS/CTS; check the CTS/RTS wiring or try flow_control = \"none\""
            if config.serial.flow_control == "rtscts"
            else ""
        )
        raise SetupError(f"radio stopped answering at {final_baud} baud{hint}") from exc
    if ap != config.radio.api_mode:
        raise SetupError(f"AP reads back {ap}, expected {config.radio.api_mode}")
    mismatched = []
    for name, value in config.radio.at.items():
        if name in WRITE_ONLY_AT:
            continue
        actual = xbee.at(name)
        if not _matches(value, actual):
            mismatched.append(f"{name}={actual.hex().upper() or 'empty'} (wanted {_show(value)})")
    if mismatched:
        raise SetupError("settings did not apply: " + ", ".join(mismatched))
    if config.radio.write:
        xbee.write_settings()
        log("settings saved to flash (ATWR)")
    log(
        f"host UART now {final_baud} baud, flow control {config.serial.flow_control}, "
        f"AP={config.radio.api_mode}"
    )


def restore_radio(xbee: XBee, config: LinkConfig, log: Log = _noop) -> int:
    """Discard unsaved settings with a software reset and confirm the radio returned.

    The radio restarts with its saved configuration, so its baud rate, API mode
    and every other volatile setting go back to what is in flash. Nothing is
    written. Returns the baud rate the radio answers at afterward. Raises
    :class:`SetupError` if it cannot be reached.
    """
    transport = xbee.transport
    current = dict.fromkeys((config.target_baud, config.serial.baud, *STANDARD_BAUDS))
    bauds = [baud for baud in current if baud is not None]
    _baud, ap = find_radio(xbee, transport, bauds, config.radio.api_mode, log)
    xbee.escaped = ap == 2
    xbee.reset_input()
    xbee.at("FR")
    time.sleep(_RESTART_SETTLE_S)

    xbee.escaped = False  # an AP query/response contains no escaped bytes
    for baud in dict.fromkeys((config.serial.baud, *STANDARD_BAUDS)):
        transport.configure(baud, "none")
        if _probe_api(xbee) is not None or _transparent_answers(transport):
            log(f"radio restored to its saved settings; UART at {baud} baud")
            return baud
    raise SetupError("the radio did not answer after the software reset (ATFR)")


def open_radio(config: LinkConfig, log: Log = _noop, *, setup: bool = True) -> XBee:
    """Open the configured port and return a ready-to-use :class:`XBee`.

    With ``setup=False`` the port is opened at ``[serial] baud`` with the
    configured flow control and no radio settings are touched.
    """
    flow = "none" if setup else config.serial.flow_control
    transport = PosixSerial(config.serial.port, config.serial.baud, flow)
    xbee = XBee(
        transport,
        escaped=config.radio.api_mode == 2,
        timeout=config.radio.timeout_s,
        legacy_tx=config.radio.legacy_tx,
    )
    try:
        if setup:
            configure_radio(xbee, transport, config, log)
        elif _probe_api(xbee) is None:
            raise SetupError(
                f"no API-mode answer on {config.serial.port} at {config.serial.baud} baud"
            )
    except BaseException:
        xbee.close()
        raise
    return xbee


def read_info(xbee: XBee, baud: int) -> RadioInfo:
    """Read identity and RF settings for diagnostics."""

    def optional(command: str) -> int | None:
        try:
            return xbee.get_int(command)
        except XBeeError:
            return None

    temperature = optional("TP")
    if temperature is not None and temperature >= 0x8000:
        temperature -= 0x10000
    return RadioInfo(
        address64=xbee.address64(),
        hardware_version=xbee.get_int("HV"),
        firmware_version=xbee.get_int("VR"),
        node_id=xbee.get_str("NI").strip(),
        channel=xbee.get_int("CH"),
        pan_id=xbee.get_int("ID"),
        address16=xbee.get_int("MY"),
        power_level=xbee.get_int("PL"),
        max_payload=xbee.max_payload(),
        api_mode=xbee.get_int("AP"),
        baud=baud,
        supply_mv=optional("%V"),
        temperature_c=temperature,
    )


def _show(value: AtValue) -> str:
    return f"{value!r}" if isinstance(value, str) else f"0x{value:X}"
