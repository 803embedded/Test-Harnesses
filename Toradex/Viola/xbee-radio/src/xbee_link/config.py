# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""User configuration: which serial port, and which AT settings to apply.

Configuration is a TOML file (see ``examples/``)::

    [serial]
    port = "/dev/ttymxc1"
    baud = 9600              # first baud rate tried when looking for the radio
    flow_control = "rtscts"  # "rtscts" or "none"

    [radio]
    api_mode = 1             # AP=1 (unescaped) or AP=2 (escaped)
    write = false            # true saves the settings with ATWR

    [radio.at]               # any settable AT command, applied and verified
    BD = 7                   # 115200 baud
    CH = 0x0C
    ID = 0x3332

    [link]
    peer = "0013A200XXXXXXXX"  # default destination for transfers
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .serial_port import FLOW_CONTROL_MODES

# Commands that are actions, or are managed by the setup code itself.
RESERVED_AT = frozenset({"AC", "AP", "CN", "FR", "RE", "WR"})
_AT_NAME = re.compile(r"^[A-Z0-9%]{2}$")

AtValue = int | str


class ConfigError(ValueError):
    """The configuration file is invalid."""


@dataclass
class SerialConfig:
    port: str = "/dev/ttymxc1"
    baud: int = 9600
    flow_control: str = "rtscts"


@dataclass
class RadioConfig:
    api_mode: int = 1
    write: bool = False
    legacy_tx: bool = False
    timeout_s: float = 2.0
    at: dict[str, AtValue] = field(default_factory=dict)


@dataclass
class LinkConfig:
    serial: SerialConfig = field(default_factory=SerialConfig)
    radio: RadioConfig = field(default_factory=RadioConfig)
    peer: int | None = None

    @classmethod
    def load(cls, path: str | Path) -> LinkConfig:
        try:
            with open(path, "rb") as handle:
                raw = tomllib.load(handle)
        except OSError as exc:
            raise ConfigError(f"cannot read config {path}: {exc}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LinkConfig:
        _reject_unknown(raw, {"serial", "radio", "link"}, "top level")
        serial_raw = _table(raw, "serial")
        radio_raw = _table(raw, "radio")
        link_raw = _table(raw, "link")
        _reject_unknown(serial_raw, {"port", "baud", "flow_control"}, "[serial]")
        _reject_unknown(radio_raw, {"api_mode", "write", "legacy_tx", "timeout", "at"}, "[radio]")
        _reject_unknown(link_raw, {"peer"}, "[link]")

        config = cls()
        config.serial.port = _typed(serial_raw, "port", str, config.serial.port)
        config.serial.baud = _typed(serial_raw, "baud", int, config.serial.baud)
        config.serial.flow_control = _typed(
            serial_raw, "flow_control", str, config.serial.flow_control
        )
        config.radio.api_mode = _typed(radio_raw, "api_mode", int, config.radio.api_mode)
        config.radio.write = _typed(radio_raw, "write", bool, config.radio.write)
        config.radio.legacy_tx = _typed(radio_raw, "legacy_tx", bool, config.radio.legacy_tx)
        timeout = radio_raw.get("timeout", config.radio.timeout_s)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ConfigError("[radio] timeout must be a number")
        config.radio.timeout_s = float(timeout)

        at_raw = _table(radio_raw, "at")
        for name, value in at_raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ConfigError(f"[radio.at] {name} must be an integer or a string")
            config.radio.at[name.upper()] = value

        peer = link_raw.get("peer")
        if peer is not None:
            if not isinstance(peer, str):
                raise ConfigError('[link] peer must be a string such as "0013A20012345678"')
            config.peer = parse_address(peer)
        config.validate()
        return config

    def validate(self) -> None:
        self.serial.port = self.serial.port.strip()
        if not self.serial.port:
            raise ConfigError("[serial] port must not be empty")
        if self.serial.baud <= 0:
            raise ConfigError("[serial] baud must be positive")
        self.serial.flow_control = self.serial.flow_control.strip().lower()
        if self.serial.flow_control not in FLOW_CONTROL_MODES:
            raise ConfigError(f"[serial] flow_control must be one of {FLOW_CONTROL_MODES}")
        if self.radio.api_mode not in (1, 2):
            raise ConfigError("[radio] api_mode must be 1 or 2")
        if self.radio.timeout_s <= 0:
            raise ConfigError("[radio] timeout must be positive")
        for name, value in self.radio.at.items():
            if not _AT_NAME.match(name):
                raise ConfigError(f"[radio.at] {name!r} is not a two-character AT command")
            if name in RESERVED_AT:
                hint = " (use [radio] api_mode)" if name == "AP" else ""
                raise ConfigError(f"[radio.at] {name} cannot be set here{hint}")
            if isinstance(value, int) and value < 0:
                raise ConfigError(f"[radio.at] {name} must not be negative")
            if isinstance(value, str) and not value.isascii():
                raise ConfigError(f"[radio.at] {name} must be ASCII")
        if "BD" in self.radio.at:
            bd_to_baud(self.radio.at["BD"])  # validates
        if self.serial.flow_control == "rtscts":
            for pin in ("D6", "D7"):
                if self.radio.at.get(pin, 1) != 1:
                    raise ConfigError(f"[radio.at] {pin} must be 1 when flow_control is rtscts")

    @property
    def target_baud(self) -> int | None:
        """Host baud after setup, or None to keep whatever the radio uses."""
        value = self.radio.at.get("BD")
        return None if value is None else bd_to_baud(value)


# ATBD codes; XBee3 also accepts a literal baud rate as the value.
BD_CODES = {
    0: 1200, 1: 2400, 2: 4800, 3: 9600, 4: 19200, 5: 38400,
    6: 57600, 7: 115200, 8: 230400, 9: 460800, 10: 921600,
}


def bd_to_baud(value: AtValue) -> int:
    if isinstance(value, str):
        raise ConfigError("[radio.at] BD must be an integer")
    if value in BD_CODES:
        return BD_CODES[value]
    if value >= 1200:
        return value
    raise ConfigError(f"[radio.at] BD={value} is neither a BD code (0-10) nor a baud rate")


def parse_address(text: str) -> int:
    cleaned = text.strip().replace(" ", "").replace(":", "")
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    if not cleaned or len(cleaned) > 16:
        raise ConfigError(f"invalid 64-bit address {text!r}")
    try:
        return int(cleaned, 16)
    except ValueError as exc:
        raise ConfigError(f"invalid 64-bit address {text!r}") from exc


def format_address(address: int | None) -> str:
    return "unknown" if address is None else f"{address:016X}"


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _typed(raw: dict[str, Any], key: str, kind: type, default: Any) -> Any:
    value = raw.get(key, default)
    if kind is int and isinstance(value, bool):
        raise ConfigError(f"{key} must be an integer")
    if not isinstance(value, kind):
        raise ConfigError(f"{key} must be of type {kind.__name__}")
    return value


def _reject_unknown(raw: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) in {where}: {', '.join(unknown)}")
