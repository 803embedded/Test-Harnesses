# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

import logging

from adapter_test import checks
from adapter_test.config import ProbeConfig
from adapter_test.gpio import GpioProbeResult
from adapter_test.radio import RadioError, RadioInfo, WireObservation

log = logging.getLogger("test")


class _PassingGpioBackend:
    def probe(self, linux_gpio: int) -> GpioProbeResult:
        return GpioProbeResult(
            performed=True,
            passed=True,
            detail=f"GPIO {linux_gpio} accessed by test backend",
        )


def _wire(wire="cts", **kw):
    """A working-CTS observation by default; override fields per test."""
    base = dict(
        wire=wire,
        net="net",
        reading_active=True,
        reading_idle=False,
        expected_active=True,
        expected_idle=False,
    )
    base.update(kw)
    return WireObservation(**base)


def test_check_radio_success(monkeypatch) -> None:
    info = RadioInfo(
        hardware_version="0x2D",
        firmware_version="1234",
        address_64bit="0013A200XXXXXXXX",
        node_id="",
        pan_id="abcd",
    )
    monkeypatch.setattr(checks, "probe_radio", lambda *a, **k: info)
    results = checks.check_radio(ProbeConfig(flow_control="none"), log)
    assert len(results) == 1
    assert results[0].passed


def test_rtscts_success_still_tests_each_wire(monkeypatch) -> None:
    info = RadioInfo("0x2D", "1234", "0013A200XXXXXXXX", "", "abcd")
    monkeypatch.setattr(checks, "probe_radio", lambda *a, **k: info)
    monkeypatch.setattr(
        checks,
        "diagnose_flow_control_wiring",
        lambda *_a, **_k: [
            _wire("cts"),
            _wire("rts", expected_active=False, expected_idle=True, reading_active=False,
                  reading_idle=True),
        ],
    )
    results = checks.check_radio(ProbeConfig(flow_control="rtscts"), log)
    assert [r.name for r in results] == ["radio", "flow-control-wire:cts", "flow-control-wire:rts"]
    assert all(r.passed for r in results)


def test_rtscts_stuck_wire_fails_a_passing_probe(monkeypatch) -> None:
    info = RadioInfo("0x2D", "1234", "0013A200XXXXXXXX", "", "abcd")
    monkeypatch.setattr(checks, "probe_radio", lambda *a, **k: info)
    monkeypatch.setattr(
        checks,
        "diagnose_flow_control_wiring",
        lambda *_a, **_k: [
            _wire("cts", reading_active=True, reading_idle=True),  # stuck asserted
            _wire("rts", expected_active=False, expected_idle=True, reading_active=False,
                  reading_idle=True),
        ],
    )
    results = checks.check_radio(ProbeConfig(flow_control="rtscts"), log)
    assert results[0].passed
    assert not results[1].passed and "OPEN" in results[1].detail
    assert checks.summarize(results, log) is False


def test_check_radio_failure(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RadioError("no radio")

    monkeypatch.setattr(checks, "probe_radio", boom)
    results = checks.check_radio(ProbeConfig(), log)
    assert not results[0].passed
    assert "no radio" in results[0].detail


def test_check_gpio_uses_backend_for_all_available_pins() -> None:
    config = ProbeConfig(check_gpio=True)
    results = checks.check_gpio(config, log, backend=_PassingGpioBackend())
    assert results
    assert all(r.passed for r in results)
    assert all(not r.skipped for r in results)
    assert all("continuity not tested" in r.detail for r in results)


def test_check_gpio_defaults_to_sysfs(monkeypatch) -> None:
    backend = _PassingGpioBackend()
    monkeypatch.setattr(checks.gpio, "SysfsBackend", lambda: backend)
    results = checks.check_gpio(ProbeConfig(check_gpio=True), log)
    assert results
    assert all(result.passed for result in results)


def test_check_gpio_uses_only_pins_available_on_wifi_module() -> None:
    config = ProbeConfig(module_variant="imx6ull-512mb-wb", check_gpio=True)
    results = checks.check_gpio(config, log, backend=_PassingGpioBackend())
    names = {result.name for result in results}
    assert "gpio-access:SODIMM_135" in names
    assert "gpio-access:SODIMM_103" not in names


def test_check_gpio_unknown_pin_fails() -> None:
    config = ProbeConfig(gpio_pins=("NOPE",))
    results = checks.check_gpio(config, log, backend=_PassingGpioBackend())
    assert len(results) == 1
    assert not results[0].passed


def test_summarize_reports_overall() -> None:
    good = [checks.CheckResult("a", passed=True, detail="")]
    bad = [
        checks.CheckResult("a", passed=True, detail=""),
        checks.CheckResult("b", passed=False, detail=""),
    ]
    skipped = [checks.CheckResult("a", passed=True, detail="", skipped=True)]
    assert checks.summarize(good, log) is True
    assert checks.summarize(bad, log) is False
    assert checks.summarize(skipped, log) is True


def test_classify_wire_ok() -> None:
    result = checks.classify_wire(_wire())
    assert result.passed and not result.skipped
    assert "OK" in result.detail


def test_classify_wire_open() -> None:
    result = checks.classify_wire(_wire(reading_active=False, reading_idle=False))
    assert not result.passed and not result.skipped
    assert "OPEN" in result.detail


def test_classify_wire_inverted() -> None:
    result = checks.classify_wire(_wire(reading_active=False, reading_idle=True))
    assert not result.passed and not result.skipped
    assert "INVERTED" in result.detail


def test_classify_wire_fault_is_a_failure_with_reason() -> None:
    result = checks.classify_wire(
        _wire(fault="the XBee did not apply the CTS# drive", reading_active=None, reading_idle=None)
    )
    assert not result.passed and not result.skipped
    assert "could not test this wire" in result.detail
    assert "did not apply the CTS# drive" in result.detail


def test_classify_wire_missing_reading_is_a_failure() -> None:
    result = checks.classify_wire(_wire(reading_active=None, reading_idle=None))
    assert not result.passed and not result.skipped
    assert "no reading" in result.detail


def test_rtscts_failure_runs_wire_diagnostic(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RadioError("RTS/CTS verification failed")

    monkeypatch.setattr(checks, "probe_radio", boom)
    monkeypatch.setattr(
        checks,
        "diagnose_flow_control_wiring",
        lambda *_a, **_k: [
            _wire("cts", reading_active=False, reading_idle=False),  # open
            _wire("rts", expected_active=False, expected_idle=True, reading_active=False,
                  reading_idle=True),  # ok
        ],
    )
    results = checks.check_radio(ProbeConfig(flow_control="rtscts"), log)
    names = [r.name for r in results]
    assert names == ["radio", "flow-control-wire:cts", "flow-control-wire:rts"]
    assert not results[0].passed
    assert not results[1].passed and "OPEN" in results[1].detail
    assert results[2].passed


def test_none_failure_skips_wire_diagnostic(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise RadioError("x")

    monkeypatch.setattr(checks, "probe_radio", boom)

    def must_not_diagnose(*_a, **_k):
        raise AssertionError("wire diagnostic must not run for a 3-wire probe")

    monkeypatch.setattr(checks, "diagnose_flow_control_wiring", must_not_diagnose)
    results = checks.check_radio(ProbeConfig(flow_control="none"), log)
    assert [r.name for r in results] == ["radio"]


def test_rtscts_os_failure_aborts_before_radio(monkeypatch) -> None:
    def must_not_probe(*_a, **_k):
        raise AssertionError("probe_radio must not be called after the OS check fails")

    monkeypatch.setattr(checks, "probe_radio", must_not_probe)
    monkeypatch.setattr(checks.flow_control, "inspect_uart_flow_control", lambda _p: None)
    results = checks.run_all(ProbeConfig(flow_control="rtscts"), log)
    assert len(results) == 1
    assert results[0].name == "os-flow-control"
    assert not results[0].passed
