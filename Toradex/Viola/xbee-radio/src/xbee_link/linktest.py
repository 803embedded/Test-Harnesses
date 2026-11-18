# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Link-test scaffolding: live diagnostics, pass/fail rules and result output.

Nothing in the driver modules imports this file. To embed the driver in
another application, delete ``linktest.py`` and ``__main__.py``; keep
``frames``, ``serial_port``, ``xbee``, ``transfer``, ``config`` and
``radio_setup``.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, TextIO

from .config import format_address
from .radio_setup import RadioInfo
from .xbee import XBee, XBeeError, delivery_status_text


def log(message: str, stream: TextIO = sys.stdout) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", file=stream, flush=True)


def human_bytes(count: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(count) < 1024 or unit == "GiB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024
    return f"{count:.1f} GiB"


def human_duration(seconds: float) -> str:
    """Format seconds as ``45 s``, ``3 min 20 s`` or ``2 h 05 min``."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def parse_size(text: str) -> int:
    """Parse sizes such as ``4096``, ``256K``, ``1M`` (binary multiples)."""
    cleaned = text.strip().upper().removesuffix("B").removesuffix("I")
    multiplier = 1
    if cleaned and cleaned[-1] in "KMG":
        multiplier = 1024 ** ("KMG".index(cleaned[-1]) + 1)
        cleaned = cleaned[:-1]
    try:
        value = float(cleaned) * multiplier
    except ValueError as exc:
        raise ValueError(f"invalid size {text!r}") from exc
    if value < 0 or value > 0xFFFFFFFF:
        raise ValueError(f"size {text!r} out of range")
    return int(value)


@dataclass
class SupplyMonitor:
    """Samples the XBee's own supply voltage (AT%V) and temperature (ATTP).

    The XBee measures its VCC pin, so a sagging carrier 3.3 V rail under RF
    load shows up here. Sampling is rate-limited to keep the UART for data.
    """

    xbee: XBee
    interval_s: float = 2.0
    min_mv: int | None = None
    max_mv: int | None = None
    last_mv: int | None = None
    last_temp_c: int | None = None
    samples: int = 0
    errors: int = 0
    _next: float = 0.0

    def sample(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < self._next:
            return
        self._next = now + self.interval_s
        try:
            mv = self.xbee.supply_voltage_mv()
            temp = self.xbee.get_int("TP")
        except XBeeError:
            self.errors += 1
            return
        self.samples += 1
        self.last_mv = mv
        self.last_temp_c = temp - 0x10000 if temp >= 0x8000 else temp
        self.min_mv = mv if self.min_mv is None else min(self.min_mv, mv)
        self.max_mv = mv if self.max_mv is None else max(self.max_mv, mv)

    def describe(self) -> str:
        if self.last_mv is None:
            return "Vcc n/a"
        return f"Vcc {self.last_mv / 1000:.3f} V  {self.last_temp_c} C"


@dataclass
class TransferRecord:
    index: int
    ok: bool
    size: int
    duration_s: float
    error: str | None = None
    retransmissions: int = 0
    frames_sent: int = 0
    duplicates: int = 0
    delivery_failures: dict[str, int] = field(default_factory=dict)


@dataclass
class TestResult:
    role: str
    local: str
    peer: str | None
    min_supply_mv: int | None
    transfers: list[TransferRecord] = field(default_factory=list)
    supply_min_mv: int | None = None
    supply_max_mv: int | None = None
    radio_resets: int = 0
    checksum_errors: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures and bool(self.transfers) and all(t.ok for t in self.transfers)

    def finalize(self, xbee: XBee, monitor: SupplyMonitor) -> None:
        self.supply_min_mv = monitor.min_mv
        self.supply_max_mv = monitor.max_mv
        self.radio_resets = sum(1 for event in xbee.modem_events if event.is_reset)
        self.checksum_errors = xbee.checksum_errors
        if not self.transfers:
            self.failures.append("no transfer completed")
        if self.radio_resets:
            self.failures.append(f"radio reset {self.radio_resets} time(s) during the test")
        if (
            self.min_supply_mv is not None
            and monitor.min_mv is not None
            and monitor.min_mv < self.min_supply_mv
        ):
            self.failures.append(
                f"XBee supply fell to {monitor.min_mv} mV (limit {self.min_supply_mv} mV)"
            )

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["passed"] = self.passed
        return data


def failure_counts(failures: dict[int, int]) -> dict[str, int]:
    return {delivery_status_text(status): count for status, count in sorted(failures.items())}


def print_info(info: RadioInfo, port: str) -> None:
    supply = "n/a" if info.supply_mv is None else f"{info.supply_mv / 1000:.3f} V"
    temp = "n/a" if info.temperature_c is None else f"{info.temperature_c} C"
    rows = [
        ("serial port", f"{port} @ {info.baud} baud, AP={info.api_mode}"),
        ("64-bit address", format_address(info.address64)),
        ("16-bit address (MY)", f"0x{info.address16:04X}"),
        ("node identifier", repr(info.node_id)),
        (
            "hardware / firmware",
            f"HV 0x{info.hardware_version:04X} / VR 0x{info.firmware_version:04X}",
        ),
        ("channel / PAN ID", f"CH 0x{info.channel:02X} / ID 0x{info.pan_id:04X}"),
        ("power level (PL)", str(info.power_level)),
        ("max RF payload (NP)", f"{info.max_payload} bytes"),
        ("supply / temperature", f"{supply} / {temp}"),
    ]
    width = max(len(name) for name, _ in rows)
    for name, value in rows:
        log(f"  {name:<{width}}  {value}")


def print_summary(result: TestResult) -> None:
    ok = sum(1 for t in result.transfers if t.ok)
    total_bytes = sum(t.size for t in result.transfers if t.ok)
    total_time = sum(t.duration_s for t in result.transfers if t.ok)
    rate = human_bytes(total_bytes / total_time) + "/s" if total_time > 0 else "n/a"
    log(f"==== xbee-link {result.role} summary ====")
    log(f"  local / peer      {result.local} / {result.peer or 'any'}")
    log(f"  transfers         {ok}/{len(result.transfers)} verified")
    log(f"  data              {human_bytes(total_bytes)} in {total_time:.1f} s ({rate})")
    if result.role == "tx":
        retx = sum(t.retransmissions for t in result.transfers)
        frames = sum(t.frames_sent for t in result.transfers)
        log(f"  RF frames         {frames} sent, {retx} retransmitted")
        merged: dict[str, int] = {}
        for t in result.transfers:
            for name, count in t.delivery_failures.items():
                merged[name] = merged.get(name, 0) + count
        if merged:
            log("  delivery failures " + ", ".join(f"{k}: {v}" for k, v in merged.items()))
    else:
        log(f"  duplicates        {sum(t.duplicates for t in result.transfers)}")
    if result.supply_min_mv is not None and result.supply_max_mv is not None:
        limit = (
            "" if result.min_supply_mv is None else f" (limit {result.min_supply_mv / 1000:.2f} V)"
        )
        log(
            f"  XBee supply       min {result.supply_min_mv / 1000:.3f} V, "
            f"max {result.supply_max_mv / 1000:.3f} V{limit}"
        )
    log(f"  radio resets      {result.radio_resets}")
    log(f"  UART checksum err {result.checksum_errors}")
    for t in result.transfers:
        if not t.ok:
            log(f"  transfer {t.index} FAILED: {t.error}")
    for failure in result.failures:
        log(f"  FAIL: {failure}")
    log(f"  RESULT: {'PASS' if result.passed else 'FAIL'}")


def write_json(result: TestResult, path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(result.to_json(), handle, indent=2)
        handle.write("\n")
