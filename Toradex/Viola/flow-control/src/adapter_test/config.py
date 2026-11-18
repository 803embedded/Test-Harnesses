# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Configuration and the Viola + iMX6ULL GPIO pin map.

The pin map is carrier- and module-specific (Toradex Viola Extension Connector
X9 + Colibri iMX6ULL Linux line numbers). The rest of this package is
platform-agnostic and talks to the XBee over a serial port.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PinInfo:
    """One GPIO usable through the Viola X9 connector on a Colibri iMX6ULL."""

    name: str
    sodimm: int
    x9_pin: int
    linux_gpio: int
    xbee_signal: str | None = None  # XBee signal on the 803-001-001 adapter, if any
    availability_note: str | None = None


# Only GPIOs physically routed between the Viola X9 connector and an XBee signal
# on the adapter are included. The global sysfs numbers were confirmed against
# `sudo cat /sys/kernel/debug/gpio` and the Viola datasheet section 3.8.1.
VIOLA_IMX6ULL_X9_GPIOS: tuple[PinInfo, ...] = (
    PinInfo("SODIMM_135", 135, 8, 24, "RESET"),
    PinInfo("SODIMM_98", 98, 9, 122, "RSSI"),
    PinInfo("SODIMM_133", 133, 10, 110, "DTR"),
    PinInfo("SODIMM_103", 103, 11, 118, "ON_SLEEP", "not connected on Wi-Fi SoMs"),
)

PIN_MAP: dict[str, PinInfo] = {pin.name: pin for pin in VIOLA_IMX6ULL_X9_GPIOS}

# Number of X9 GPIOs connected to XBee signals on the adapter.
NUM_EXPECTED_PINS = 4

# XBee3 factory default UART settings.
DEFAULT_BAUD_RATE = 9600
DEFAULT_OPEN_TIMEOUT_S = 5.0
DEFAULT_WRITE_TIMEOUT_S = 5.0

# On the 803-001-001 adapter the XBee UART is on Colibri UART_B = iMX6ULL
# UART2, which is /dev/ttymxc1 on a standard Toradex image (ttymxc0 is the
# UART_A debug console). Confirm on target with `dmesg | grep ttymxc`, and
# override with the XBEE_PORT environment variable or --port if it differs.
DEFAULT_SERIAL_PORT = "/dev/ttymxc1"

# UART flow control: "none" (3-wire) or "rtscts" (hardware RTS/CTS, default)
# The XBee must have RTS/CTS enabled (ATD6=1, ATD7=1), and the Colibri UART2
# device tree must declare `uart-has-rtscts`. Stock Torizon OS Upstream 7.4.0
# has the required UART2 properties and pad muxing (verified 2026-09-04).
DEFAULT_FLOW_CONTROL = "rtscts"

DEFAULT_MODULE_VARIANT = "imx6ull-1gb-it"
WIFI_MODULE_VARIANTS = (
    "imx6ull-512mb-wb-it",
    "imx6ull-512mb-wb",
)
SUPPORTED_MODULE_VARIANTS = (
    "imx6ull-1gb-it",
    "imx6ull-512mb-it",
    "imx6ull-256mb",
    *WIFI_MODULE_VARIANTS,
)

WIFI_UNAVAILABLE_GPIO_PINS = frozenset({"SODIMM_103"})


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"${name} must be an integer, got {value!r}") from exc


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"${name} must be a number, got {value!r}") from exc


@dataclass
class ProbeConfig:
    """Runtime configuration for a test run."""

    serial_port: str = DEFAULT_SERIAL_PORT
    baud_rate: int = DEFAULT_BAUD_RATE
    flow_control: str = DEFAULT_FLOW_CONTROL  # "none" or "rtscts"
    module_variant: str = DEFAULT_MODULE_VARIANT
    open_timeout_s: float = DEFAULT_OPEN_TIMEOUT_S
    write_timeout_s: float = DEFAULT_WRITE_TIMEOUT_S
    gpio_pins: tuple[str, ...] = field(default_factory=tuple)
    check_gpio: bool = False

    @classmethod
    def from_env(cls) -> ProbeConfig:
        """Build a config from environment variables, falling back to defaults."""
        return cls(
            serial_port=os.environ.get("XBEE_PORT", DEFAULT_SERIAL_PORT),
            baud_rate=_env_int("XBEE_BAUD", DEFAULT_BAUD_RATE),
            flow_control=os.environ.get("XBEE_FLOW_CONTROL", DEFAULT_FLOW_CONTROL),
            module_variant=os.environ.get("TARGET_MODULE", DEFAULT_MODULE_VARIANT),
            open_timeout_s=_env_float("XBEE_OPEN_TIMEOUT", DEFAULT_OPEN_TIMEOUT_S),
            write_timeout_s=_env_float("XBEE_WRITE_TIMEOUT", DEFAULT_WRITE_TIMEOUT_S),
        )

    def validate(self) -> None:
        """Normalize and validate configuration before any hardware access."""
        self.serial_port = self.serial_port.strip()
        if not self.serial_port:
            raise ValueError("serial port must not be empty")
        if self.baud_rate <= 0:
            raise ValueError(f"baud rate must be positive, got {self.baud_rate}")
        if self.open_timeout_s <= 0:
            raise ValueError(f"open timeout must be positive, got {self.open_timeout_s}")
        if self.write_timeout_s <= 0:
            raise ValueError(f"write timeout must be positive, got {self.write_timeout_s}")

        self.flow_control = self.flow_control.strip().lower()
        if self.flow_control not in ("none", "rtscts"):
            raise ValueError(
                f"unknown flow-control mode {self.flow_control!r}; expected 'none' or 'rtscts'"
            )

        if self.module_variant not in SUPPORTED_MODULE_VARIANTS:
            raise ValueError(f"unsupported module {self.module_variant!r}")

        unknown_pins = sorted(set(self.gpio_pins) - PIN_MAP.keys())
        if unknown_pins:
            raise ValueError(f"unknown GPIO pin(s): {', '.join(unknown_pins)}")
        unavailable_requested = sorted(set(self.gpio_pins) & self.unavailable_gpio_pins())
        if self.check_gpio and unavailable_requested:
            raise ValueError(
                "GPIO pin(s) unavailable on the selected module: "
                + ", ".join(unavailable_requested)
            )

    def unavailable_gpio_pins(self) -> frozenset[str]:
        """Return mapped GPIO pins not exposed by the selected module."""
        unavailable: set[str] = set()
        if self.module_variant in WIFI_MODULE_VARIANTS:
            unavailable.update(WIFI_UNAVAILABLE_GPIO_PINS)
        return frozenset(unavailable)

    def gpio_pins_to_check(self) -> tuple[str, ...]:
        """Return explicitly requested pins or all mapped pins available on the module."""
        if self.gpio_pins:
            return self.gpio_pins
        unavailable = self.unavailable_gpio_pins()
        return tuple(name for name in PIN_MAP if name not in unavailable)
