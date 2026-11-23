# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Orchestration of the post-assembly checks.

A run produces a list of CheckResult records and an overall pass/fail. The
radio check is the headline test; the GPIO check is optional and off by default.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from . import flow_control, gpio
from .config import PIN_MAP, ProbeConfig
from .radio import (
    GpioWire,
    RadioError,
    WireObservation,
    diagnose_flow_control_wiring,
    diagnose_gpio_wiring,
    probe_radio,
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    skipped: bool = False


def check_radio(config: ProbeConfig, log: logging.Logger) -> list[CheckResult]:
    """Open the local XBee and read back its identity."""
    sequence = "3-wire probe"
    if config.flow_control == "rtscts":
        sequence = "3-wire D6/D7 setup, then RTS/CTS verification"
    log.info(
        "Probing XBee on %s @ %d baud (%s)",
        config.serial_port,
        config.baud_rate,
        sequence,
    )
    try:
        info = probe_radio(
            config.serial_port,
            config.baud_rate,
            config.open_timeout_s,
            config.flow_control,
            config.write_timeout_s,
        )
    except RadioError as exc:
        log.error("Radio probe failed: %s", exc)
        results = [CheckResult("radio", passed=False, detail=str(exc))]
        if config.flow_control == "rtscts":
            # An rtscts probe failure usually means a flow-control wire is bad.
            # Isolate which net so the report names the exact wire to rework.
            results.extend(diagnose_flow_control_wiring_checks(config, log))
        return results

    detail = (
        f"HW={info.hardware_version} FW={info.firmware_version} "
        f"addr={info.address_64bit} NI='{info.node_id}' PAN={info.pan_id}"
    )
    log.info("Radio OK: %s", detail)
    results = [CheckResult("radio", passed=True, detail=detail)]
    if config.flow_control == "rtscts":
        # A passing probe cannot detect a line stuck in its asserted state, so
        # each wire is always toggled and sensed at the far end.
        results.extend(diagnose_flow_control_wiring_checks(config, log))
    return results


def classify_wire(obs: WireObservation, prefix: str = "flow-control-wire") -> CheckResult:
    """Turn a raw wire observation into a pass/fail result naming the net.

    Every non-PASS outcome is a FAIL: a broken/misrouted wire, and also the case
    where the test could not be trusted (``fault``). The latter is a real failure
    of the check, so it is reported with the exact reason rather than skipped.
    """
    name = f"{prefix}:{obs.wire}"
    if obs.fault is not None:
        return CheckResult(
            name, passed=False, detail=f"{obs.net}: could not test this wire: {obs.fault}"
        )
    if obs.reading_active is None or obs.reading_idle is None:
        return CheckResult(
            name,
            passed=False,
            detail=f"{obs.net}: could not test this wire: the sensed end returned no reading",
        )
    if obs.reading_active == obs.reading_idle:
        return CheckResult(
            name,
            passed=False,
            detail=f"{obs.net}: OPEN or misrouted (the sensed end did not follow the driven end)",
        )
    if obs.reading_active == obs.expected_active and obs.reading_idle == obs.expected_idle:
        return CheckResult(
            name, passed=True, detail=f"{obs.net}: OK (the sensed end tracked the driven end)"
        )
    return CheckResult(
        name,
        passed=False,
        detail=f"{obs.net}: connected but INVERTED (tracks with reversed polarity)",
    )


def diagnose_flow_control_wiring_checks(
    config: ProbeConfig, log: logging.Logger
) -> list[CheckResult]:
    """Run the per-wire flow-control test and report the result for each net."""
    log.info("Testing RTS and CTS wiring through the adapter")
    try:
        observations = diagnose_flow_control_wiring(
            config.serial_port, config.baud_rate, config.open_timeout_s
        )
    except RadioError as exc:
        detail = f"the wiring diagnostic could not run: {exc}"
        log.error("Wiring FAIL: %s", detail)
        return [CheckResult("flow-control-wiring", passed=False, detail=detail)]
    results = [classify_wire(obs) for obs in observations]
    for result in results:
        if result.passed:
            log.info("Wiring PASS: %s", result.detail)
        else:
            log.error("Wiring FAIL: %s", result.detail)
    return results


def check_os_flow_control(config: ProbeConfig, log: logging.Logger) -> list[CheckResult]:
    """Verify the OS is set up for hardware flow control when rtscts is requested.

    Only runs when flow_control is "rtscts": there is nothing to verify for a
    3-wire run. It reads the live device tree (read-only) to catch the silent
    failure mode where the port is opened with rtscts but the kernel never had
    `uart-has-rtscts`, so flow control is not actually active.
    """
    if config.flow_control != "rtscts":
        return []

    state = flow_control.inspect_uart_flow_control(config.serial_port)
    if state is not None and state.uart_has_rtscts and state.fsl_dte_mode:
        detail = (
            f"{config.serial_port}: device tree declares uart-has-rtscts and fsl,dte-mode"
        )
        log.info("OS flow control OK: %s", detail)
        return [CheckResult("os-flow-control", passed=True, detail=detail)]
    if state is not None:
        missing = []
        if not state.uart_has_rtscts:
            missing.append("uart-has-rtscts")
        if not state.fsl_dte_mode:
            missing.append("fsl,dte-mode")
        detail = (
            f"{config.serial_port}: device tree is missing {', '.join(missing)}, "
            "so the expected DTE hardware-flow-control configuration is not active"
        )
        log.error("OS flow control FAIL: %s", detail)
        return [CheckResult("os-flow-control", passed=False, detail=detail)]

    detail = (
        f"{config.serial_port}: could not determine flow-control config "
        "(not Linux, or the device-tree node was not found)"
    )
    log.warning("OS flow control unknown: %s", detail)
    return [CheckResult("os-flow-control", passed=False, detail=detail)]


def check_gpio(
    config: ProbeConfig,
    log: logging.Logger,
    backend: gpio.GpioBackend | None = None,
) -> list[CheckResult]:
    """Check OS accessibility of requested GPIO lines, not adapter continuity."""
    if backend is None:
        backend = gpio.SysfsBackend()
    pins = config.gpio_pins_to_check()
    results: list[CheckResult] = []
    for name in pins:
        pin = PIN_MAP.get(name)
        if pin is None:
            results.append(CheckResult(f"gpio-access:{name}", passed=False, detail="unknown pin"))
            continue
        probe = backend.probe(pin.linux_gpio)
        signal = f", XBee {pin.xbee_signal}" if pin.xbee_signal else ""
        availability = f", note: {pin.availability_note}" if pin.availability_note else ""
        detail = (
            f"X9 pin {pin.x9_pin}, SODIMM {pin.sodimm}, "
            f"linux gpio {pin.linux_gpio}{signal}{availability}; {probe.detail}; "
            "electrical continuity not tested"
        )
        status = "SKIP" if not probe.performed else ("OK" if probe.passed else "FAIL")
        log.info("GPIO access %s: %s (%s)", name, status, detail)
        results.append(
            CheckResult(
                f"gpio-access:{name}",
                passed=probe.passed,
                detail=detail,
                skipped=not probe.performed,
            )
        )
    return results


def check_gpio_wiring(
    config: ProbeConfig,
    log: logging.Logger,
    access_results: list[CheckResult],
    *,
    radio_ok: bool = True,
    line_factory: Callable[[int], gpio.GpioLine] | None = None,
) -> list[CheckResult]:
    """Test electrical continuity of each adapter GPIO using the XBee as the far end.

    A pin is tested only if the radio works and its OS access check passed. An
    untested pin is reported as SKIP because its cause is already reported as a FAIL.
    """
    pins = [PIN_MAP[name] for name in config.gpio_pins_to_check() if name in PIN_MAP]
    if not radio_ok:
        return [
            CheckResult(
                f"gpio-wire:{pin.xbee_signal or pin.name}",
                passed=True,
                detail="not tested: the radio check failed",
                skipped=True,
            )
            for pin in pins
        ]

    factory = line_factory if line_factory is not None else gpio.SysfsLine
    accessible = {r.name for r in access_results if r.passed and not r.skipped}
    results: list[CheckResult] = []
    claimed: list[gpio.GpioLine] = []
    wires: list[GpioWire] = []
    try:
        for pin in pins:
            signal = pin.xbee_signal or pin.name
            if f"gpio-access:{pin.name}" not in accessible:
                results.append(
                    CheckResult(
                        f"gpio-wire:{signal}",
                        passed=True,
                        detail="not tested: the GPIO access check did not pass",
                        skipped=True,
                    )
                )
                continue
            try:
                line = factory(pin.linux_gpio)
            except gpio.GpioLineError as exc:
                results.append(CheckResult(f"gpio-wire:{signal}", passed=False, detail=str(exc)))
                continue
            claimed.append(line)
            net = f"{pin.name} / X9.{pin.x9_pin} <-> XBee {signal}"
            wires.append(GpioWire(signal, net, line))

        if wires:
            log.info("Testing GPIO wiring through the adapter (the XBee will be reset)")
            try:
                observations = diagnose_gpio_wiring(
                    config.serial_port, config.baud_rate, config.open_timeout_s, wires
                )
            except RadioError as exc:
                detail = f"the GPIO wiring test could not run: {exc}"
                log.error("GPIO wiring FAIL: %s", detail)
                results.append(CheckResult("gpio-wiring", passed=False, detail=detail))
            else:
                for obs in observations:
                    result = classify_wire(obs, prefix="gpio-wire")
                    if result.passed:
                        log.info("GPIO wiring PASS: %s", result.detail)
                    else:
                        log.error("GPIO wiring FAIL: %s", result.detail)
                    results.append(result)
    finally:
        for line in claimed:
            line.close()
    return results


def run_all(config: ProbeConfig, log: logging.Logger) -> list[CheckResult]:
    """Run all enabled checks and return their results."""
    try:
        config.validate()
    except ValueError as exc:
        detail = str(exc)
        log.error("Configuration invalid: %s", detail)
        return [CheckResult("configuration", passed=False, detail=detail)]

    results = check_os_flow_control(config, log)
    if any(not result.passed for result in results):
        return results
    radio_results = check_radio(config, log)
    results.extend(radio_results)
    if config.check_gpio:
        access_results = check_gpio(config, log)
        results.extend(access_results)
        radio_ok = all(result.passed for result in radio_results)
        results.extend(check_gpio_wiring(config, log, access_results, radio_ok=radio_ok))
    return results


def summarize(results: list[CheckResult], log: logging.Logger) -> bool:
    """Log a summary and return True if every check passed."""
    passed = sum(1 for r in results if r.passed and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    failed = sum(1 for r in results if not r.passed)
    total = len(results)
    all_ok = failed == 0
    for r in results:
        status = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
        log.info("  [%s] %s - %s", status, r.name, r.detail)
    log.info("Summary: %d passed, %d failed, %d skipped (%d total)", passed, failed, skipped, total)
    return all_ok
