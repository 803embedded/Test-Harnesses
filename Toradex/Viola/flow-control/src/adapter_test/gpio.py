# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Sysfs GPIO access for the adapter wiring checks.

The implementation uses the legacy ``/sys/class/gpio`` interface, which
matches the global line numbers in :data:`config.VIOLA_IMX6ULL_X9_GPIOS`.

NOTE: sysfs GPIO is deprecated in mainline Linux in favour of libgpiod
(``/dev/gpiochipN``). A libgpiod backend is the intended long-term replacement;
it addresses lines by (chip, offset) rather than by global number, so the pin
map would need a chip+offset mapping added. These backends only establish OS
accessibility; they do not prove electrical continuity through the adapter.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SYSFS_ROOT = Path("/sys/class/gpio")


@dataclass(frozen=True)
class GpioProbeResult:
    """Result of an OS-level GPIO accessibility check."""

    performed: bool
    passed: bool
    detail: str


class GpioBackend(Protocol):
    """Minimal backend used by GPIO accessibility checks."""

    def probe(self, linux_gpio: int) -> GpioProbeResult:
        """Safely check whether a GPIO line can be read."""
        ...


class SysfsBackend:
    """Backend using the legacy /sys/class/gpio interface."""

    def __init__(self, settle_s: float = 0.05) -> None:
        self._settle_s = settle_s

    def probe(self, linux_gpio: int) -> GpioProbeResult:
        gpio_dir = SYSFS_ROOT / f"gpio{linux_gpio}"
        exported_here = False
        try:
            if not gpio_dir.exists():
                (SYSFS_ROOT / "export").write_text(str(linux_gpio))
                exported_here = True
                time.sleep(self._settle_s)
                if not gpio_dir.is_dir():
                    return GpioProbeResult(
                        performed=True,
                        passed=False,
                        detail=f"GPIO {linux_gpio} did not appear after export",
                    )
            else:
                direction = (gpio_dir / "direction").read_text().strip()
                if direction != "in":
                    return GpioProbeResult(
                        performed=True,
                        passed=False,
                        detail=(
                            f"GPIO {linux_gpio} is already configured as {direction!r}; "
                            "left unchanged"
                        ),
                    )
            (gpio_dir / "direction").write_text("in")
            value = (gpio_dir / "value").read_text().strip()
            if value not in ("0", "1"):
                return GpioProbeResult(
                    performed=True,
                    passed=False,
                    detail=f"GPIO {linux_gpio} returned invalid value {value!r}",
                )
            return GpioProbeResult(
                performed=True,
                passed=True,
                detail=f"GPIO {linux_gpio} is accessible as input (value={value})",
            )
        except OSError as exc:
            return GpioProbeResult(
                performed=True,
                passed=False,
                detail=f"GPIO {linux_gpio} access failed: {exc}",
            )
        finally:
            if exported_here:
                with contextlib.suppress(OSError):
                    (SYSFS_ROOT / "unexport").write_text(str(linux_gpio))
