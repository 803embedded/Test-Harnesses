# SPDX-License-Identifier: MIT
# Copyright (c) 2026 803embedded LLC

import random
import threading

import pytest

from fake_radio import FakeRadio
from xbee_link import transfer
from xbee_link.frames import ModemStatus
from xbee_link.transfer import BufferReceiver, RadioResetError, TransferError, send_buffer
from xbee_link.xbee import XBee

ADDR_A = 0x0013A20000000001
ADDR_B = 0x0013A20000000002


def _pair(loss: float = 0.0) -> tuple[FakeRadio, FakeRadio, XBee, XBee]:
    a = FakeRadio(ADDR_A, loss=loss, seed=1)
    b = FakeRadio(ADDR_B, loss=loss, seed=2)
    a.link(b)
    return a, b, XBee(a, timeout=1.0), XBee(b, timeout=1.0)


def _run(
    xa: XBee, xb: XBee, data: bytes, **send_kwargs: object
) -> tuple[transfer.SendResult, transfer.ReceiveResult]:
    received: list[transfer.ReceiveResult] = []
    errors: list[BaseException] = []

    def rx() -> None:
        try:
            received.append(BufferReceiver(xb).receive(start_timeout=5, idle_timeout=5))
        except BaseException as exc:  # surfaced in the main thread
            errors.append(exc)

    thread = threading.Thread(target=rx)
    thread.start()
    try:
        sent = send_buffer(xa, ADDR_B, data, **send_kwargs)  # type: ignore[arg-type]
    finally:
        thread.join(10)
    if errors:
        raise errors[0]
    return sent, received[0]


@pytest.mark.parametrize("size", [0, 1, 96, 97, 5000])
def test_transfer_round_trip(size: int) -> None:
    _, _, xa, xb = _pair()
    data = random.Random(size).randbytes(size)
    sent, received = _run(xa, xb, data)
    assert received.data == data
    assert received.source64 == ADDR_A
    assert sent.chunk_size == 96 and sent.chunks == -(-size // 96)
    assert sent.report is not None and sent.report.status == transfer.REPORT_OK


def test_transfer_retransmits_after_delivery_failures() -> None:
    _, _, xa, xb = _pair(loss=0.2)
    data = random.Random(7).randbytes(20000)
    sent, received = _run(xa, xb, data, window=8, retries=20)
    assert received.data == data
    assert sent.retransmissions > 0
    assert sent.delivery_failures.get(0x01, 0) > 0


def test_missing_chunks_are_resent_after_report() -> None:
    _, b, xa, xb = _pair()
    b.drop_next_rx = 0
    data = random.Random(3).randbytes(3000)
    original_transmit = xa.transmit
    dropped = {"done": False}

    def lossy_transmit(dest: int, payload: bytes, *, options: int = 0) -> int:
        # The first DATA chunk is acknowledged but never reaches the receiver.
        if payload[0] == transfer.MSG_DATA and not dropped["done"]:
            dropped["done"] = True
            b.drop_next_rx = 1
        return original_transmit(dest, payload, options=options)

    xa.transmit = lossy_transmit  # type: ignore[method-assign]
    sent, received = _run(xa, xb, data)
    assert received.data == data
    assert sent.rounds == 2


def test_radio_reset_aborts_transfer() -> None:
    a, _, xa, _ = _pair()
    original_transmit = xa.transmit

    def resetting_transmit(dest: int, payload: bytes, *, options: int = 0) -> int:
        if payload[0] == transfer.MSG_DATA:
            a.emit(b"\x8a\x00")  # hardware reset reported mid-transfer
        return original_transmit(dest, payload, options=options)

    xa.transmit = resetting_transmit  # type: ignore[method-assign]
    with pytest.raises(RadioResetError, match="hardware reset"):
        send_buffer(xa, ADDR_B, bytes(1000))
    assert ModemStatus(0) in xa.modem_events


def test_unreachable_peer_fails() -> None:
    a = FakeRadio(ADDR_A)
    xa = XBee(a, timeout=0.2)
    with pytest.raises(TransferError, match="not delivered"):
        send_buffer(xa, ADDR_B, b"data", retries=1, status_timeout=0.2)


def test_report_encoding_round_trip() -> None:
    report = transfer.Report(9, transfer.REPORT_MISSING, 100, 0, 3, (0, 96, 192))
    assert transfer.Report.decode(report.encode()) == report
    with pytest.raises(ValueError):
        transfer.Report.decode(b"\xb4\x01")
