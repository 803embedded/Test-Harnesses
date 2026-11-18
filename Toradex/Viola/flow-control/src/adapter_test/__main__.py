# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Command-line entry point: `python -m adapter_test` or `adapter-test`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .checks import run_all, summarize
from .config import (
    DEFAULT_BAUD_RATE,
    DEFAULT_FLOW_CONTROL,
    DEFAULT_MODULE_VARIANT,
    DEFAULT_OPEN_TIMEOUT_S,
    DEFAULT_SERIAL_PORT,
    DEFAULT_WRITE_TIMEOUT_S,
    PIN_MAP,
    SUPPORTED_MODULE_VARIANTS,
    ProbeConfig,
)
from .logging_setup import setup_logging


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="adapter-test",
        description="Post-assembly test for 803embedded Digi adapter boards "
        "(Toradex Viola + Colibri iMX6ULL).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--port",
        default=None,
        help=f"XBee serial port (default: $XBEE_PORT or {DEFAULT_SERIAL_PORT})",
    )
    parser.add_argument(
        "--baud",
        type=_positive_int,
        default=None,
        help=f"XBee baud rate (default: $XBEE_BAUD or {DEFAULT_BAUD_RATE})",
    )
    parser.add_argument(
        "--flow-control",
        choices=("none", "rtscts"),
        default=None,
        help="UART flow control (default: $XBEE_FLOW_CONTROL or "
        f"{DEFAULT_FLOW_CONTROL})",
    )
    parser.add_argument(
        "--module",
        dest="module_variant",
        choices=SUPPORTED_MODULE_VARIANTS,
        default=None,
        help=f"target SoM (default: $TARGET_MODULE or {DEFAULT_MODULE_VARIANT})",
    )
    parser.add_argument(
        "--open-timeout",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help=f"AT/open timeout (default: $XBEE_OPEN_TIMEOUT or {DEFAULT_OPEN_TIMEOUT_S})",
    )
    parser.add_argument(
        "--write-timeout",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="RTS/CTS verification write timeout "
        f"(default: $XBEE_WRITE_TIMEOUT or {DEFAULT_WRITE_TIMEOUT_S})",
    )
    parser.add_argument(
        "--check-gpio",
        action="store_true",
        help="also check sysfs access to adapter GPIOs (not electrical continuity)",
    )
    parser.add_argument(
        "--gpio-pins",
        nargs="+",
        metavar="NAME",
        choices=sorted(PIN_MAP.keys()),
        help="specific pins to check (default: all mapped pins available on the module)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    parser.add_argument("--syslog", action="store_true", help="log to syslog on a host")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = setup_logging(verbose=args.verbose, use_syslog=args.syslog)

    try:
        config = ProbeConfig.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    if args.port is not None:
        config.serial_port = args.port
    if args.baud is not None:
        config.baud_rate = args.baud
    if args.flow_control is not None:
        config.flow_control = args.flow_control
    if args.module_variant is not None:
        config.module_variant = args.module_variant
    if args.open_timeout is not None:
        config.open_timeout_s = args.open_timeout
    if args.write_timeout is not None:
        config.write_timeout_s = args.write_timeout
    config.check_gpio = args.check_gpio
    config.gpio_pins = tuple(args.gpio_pins) if args.gpio_pins else ()
    try:
        config.validate()
    except ValueError as exc:
        parser.error(str(exc))

    results = run_all(config, log)
    all_ok = summarize(results, log)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
