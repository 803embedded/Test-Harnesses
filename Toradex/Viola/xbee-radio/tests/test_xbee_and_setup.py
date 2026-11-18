# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

import pytest

from fake_radio import FakeRadio
from xbee_link import radio_setup
from xbee_link.config import LinkConfig
from xbee_link.xbee import AtCommandError, TransmitError, XBee, XBeeTimeout

ADDR_A = 0x0013A20000000001
ADDR_B = 0x0013A20000000002


@pytest.fixture(autouse=True)
def fast_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(radio_setup, "_PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(radio_setup, "_BAUD_SETTLE_S", 0.0)


def test_at_commands_and_errors() -> None:
    radio = FakeRadio(ADDR_A)
    xbee = XBee(radio, timeout=0.5)
    assert xbee.get_int("AP") == 1
    assert xbee.address64() == ADDR_A
    assert xbee.max_payload() == 102
    with pytest.raises(AtCommandError):
        xbee.at("ZZ")


def test_timeout_when_radio_silent() -> None:
    radio = FakeRadio(ADDR_A, baud=115200)
    radio.host_baud = 9600
    xbee = XBee(radio, timeout=0.1)
    with pytest.raises(XBeeTimeout):
        xbee.at("AP")


def test_send_and_receive_between_linked_radios() -> None:
    a, b = FakeRadio(ADDR_A), FakeRadio(ADDR_B)
    a.link(b)
    xa, xb = XBee(a, timeout=0.5), XBee(b, timeout=0.5)
    assert xa.send(ADDR_B, b"hello").ok
    packet = xb.receive(0.5)
    assert packet is not None
    assert packet.data == b"hello" and packet.source64 == ADDR_A and packet.rssi_dbm == -40
    with pytest.raises(TransmitError, match="payload too large"):
        xa.send(ADDR_B, bytes(200))
    with pytest.raises(TransmitError, match="MAC ACK"):
        xa.send(0x1234, b"nobody")


def test_setup_finds_radio_at_other_baud_and_applies_settings() -> None:
    radio = FakeRadio(ADDR_A, baud=115200)
    config = LinkConfig.from_dict(
        {"serial": {"baud": 9600}, "radio": {"at": {"BD": 7, "CH": 0x0C, "NI": "node"}}}
    )
    xbee = XBee(radio, timeout=0.3)
    messages: list[str] = []
    radio_setup.configure_radio(xbee, radio, config, messages.append)
    assert radio.params["CH"] == b"\x0c"
    assert radio.params["NI"] == b"node"
    assert radio.host_baud == 115200
    assert any("115200" in m for m in messages)


def test_setup_switches_api_mode_and_baud() -> None:
    radio = FakeRadio(ADDR_A, baud=9600)
    config = LinkConfig.from_dict({"radio": {"api_mode": 2, "at": {"BD": 7}}})
    xbee = XBee(radio, timeout=0.3)
    radio_setup.configure_radio(xbee, radio, config)
    assert radio.params["AP"] == b"\x02"
    assert radio.radio_baud == radio.host_baud == 115200
    assert xbee.escaped
    assert xbee.get_int("AP") == 2


def test_setup_reports_missing_radio() -> None:
    radio = FakeRadio(ADDR_A, baud=1234)
    radio.host_baud = 9600
    xbee = XBee(radio, timeout=0.05)
    config = LinkConfig()
    with pytest.raises(radio_setup.SetupError, match="no XBee answered"):
        radio_setup.find_radio(xbee, radio, [9600], 1)
    assert config.target_baud is None
