# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

import logging

import pytest

from adapter_test import __main__ as cli
from adapter_test.config import ProbeConfig


def _logger() -> logging.Logger:
    return logging.getLogger("test-main")


def test_main_passes_validated_overrides_to_checks(monkeypatch) -> None:
    captured: list[ProbeConfig] = []
    monkeypatch.setattr(cli.ProbeConfig, "from_env", classmethod(lambda cls: cls()))
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: _logger())
    monkeypatch.setattr(cli, "run_all", lambda config, _log: captured.append(config) or [])
    monkeypatch.setattr(cli, "summarize", lambda _results, _log: True)

    result = cli.main(
        [
            "--flow-control",
            "rtscts",
            "--open-timeout",
            "2.5",
            "--write-timeout",
            "0.75",
        ]
    )

    assert result == 0
    assert len(captured) == 1
    assert captured[0].flow_control == "rtscts"
    assert captured[0].open_timeout_s == 2.5
    assert captured[0].write_timeout_s == 0.75


def test_main_rejects_malformed_environment_before_checks(monkeypatch) -> None:
    monkeypatch.setenv("XBEE_BAUD", "fast")
    monkeypatch.setattr(cli, "setup_logging", lambda **_kwargs: _logger())
    monkeypatch.setattr(
        cli,
        "run_all",
        lambda *_args, **_kwargs: pytest.fail("checks must not run for invalid configuration"),
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.main([])

    assert exc_info.value.code == 2


def test_parser_rejects_non_positive_timeout() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(["--write-timeout", "0"])

    assert exc_info.value.code == 2


@pytest.mark.parametrize("variant", ["imx6ull-512mb-wb", "imx6ull-512mb-wb-it"])
def test_parser_accepts_wifi_module_variants(variant: str) -> None:
    args = cli.build_parser().parse_args(["--module", variant])
    assert args.module_variant == variant
