# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

from types import SimpleNamespace

import pytest
from digi.xbee import devices, serial
from digi.xbee.exception import InvalidOperatingModeException
from digi.xbee.models.mode import OperatingMode
from digi.xbee.serial import FlowControl

from adapter_test import radio
from adapter_test.radio import RadioError, RadioInfo, probe_radio


class FakeDevice:
    def __init__(
        self,
        parameters,
        flow_control,
        *,
        close_error=False,
        open_error=None,
    ) -> None:
        self.parameters = parameters
        self.flow_control = flow_control
        self.close_error = close_error
        self.open_error = open_error
        self.comm_iface = SimpleNamespace(write_timeout=None)
        self.timeout = None
        self.opened = False
        self.parameter_reads = []
        self.parameter_writes = []
        self.apply_count = 0

    def set_sync_ops_timeout(self, timeout) -> None:
        self.timeout = timeout

    def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        self.opened = True

    def is_open(self) -> bool:
        return self.opened

    def close(self) -> None:
        self.opened = False
        if self.close_error:
            raise OSError("close broke")

    def get_parameter(self, name):
        self.parameter_reads.append(name)
        return bytes([self.parameters[name]])

    def set_parameter(self, name, value, apply=None) -> None:
        self.parameter_writes.append((name, value, apply))
        self.parameters[name] = value[0]

    def apply_changes(self) -> None:
        self.apply_count += 1

    def write_changes(self) -> None:
        raise AssertionError("WR must not be issued")

    def get_hardware_version(self):
        return "0x2D"

    def get_firmware_version(self):
        return bytes.fromhex("1234")

    def get_64bit_addr(self):
        return "0013A20000000001"

    def get_node_id(self):
        return "fixture"

    def get_pan_id(self):
        return bytes.fromhex("ABCD")


class FakeFactory:
    def __init__(
        self,
        *,
        d6=0,
        d7=1,
        close_error=False,
        transparent_on_first_open=False,
    ) -> None:
        self.parameters = {"D6": d6, "D7": d7}
        self.close_error = close_error
        self.transparent_on_first_open = transparent_on_first_open
        self.devices = []

    def __call__(self, _port, _baud, *, flow_control):
        open_error = None
        if self.transparent_on_first_open and not self.devices:
            open_error = InvalidOperatingModeException(op_mode=OperatingMode.AT_MODE)
        device = FakeDevice(
            self.parameters,
            flow_control,
            close_error=self.close_error,
            open_error=open_error,
        )
        self.devices.append(device)
        return device


class FakeTransparentSerial:
    def __init__(self, state, api_parameters, baud_rate, port, flow_control, timeout) -> None:
        self.state = state
        self.api_parameters = api_parameters
        self.baud_rate = baud_rate
        self.port = port
        self.flow_control = flow_control
        self.timeout = timeout
        self.write_timeout = None
        self.is_open = False
        self.writes = []
        self.last_write = b""
        self.pending = {}

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def reset_input_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass

    def write(self, payload) -> int:
        self.last_write = payload
        self.writes.append(payload)
        return len(payload)

    def read_until(self, _terminator) -> bytes:
        if self.last_write == b"+++":
            return b"OK\r"

        command = self.last_write.removeprefix(b"AT").removesuffix(b"\r").decode("ascii")
        responses = {
            "AP": f"{self.state['AP']:X}",
            "D6": f"{self.state['D6']:X}",
            "D7": f"{self.state['D7']:X}",
            "HV": "41",
            "VR": "1234",
            "SH": "13A200",
            "SL": "01020304",
            "NI": "fixture",
            "ID": "ABCD",
        }
        if command in responses:
            return f"{responses[command]}\r".encode("ascii")
        if command == "D61":
            self.pending["D6"] = 1
        elif command == "D71":
            self.pending["D7"] = 1
        elif command == "AP1":
            self.pending["AP"] = 1
        elif command == "CN":
            self.state.update(self.pending)
            for name in ("D6", "D7"):
                self.api_parameters[name] = self.state[name]
        else:
            return b"ERROR\r"
        return b"OK\r"


class FakeTransparentSerialFactory:
    def __init__(self, parameters) -> None:
        self.parameters = parameters
        self.state = {"AP": 0, **parameters}
        self.ports = []

    def __call__(self, baud_rate, port, *, flow_control, timeout):
        raw_port = FakeTransparentSerial(
            self.state,
            self.parameters,
            baud_rate,
            port,
            flow_control,
            timeout,
        )
        self.ports.append(raw_port)
        return raw_port


def test_none_path_opens_once_and_does_not_touch_parameters(monkeypatch) -> None:
    factory = FakeFactory()
    monkeypatch.setattr(devices, "XBeeDevice", factory)

    info = probe_radio("loop", 9600, 1.0, "none")

    assert isinstance(info, RadioInfo)
    assert len(factory.devices) == 1
    assert factory.devices[0].flow_control is FlowControl.NONE
    assert factory.devices[0].parameter_reads == []
    assert factory.devices[0].parameter_writes == []


def test_none_path_reads_transparent_radio_without_writing_settings(monkeypatch) -> None:
    factory = FakeFactory(d6=0, d7=0, transparent_on_first_open=True)
    raw_factory = FakeTransparentSerialFactory(factory.parameters)
    monkeypatch.setattr(devices, "XBeeDevice", factory)
    monkeypatch.setattr(serial, "XBeeSerialPort", raw_factory)
    monkeypatch.setattr(radio.time, "sleep", lambda _seconds: None)

    info = probe_radio("loop", 9600, 1.0, "none")

    assert info == RadioInfo("0x41", "1234", "0013A20001020304", "fixture", "abcd")
    assert len(factory.devices) == 1
    assert len(raw_factory.ports) == 1
    raw_port = raw_factory.ports[0]
    assert raw_port.flow_control is FlowControl.NONE
    assert raw_port.timeout == 1.0
    assert raw_port.write_timeout == 1.0
    assert raw_port.writes == [
        b"+++",
        b"ATAP\r",
        b"ATHV\r",
        b"ATVR\r",
        b"ATSH\r",
        b"ATSL\r",
        b"ATNI\r",
        b"ATID\r",
        b"ATCN\r",
    ]
    assert raw_factory.state == {"AP": 0, "D6": 0, "D7": 0}


def test_rtscts_path_sets_parameters_volatile_then_reopens(monkeypatch) -> None:
    factory = FakeFactory(d6=0, d7=1)
    monkeypatch.setattr(devices, "XBeeDevice", factory)

    info = probe_radio("loop", 9600, 1.0, "rtscts", write_timeout_s=0.75)

    assert isinstance(info, RadioInfo)
    assert len(factory.devices) == 2
    setup, verify = factory.devices
    assert setup.flow_control is FlowControl.NONE
    assert verify.flow_control is FlowControl.HARDWARE_RTS_CTS
    assert setup.parameter_writes == [
        ("D6", b"\x01", False),
        ("D7", b"\x01", False),
    ]
    assert setup.parameter_reads == ["D6", "D7"]
    assert verify.parameter_reads == ["D6", "D7"]
    assert setup.apply_count == 1
    assert factory.parameters == {"D6": 1, "D7": 1}
    assert verify.comm_iface.write_timeout == 0.75


def test_rtscts_path_does_not_reapply_enabled_parameters(monkeypatch) -> None:
    factory = FakeFactory(d6=1, d7=1)
    monkeypatch.setattr(devices, "XBeeDevice", factory)

    probe_radio("loop", 9600, 1.0, "rtscts")

    assert factory.devices[0].parameter_writes == []
    assert factory.devices[0].apply_count == 0


def test_rtscts_path_converts_transparent_radio_for_session(monkeypatch) -> None:
    factory = FakeFactory(d6=0, d7=0, transparent_on_first_open=True)
    raw_factory = FakeTransparentSerialFactory(factory.parameters)
    monkeypatch.setattr(devices, "XBeeDevice", factory)
    monkeypatch.setattr(serial, "XBeeSerialPort", raw_factory)
    monkeypatch.setattr(radio.time, "sleep", lambda _seconds: None)

    info = probe_radio("loop", 9600, 1.0, "rtscts", write_timeout_s=0.75)

    assert isinstance(info, RadioInfo)
    assert len(factory.devices) == 2
    assert factory.devices[1].flow_control is FlowControl.HARDWARE_RTS_CTS
    assert factory.devices[1].comm_iface.write_timeout == 0.75
    assert raw_factory.ports[0].writes == [
        b"+++",
        b"ATAP\r",
        b"ATD6\r",
        b"ATD7\r",
        b"ATD61\r",
        b"ATD71\r",
        b"ATAP1\r",
        b"ATCN\r",
    ]
    assert raw_factory.state == {"AP": 1, "D6": 1, "D7": 1}
    assert factory.parameters == {"D6": 1, "D7": 1}
    assert all(b"WR" not in command for command in raw_factory.ports[0].writes)


def test_constructor_error_is_wrapped(monkeypatch) -> None:
    def invalid_device(*_args, **_kwargs):
        raise ValueError("bad baud")

    monkeypatch.setattr(devices, "XBeeDevice", invalid_device)
    with pytest.raises(RadioError, match="bad baud"):
        probe_radio("loop", -1, 1.0)


def test_close_error_is_wrapped(monkeypatch) -> None:
    factory = FakeFactory(close_error=True)
    monkeypatch.setattr(devices, "XBeeDevice", factory)
    with pytest.raises(RadioError, match="closing loop failed"):
        probe_radio("loop", 9600, 1.0)
