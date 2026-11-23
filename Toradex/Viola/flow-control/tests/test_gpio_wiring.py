# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""GPIO wiring test against a simulated adapter, XBee, and host GPIOs."""

import logging

import pytest
from digi.xbee.exception import TimeoutException
from digi.xbee.io import IOLine, IOValue

from adapter_test import checks, gpio, radio
from adapter_test.config import PIN_MAP, ProbeConfig
from adapter_test.radio import GpioWire, RadioError, diagnose_gpio_wiring

log = logging.getLogger("test-gpio-wiring")

GOOD = "good"
OPEN = "open"
INVERTED = "inverted"
SIGNAL_COMMAND = {"RSSI": "P0", "DTR": "D8", "ON_SLEEP": "D9"}


class Bench:
    """A simulated adapter. Each net is good, open, or inverted."""

    def __init__(self, **faults: str) -> None:
        self.faults = faults
        self.params = {"SM": 0, "P0": 1, "D8": 1, "D9": 1}
        self.host: dict[str, bool | None] = {}  # None = host line is an input
        self.events: list[str] = []
        self.written = False
        self.recovers = True

    def wire(self, signal: str, level: bool) -> bool:
        fault = self.faults.get(signal, GOOD)
        if fault == OPEN:
            return True  # the far end rests at its pull-up
        return not level if fault == INVERTED else level

    @property
    def in_reset(self) -> bool:
        return self.faults.get("RESET", GOOD) == GOOD and self.host.get("RESET") is False


class BenchLine:
    def __init__(self, bench: Bench, signal: str) -> None:
        self.bench = bench
        self.signal = signal
        self.closed = False
        bench.host[signal] = None

    def read(self) -> bool:
        command = SIGNAL_COMMAND[self.signal]
        return self.bench.wire(self.signal, self.bench.params[command] == 5)

    def drive(self, high: bool) -> None:
        if self.signal == "DTR":
            assert self.bench.params["D8"] == 3, "host drove DTR before XBee DIO8 was an input"
        if self.signal == "RESET":
            assert high is False, "RESET# must never be driven high"
        self.bench.host[self.signal] = high
        self.bench.events.append(f"drive:{self.signal}")

    def release(self) -> None:
        self.bench.host[self.signal] = None

    def close(self) -> None:
        self.release()
        self.closed = True


class BenchSample:
    def __init__(self, bench: Bench) -> None:
        self.bench = bench

    def get_digital_value(self, line):
        assert line == IOLine.DIO8
        host = self.bench.host["DTR"]
        level = self.bench.wire("DTR", bool(host))
        return IOValue.HIGH if level else IOValue.LOW


class BenchDevice:
    def __init__(self, bench: Bench) -> None:
        self.bench = bench
        self.opened = True

    def is_open(self) -> bool:
        return self.opened

    def close(self) -> None:
        self.opened = False

    def set_sync_ops_timeout(self, _timeout) -> None:
        pass

    def get_parameter(self, name):
        if self.bench.in_reset:
            raise TimeoutException()
        if name == "VR":
            return b"\x10\x00"
        return bytes([self.bench.params[name]])

    def set_parameter(self, name, value, apply=None) -> None:
        assert name != "WR"
        self.bench.params[name] = value[0]

    def write_changes(self) -> None:
        self.bench.written = True

    def read_io_sample(self):
        return BenchSample(self.bench)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch) -> None:
    monkeypatch.setattr(radio.time, "sleep", lambda _s: None)


def _run(monkeypatch, bench: Bench, signals=("RESET", "RSSI", "DTR", "ON_SLEEP")):
    def fake_probe(*_a, **_k):
        if not bench.recovers:
            raise RadioError("no answer")
        bench.events.append("recovered")

    monkeypatch.setattr(radio, "_open_api_device", lambda *_a: BenchDevice(bench))
    monkeypatch.setattr(radio, "probe_radio", fake_probe)
    wires = [GpioWire(s, f"net-{s}", BenchLine(bench, s)) for s in signals]
    observations = diagnose_gpio_wiring("/dev/ttymxc1", 9600, 1.0, wires)
    return {o.wire: checks.classify_wire(o, prefix="gpio-wire") for o in observations}, observations


def test_good_adapter_passes_every_wire(monkeypatch) -> None:
    bench = Bench()
    results, _ = _run(monkeypatch, bench)
    assert set(results) == {"RESET", "RSSI", "DTR", "ON_SLEEP"}
    assert all(result.passed for result in results.values())
    assert results["RESET"].name == "gpio-wire:RESET"


@pytest.mark.parametrize("signal", ["RESET", "RSSI", "DTR", "ON_SLEEP"])
def test_open_wire_fails_only_that_wire(monkeypatch, signal: str) -> None:
    results, _ = _run(monkeypatch, Bench(**{signal: OPEN}))
    assert not results[signal].passed
    assert "OPEN" in results[signal].detail
    assert all(result.passed for name, result in results.items() if name != signal)


@pytest.mark.parametrize("signal", ["RSSI", "DTR", "ON_SLEEP"])
def test_inverted_wire_is_reported(monkeypatch, signal: str) -> None:
    results, _ = _run(monkeypatch, Bench(**{signal: INVERTED}))
    assert not results[signal].passed
    assert "INVERTED" in results[signal].detail


def test_settings_restored_and_never_written(monkeypatch) -> None:
    bench = Bench()
    _run(monkeypatch, bench)
    assert bench.params == {"SM": 0, "P0": 1, "D8": 1, "D9": 1}
    assert not bench.written
    assert all(level is None for level in bench.host.values())


def test_reset_runs_last_whatever_the_order(monkeypatch) -> None:
    bench = Bench()
    _, observations = _run(monkeypatch, bench, ("RESET", "DTR", "RSSI"))
    assert observations[-1].wire == "RESET"
    assert bench.events.index("drive:DTR") < bench.events.index("drive:RESET")


def test_radio_that_does_not_return_after_reset_fails(monkeypatch) -> None:
    bench = Bench()
    bench.recovers = False
    results, _ = _run(monkeypatch, bench, ("RESET",))
    assert not results["RESET"].passed
    assert "after RESET# was released" in results["RESET"].detail
    assert bench.host["RESET"] is None


def test_reset_finds_radio_back_at_its_stored_baud(monkeypatch) -> None:
    bench = Bench()
    tried: list[int] = []

    def stored_baud_only(_port, baud, *_a, **_k):
        tried.append(baud)
        if baud != 9600:
            raise RadioError("no answer")

    monkeypatch.setattr(radio, "_open_api_device", lambda *_a: BenchDevice(bench))
    monkeypatch.setattr(radio, "probe_radio", stored_baud_only)
    wire = GpioWire("RESET", "net-RESET", BenchLine(bench, "RESET"))
    observations = diagnose_gpio_wiring("/dev/ttymxc1", 230400, 1.0, [wire])
    assert checks.classify_wire(observations[0], prefix="gpio-wire").passed
    assert tried == [230400, 9600]


def test_sleeping_radio_is_refused_without_driving(monkeypatch) -> None:
    bench = Bench()
    bench.params["SM"] = 1
    results, _ = _run(monkeypatch, bench)
    assert all(not result.passed and "SM=1" in result.detail for result in results.values())
    assert not bench.events


def _access(*names: str) -> list[checks.CheckResult]:
    return [checks.CheckResult(f"gpio-access:{n}", passed=True, detail="") for n in names]


def test_check_skips_pins_without_access_and_closes_lines(monkeypatch) -> None:
    bench = Bench()
    lines: list[BenchLine] = []
    by_number = {pin.linux_gpio: pin.xbee_signal for pin in PIN_MAP.values()}

    def factory(number: int) -> BenchLine:
        lines.append(BenchLine(bench, by_number[number]))
        return lines[-1]

    monkeypatch.setattr(radio, "_open_api_device", lambda *_a: BenchDevice(bench))
    monkeypatch.setattr(radio, "probe_radio", lambda *_a, **_k: None)
    results = checks.check_gpio_wiring(
        ProbeConfig(check_gpio=True),
        log,
        _access("SODIMM_135", "SODIMM_98", "SODIMM_133"),
        line_factory=factory,
    )
    by_name = {result.name: result for result in results}
    assert by_name["gpio-wire:ON_SLEEP"].skipped
    assert by_name["gpio-wire:RESET"].passed and not by_name["gpio-wire:RESET"].skipped
    assert len(lines) == 3 and all(line.closed for line in lines)


def test_check_skips_everything_when_radio_failed() -> None:
    def must_not_claim(_number: int):
        raise AssertionError("no line may be claimed after a radio failure")

    results = checks.check_gpio_wiring(
        ProbeConfig(check_gpio=True), log, [], radio_ok=False, line_factory=must_not_claim
    )
    assert len(results) == 4
    assert all(result.skipped for result in results)


def test_wifi_module_omits_on_sleep() -> None:
    results = checks.check_gpio_wiring(
        ProbeConfig(module_variant="imx6ull-512mb-wb", check_gpio=True), log, [], radio_ok=False
    )
    assert "gpio-wire:ON_SLEEP" not in {result.name for result in results}


def test_run_all_runs_wiring_only_with_check_gpio(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        checks, "check_radio", lambda *_a: [checks.CheckResult("radio", True, "")]
    )
    monkeypatch.setattr(checks, "check_gpio", lambda *_a: [])
    monkeypatch.setattr(
        checks,
        "check_gpio_wiring",
        lambda *_a, radio_ok: calls.append(radio_ok) or [],
    )
    checks.run_all(ProbeConfig(flow_control="none"), log)
    assert calls == []
    checks.run_all(ProbeConfig(flow_control="none", check_gpio=True), log)
    assert calls == [True]


def _sysfs(tmp_path, monkeypatch, direction: str = "in"):
    gpio_dir = tmp_path / "gpio24"
    gpio_dir.mkdir()
    (gpio_dir / "direction").write_text(direction)
    (gpio_dir / "value").write_text("1")
    monkeypatch.setattr(gpio, "SYSFS_ROOT", tmp_path)
    return gpio_dir


def test_sysfs_line_drives_reads_and_ends_as_input(tmp_path, monkeypatch) -> None:
    gpio_dir = _sysfs(tmp_path, monkeypatch)
    line = gpio.SysfsLine(24, settle_s=0)
    assert line.read() is True
    line.drive(False)
    assert (gpio_dir / "direction").read_text() == "low"
    line.close()
    assert (gpio_dir / "direction").read_text() == "in"


def test_sysfs_line_refuses_an_existing_output(tmp_path, monkeypatch) -> None:
    gpio_dir = _sysfs(tmp_path, monkeypatch, direction="out")
    with pytest.raises(gpio.GpioLineError, match="left unchanged"):
        gpio.SysfsLine(24, settle_s=0)
    assert (gpio_dir / "direction").read_text() == "out"
