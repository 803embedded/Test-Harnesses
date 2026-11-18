# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

"""Command-line link test: ``xbee-link info | rx | tx``.

Run ``rx`` on one board and ``tx`` on the other. Both print live diagnostics
(progress, throughput, XBee supply voltage, RSSI) and a PASS/FAIL summary.
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from . import __version__
from .config import ConfigError, LinkConfig, format_address, parse_address
from .linktest import (
    SupplyMonitor,
    TestResult,
    TransferRecord,
    failure_counts,
    human_bytes,
    human_duration,
    log,
    parse_size,
    print_info,
    print_summary,
    write_json,
)
from .radio_setup import open_radio, read_info
from .serial_port import PosixSerial
from .transfer import BufferReceiver, RadioResetError, TransferError, send_buffer
from .xbee import XBee, XBeeError, XBeeTimeout

DEFAULT_CONFIG_PATH = "/etc/xbee-link/config.toml"
EXIT_PASS, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
_MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_TX_DURATION_S = 300.0
DEFAULT_TRANSFER_SIZE = 256 * 1024
_ESTIMATE_AFTER_S = 5.0  # measure this long before printing a time estimate
_LONG_RUN_WARNING_S = 600.0  # flag --total runs expected to take longer than this


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"{value!r} must be greater than zero")
    return parsed


def _size(value: str) -> int:
    try:
        return parse_size(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _address(value: str) -> int:
    try:
        return parse_address(value)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xbee-link",
        description="Configure an XBee from a config file and test an RF link to a peer.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c", "--config",
        help=f"TOML config (default: $XBEE_LINK_CONFIG, else {DEFAULT_CONFIG_PATH} if present)",
    )
    parser.add_argument("--port", help="override [serial] port")
    parser.add_argument(
        "--no-setup", action="store_true",
        help="do not configure the radio; open it at [serial] baud as it is",
    )
    parser.add_argument(
        "--min-supply-mv", type=int, default=3000, metavar="MV",
        help="fail if the XBee supply (AT%%V) drops below this (default 3000; 0 disables)",
    )
    parser.add_argument("--json", metavar="PATH", help="also write the result as JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="configure the radio and print its identity and settings")

    rx = sub.add_parser("rx", help="receive and verify transfers")
    rx.add_argument("--count", type=int, default=1, help="transfers to receive (0 = until stopped)")
    rx.add_argument(
        "--start-timeout", type=float, default=600.0, metavar="S",
        help="give up if no transfer starts within S seconds (default 600; 0 = wait forever)",
    )
    rx.add_argument("--idle-timeout", type=float, default=15.0, metavar="S")
    rx.add_argument("--peer", type=_address, help="only accept transfers from this address")
    rx.add_argument("--output", metavar="PATH", help="save received data (suffix .N if count > 1)")

    tx = sub.add_parser("tx", help="send transfers to the peer and wait for verification")
    tx.add_argument(
        "--peer", type=_address,
        help="destination 64-bit address (default: $XBEE_PEER, else [link] peer)",
    )
    mode = tx.add_mutually_exclusive_group()
    mode.add_argument(
        "--duration", type=_positive_float, metavar="S",
        help="send random data at full rate for S seconds "
        f"(default: $XBEE_TX_DURATION, else {DEFAULT_TX_DURATION_S:.0f})",
    )
    mode.add_argument(
        "--total", type=_size, metavar="SIZE",
        help="instead, send random data at full rate until SIZE bytes (e.g. 10M) are "
        "verified ($XBEE_TX_TOTAL); the estimated time is printed after a few seconds",
    )
    mode.add_argument("--file", type=Path, help="instead, send this file once")
    tx.add_argument(
        "--transfer-size", type=_size, default=DEFAULT_TRANSFER_SIZE, metavar="SIZE",
        help="bytes per CRC-verified transfer (default 256K); does not change the RF load",
    )
    tx.add_argument("--window", type=int, default=4, help="frames in flight (default 4)")
    tx.add_argument("--seed", type=int, default=1, help="random data seed (default 1)")
    return parser


def _load_config(path: str | None) -> tuple[LinkConfig, str]:
    path = path or os.environ.get("XBEE_LINK_CONFIG")
    if path:
        return LinkConfig.load(path), path
    if os.path.exists(DEFAULT_CONFIG_PATH):
        return LinkConfig.load(DEFAULT_CONFIG_PATH), DEFAULT_CONFIG_PATH
    return LinkConfig(), "built-in defaults (no AT changes)"


def _env_peer() -> int | None:
    value = os.environ.get("XBEE_PEER", "").strip()
    return parse_address(value) if value else None


def _resolve_tx_mode(args: argparse.Namespace) -> None:
    """Settle on exactly one of --duration, --total and --file.

    Command-line options win. Otherwise $XBEE_TX_DURATION or $XBEE_TX_TOTAL
    (setting both is an error), otherwise the default duration.
    """
    if args.transfer_size <= 0:
        raise ConfigError("--transfer-size must be greater than zero")
    if args.file is None and args.duration is None and args.total is None:
        duration = os.environ.get("XBEE_TX_DURATION", "").strip()
        total = os.environ.get("XBEE_TX_TOTAL", "").strip()
        if duration and total:
            raise ConfigError(
                "set either XBEE_TX_DURATION (seconds) or XBEE_TX_TOTAL (bytes), not both"
            )
        try:
            if total:
                args.total = parse_size(total)
            else:
                args.duration = _positive_float(duration) if duration else DEFAULT_TX_DURATION_S
        except (ValueError, argparse.ArgumentTypeError) as exc:
            name = "XBEE_TX_TOTAL" if total else "XBEE_TX_DURATION"
            raise ConfigError(f"invalid {name}: {exc}") from exc
    if args.total is not None and args.total <= 0:
        raise ConfigError("the total size must be greater than zero")


def _terminate(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # `docker stop` (SIGTERM) and a dropped terminal (SIGHUP) end the run cleanly.
    signal.signal(signal.SIGTERM, _terminate)
    if hasattr(signal, "SIGHUP"):  # not on Windows, where only the unit tests run
        signal.signal(signal.SIGHUP, _terminate)

    try:
        config, source = _load_config(args.config)
        if args.port:
            config.serial.port = args.port
            config.validate()
        if args.command == "tx":
            _env_peer()  # validate early
            _resolve_tx_mode(args)
    except ConfigError as exc:
        log(f"config error: {exc}", sys.stderr)
        return EXIT_USAGE
    log(f"xbee-link {__version__}: config {source}")
    log(f"opening {config.serial.port} (setup {'skipped' if args.no_setup else 'enabled'})")

    try:
        xbee = open_radio(config, log, setup=not args.no_setup)
    except (OSError, XBeeError) as exc:
        log(f"FAIL: radio setup: {exc}")
        return EXIT_FAIL
    except KeyboardInterrupt:
        log("interrupted")
        return EXIT_FAIL

    try:
        transport = xbee.transport
        baud = transport.baud if isinstance(transport, PosixSerial) else config.serial.baud
        info = read_info(xbee, baud)
        log("radio ready:")
        print_info(info, config.serial.port)
        if args.command == "info":
            return EXIT_PASS
        min_mv = args.min_supply_mv or None
        if args.command == "rx":
            result = run_rx(xbee, info.address64, args, min_mv)
        else:
            peer = args.peer if args.peer is not None else _env_peer()
            if peer is None:
                peer = config.peer
            if peer is None:
                log("FAIL: no peer address; pass --peer, set $XBEE_PEER or set [link] peer "
                    "(the rx side prints its address)")
                return EXIT_USAGE
            result = run_tx(xbee, info.address64, peer, args, min_mv)
    except XBeeError as exc:
        log(f"FAIL: {exc}")
        return EXIT_FAIL
    finally:
        xbee.close()

    print_summary(result)
    if args.json:
        write_json(result, args.json)
        log(f"result written to {args.json}")
    return EXIT_PASS if result.passed else EXIT_FAIL


def run_rx(xbee: XBee, local: int, args: argparse.Namespace, min_mv: int | None) -> TestResult:
    peer = format_address(args.peer) if args.peer is not None else None
    result = TestResult("rx", format_address(local), peer, min_mv)
    monitor = SupplyMonitor(xbee)
    monitor.sample(force=True)
    receiver = BufferReceiver(xbee)
    start_timeout = args.start_timeout or None
    index = 0
    try:
        while args.count <= 0 or index < args.count:
            index += 1
            log(f"rx {index}: listening on {format_address(local)} ...")
            started = time.monotonic()

            def progress(done: int, total: int, index: int = index) -> None:
                monitor.sample()
                rssi = xbee.last_rssi_dbm()
                pct = 100 * done / total if total else 100
                log(f"rx {index}: {human_bytes(done)} / {human_bytes(total)} ({pct:3.0f}%)  "
                    f"RSSI {'n/a' if rssi is None else f'{rssi} dBm'}  {monitor.describe()}")

            try:
                received = receiver.receive(
                    start_timeout=start_timeout, idle_timeout=args.idle_timeout,
                    accept_from=args.peer, on_progress=progress, progress_interval=2.0,
                )
            except XBeeTimeout as exc:
                log(f"rx {index}: {exc}")
                if not result.transfers:
                    result.failures.append(str(exc))
                break
            except TransferError as exc:
                log(f"rx {index}: FAILED: {exc}")
                result.transfers.append(TransferRecord(index, False, 0, time.monotonic() - started,
                                                       error=str(exc)))
                if isinstance(exc, RadioResetError):
                    break
                continue
            monitor.sample(force=True)
            result.transfers.append(TransferRecord(index, True, received.size, received.duration_s,
                                                   duplicates=received.duplicates))
            rssi = "n/a" if received.last_rssi_dbm is None else f"{received.last_rssi_dbm} dBm"
            log(f"rx {index}: verified {human_bytes(received.size)} (CRC32 OK) from "
                f"{format_address(received.source64)} in {received.duration_s:.1f} s, "
                f"{human_bytes(received.throughput_bps)}/s, {received.duplicates} duplicates, "
                f"RSSI {rssi}")
            if args.output:
                path = args.output if args.count == 1 else f"{args.output}.{index}"
                Path(path).write_bytes(received.data)
                log(f"rx {index}: saved to {path}")
    except KeyboardInterrupt:
        log("stopped")
    result.finalize(xbee, monitor)
    return result


def run_tx(
    xbee: XBee, local: int, peer: int, args: argparse.Namespace, min_mv: int | None
) -> TestResult:
    result = TestResult("tx", format_address(local), format_address(peer), min_mv)
    monitor = SupplyMonitor(xbee)
    monitor.sample(force=True)
    file_data = args.file.read_bytes() if args.file else None
    run_started = time.monotonic()
    deadline = run_started + args.duration if args.duration is not None else None
    remaining: int | None = args.total  # bytes still to be verified in --total mode
    if file_data is not None:
        log(f"plan: send {args.file} ({human_bytes(len(file_data))}) once")
    elif remaining is not None:
        log(f"plan: send {human_bytes(remaining)} at full rate, verified every "
            f"{human_bytes(args.transfer_size)}; time estimate follows once the rate is known")
    else:
        log(f"plan: send at full rate for {human_duration(args.duration)}, verified every "
            f"{human_bytes(args.transfer_size)}")
    delivered = 0  # verified bytes so far
    estimated = False
    consecutive_failures = 0
    rate_bps = 0.0  # measured throughput, used to size the last transfer
    index = 0

    def announce_estimate(rate: float) -> None:
        if remaining is not None and args.total is not None:
            eta = (args.total - delivered) / rate
            finish = time.strftime("%H:%M", time.localtime(time.time() + eta))
            prefix = "WARNING: " if eta > _LONG_RUN_WARNING_S else ""
            log(f"{prefix}measured {human_bytes(rate)}/s: {human_bytes(args.total)} will take "
                f"about {human_duration(eta + time.monotonic() - run_started)} in total "
                f"(done around {finish}); press Ctrl-C or `docker stop` to abort")
        elif deadline is not None:
            log(f"measured {human_bytes(rate)}/s: {human_duration(args.duration)} will send "
                f"about {human_bytes(rate * args.duration)}")

    try:
        while True:
            index += 1
            if file_data is not None:
                data = file_data
            else:
                size = args.transfer_size
                if remaining is not None:
                    size = min(size, remaining)
                elif deadline is not None and rate_bps > 0:
                    # Shrink the final transfer so the run ends close to the deadline.
                    left = deadline - time.monotonic()
                    size = max(1, min(size, int(rate_bps * left)))
                data = random.Random(args.seed + index).randbytes(size)
            log(f"tx {index}: sending {human_bytes(len(data))} to {format_address(peer)}")
            started = time.monotonic()

            def progress(
                done: int, total: int, index: int = index, started: float = started,
                before: int = delivered,
            ) -> None:
                nonlocal estimated
                monitor.sample()
                now = time.monotonic()
                elapsed = now - started
                rate = human_bytes(done / elapsed) + "/s" if elapsed > 0 else "-"
                pct = 100 * done / total if total else 100
                overall = before + done
                run_elapsed = now - run_started
                overall_rate = overall / run_elapsed if run_elapsed > 0 else 0.0
                if not estimated and run_elapsed >= _ESTIMATE_AFTER_S and overall_rate > 0:
                    estimated = True
                    announce_estimate(overall_rate)
                status = ""
                if args.total is not None and estimated:
                    eta = (args.total - overall) / overall_rate
                    status = (f"  | {human_bytes(overall)} / {human_bytes(args.total)}, "
                              f"{human_duration(eta)} left")
                elif deadline is not None:
                    status = f"  | {human_duration(max(0.0, deadline - now))} left"
                log(f"tx {index}: {human_bytes(done)} / {human_bytes(total)} ({pct:3.0f}%)  "
                    f"{rate}  {monitor.describe()}{status}")

            try:
                sent = send_buffer(xbee, peer, data, window=args.window,
                                   on_progress=progress, progress_interval=2.0)
            except TransferError as exc:
                consecutive_failures += 1
                log(f"tx {index}: FAILED: {exc}")
                result.transfers.append(TransferRecord(index, False, len(data),
                                                       time.monotonic() - started, error=str(exc)))
                if isinstance(exc, RadioResetError) or (
                    consecutive_failures >= _MAX_CONSECUTIVE_FAILURES
                ):
                    break
            else:
                consecutive_failures = 0
                rate_bps = sent.throughput_bps or rate_bps
                delivered += sent.size
                if remaining is not None:
                    remaining -= sent.size
                monitor.sample(force=True)
                result.transfers.append(TransferRecord(
                    index, True, sent.size, sent.duration_s,
                    retransmissions=sent.retransmissions, frames_sent=sent.frames_sent,
                    delivery_failures=failure_counts(sent.delivery_failures),
                ))
                log(f"tx {index}: peer verified {human_bytes(sent.size)} (CRC32 OK) in "
                    f"{sent.duration_s:.1f} s, {human_bytes(sent.throughput_bps)}/s, "
                    f"{sent.frames_sent} frames, {sent.retransmissions} retransmitted")
            if file_data is not None:
                break
            if remaining is not None:
                if remaining <= 0:
                    break
            elif deadline is None or time.monotonic() >= deadline:
                break
    except KeyboardInterrupt:
        log("stopped before the run was complete")
        result.failures.append(
            f"aborted after {human_bytes(delivered)} verified "
            f"({human_duration(time.monotonic() - run_started)})"
        )
    result.finalize(xbee, monitor)
    return result


if __name__ == "__main__":
    sys.exit(main())
