# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

import argparse
from pathlib import Path

import pytest

from xbee_link.__main__ import _resolve_tx_mode, build_parser, main
from xbee_link.config import ConfigError, LinkConfig, bd_to_baud, parse_address
from xbee_link.linktest import human_duration, parse_size

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "viola-imx6ull.toml"


def test_example_config_loads() -> None:
    config = LinkConfig.load(EXAMPLE)
    assert config.serial.port == "/dev/ttymxc1"
    assert config.serial.flow_control == "rtscts"
    assert config.radio.at["MY"] == 0xFFFE
    assert config.target_baud == 230400
    assert config.peer is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"serial": {"flow_control": "xonxoff"}}, "flow_control"),
        ({"radio": {"at": {"AP": 1}}}, "api_mode"),
        ({"radio": {"at": {"TOOLONG": 1}}}, "two-character"),
        ({"radio": {"at": {"D7": 0}}}, "D7 must be 1"),
        ({"radio": {"at": {"BD": 11}}}, "BD=11"),
        ({"radio": {"at": {"CH": True}}}, "integer or a string"),
        ({"radio": {"api_mode": 3}}, "api_mode"),
        ({"link": {"peer": "xyz"}}, "invalid 64-bit"),
        ({"serial": {"bogus": 1}}, "unknown key"),
    ],
)
def test_invalid_config(raw: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        LinkConfig.from_dict(raw)


def test_helpers() -> None:
    assert parse_address("0013A200 12345678") == 0x0013A20012345678
    assert parse_address("0x1") == 1
    assert bd_to_baud(7) == 115200
    assert bd_to_baud(250000) == 250000
    assert parse_size("256K") == 262144
    assert parse_size("1MiB") == 1048576
    assert parse_size("100") == 100
    with pytest.raises(ValueError):
        parse_size("lots")
    assert human_duration(45) == "45 s"
    assert human_duration(200) == "3 min 20 s"
    assert human_duration(7500) == "2 h 05 min"


def test_parser_accepts_commands() -> None:
    args = build_parser().parse_args(["tx", "--peer", "0013A20012345678", "--duration", "60"])
    assert args.peer == 0x0013A20012345678 and args.duration == 60
    assert args.transfer_size == 256 * 1024
    args = build_parser().parse_args(["rx", "--count", "0"])
    assert args.count == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--duration", "10", "--total", "1M"],
        ["--duration", "10", "--file", "x.bin"],
        ["--total", "1M", "--file", "x.bin"],
        ["--duration", "0"],
    ],
)
def test_tx_modes_are_exclusive(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["tx", *argv])


def _tx_args(*argv: str) -> argparse.Namespace:
    args = build_parser().parse_args(["tx", *argv])
    _resolve_tx_mode(args)
    return args


def test_tx_mode_defaults_and_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XBEE_TX_DURATION", raising=False)
    monkeypatch.delenv("XBEE_TX_TOTAL", raising=False)
    args = _tx_args()
    assert args.duration == 300 and args.total is None

    monkeypatch.setenv("XBEE_TX_TOTAL", "10M")
    args = _tx_args()
    assert args.total == 10 * 1024 * 1024 and args.duration is None

    args = _tx_args("--duration", "5")  # the command line wins over the environment
    assert args.duration == 5 and args.total is None
    assert _tx_args("--file", str(tmp_path)).duration is None

    monkeypatch.setenv("XBEE_TX_DURATION", "60")
    with pytest.raises(ConfigError, match="not both"):
        _tx_args()

    monkeypatch.delenv("XBEE_TX_TOTAL")
    monkeypatch.setenv("XBEE_TX_DURATION", "soon")
    with pytest.raises(ConfigError, match="XBEE_TX_DURATION"):
        _tx_args()

    monkeypatch.delenv("XBEE_TX_DURATION")
    with pytest.raises(ConfigError, match="greater than zero"):
        _tx_args("--total", "0")


def test_main_reports_config_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[serial]\nflow_control = 'maybe'\n")
    assert main(["-c", str(bad), "info"]) == 2
