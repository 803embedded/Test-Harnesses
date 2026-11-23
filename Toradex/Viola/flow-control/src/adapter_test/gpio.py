# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Sysfs GPIO access for the adapter wiring checks.

The implementation uses the legacy ``/sys/class/gpio`` interface, which
matches the global line numbers in :data:`config.VIOLA_IMX6ULL_X9_GPIOS`.

NOTE: sysfs GPIO is deprecated in mainline Linux in favour of libgpiod
(``/dev/gpiochipN``). A libgpiod backend is the intended long-term replacement;
it addresses lines by (chip, offset) rather than by global number, so the pin
map would need a chip+offset mapping added. :class:`SysfsBackend` only
establishes OS accessibility. :class:`SysfsLine` gives the wiring test control of
a line; continuity is proved there by pairing it with the XBee at the far end.
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


class GpioLineError(RuntimeError):
    """A GPIO line could not be claimed or controlled."""


class GpioLine(Protocol):
    """One host GPIO held by the wiring test."""

    def read(self) -> bool:
        """Return the sensed level (True = high)."""
        ...

    def drive(self, high: bool) -> None:
        """Drive the line as an output at the given level."""
        ...

    def release(self) -> None:
        """Stop driving the line (return it to input)."""
        ...

    def close(self) -> None:
        """Release the line and undo any export made when it was claimed."""
        ...


class SysfsLine:
    """A claimed sysfs GPIO. It starts as an input and is left as an input.

    A line that is already exported as an output belongs to something else, so
    it is refused rather than taken over.
    """

    def __init__(self, linux_gpio: int, settle_s: float = 0.05) -> None:
        self._number = linux_gpio
        self._dir = SYSFS_ROOT / f"gpio{linux_gpio}"
        self._exported_here = False
        try:
            if self._dir.exists():
                direction = (self._dir / "direction").read_text().strip()
                if direction != "in":
                    raise GpioLineError(
                        f"GPIO {linux_gpio} is already configured as {direction!r}; left unchanged"
                    )
            else:
                (SYSFS_ROOT / "export").write_text(str(linux_gpio))
                self._exported_here = True
                time.sleep(settle_s)
            (self._dir / "direction").write_text("in")
        except OSError as exc:
            self.close()
            raise GpioLineError(f"GPIO {linux_gpio} could not be claimed: {exc}") from exc

    def read(self) -> bool:
        try:
            value = (self._dir / "value").read_text().strip()
        except OSError as exc:
            raise GpioLineError(f"GPIO {self._number} read failed: {exc}") from exc
        if value not in ("0", "1"):
            raise GpioLineError(f"GPIO {self._number} returned invalid value {value!r}")
        return value == "1"

    def drive(self, high: bool) -> None:
        # Writing "high"/"low" sets direction and level together, without a glitch.
        try:
            (self._dir / "direction").write_text("high" if high else "low")
        except OSError as exc:
            raise GpioLineError(f"GPIO {self._number} drive failed: {exc}") from exc

    def release(self) -> None:
        try:
            (self._dir / "direction").write_text("in")
        except OSError as exc:
            raise GpioLineError(f"GPIO {self._number} release failed: {exc}") from exc

    def close(self) -> None:
        with contextlib.suppress(OSError, GpioLineError):
            self.release()
        if self._exported_here:
            self._exported_here = False
            with contextlib.suppress(OSError):
                (SYSFS_ROOT / "unexport").write_text(str(self._number))
